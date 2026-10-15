"""改写 prompt 的本地规则资产注入器。

审查报告发现：`references/` 下 40+ 份规则文档一句都没有进入 LLM prompt，
而 v3 的台版腔检查在 v4 中整体消失。本模块把本地规则资产压缩成「必执行
硬指令 + 本地规则摘录」，默认注入每一次整章/分段改写调用，不再依赖用户
手动传 --index-lib/--writing-guide。

设计约束：
- 只注入结构/规则/检查标准，不复制示范库中的具体句面；
- 每段摘录有文件出处；文件缺失时静默降级，审计由调用方记录；
- 长度有上限，避免挤占正文字节预算。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .paths import PROJECT_ROOT

# 每次改写必注入的硬指令（不可被参考资产覆盖）
#
# 第 7 条是「只许润色」还是「按白名单增写」，由 NOVEL_EXPANSION_ENABLED 切换。
# 为什么必须是开关而不是直接改：改写器目前的实质是**均匀化算子**（实测全章
# 句长 20.3→13.8、句长标准差 12.1→7.3、长段占比 4.3%→0.3%），
# 「不得增删、段落内部只许润色」正是它的成因。放开增写会改变产出形态，
# 必须可单章开启、随时回退，默认关闭。
_HARD_INSTRUCTION_HEAD = (
    "【本地化硬指令（必须全部执行）】\n"
    "1. 台版腔清零：输出不得出现「喔/耶/喽/呐/的说」等句尾语气词；"
    "句尾「呢/啦/呀」要降到中文小说自然密度，禁止保留台版翻译腔口癖。\n"
    "2. 日式翻译腔清零：不得出现「……的样子」「……的感觉」「但是但是」式句式；"
    "被动句/从句堆叠/多重定语改成自然中式叙述，语言干脆利落，严禁油腻网文套话或市井流气。\n"
    "3. 锁定替换词：规则稿中已经完成的词典替换和用户译名决策是硬约束，"
    "禁止把「蘑菇弹」改回「核弹」、把「散修」改回「流浪剑士」、"
    "把「达官贵人」改回「高层人士」，禁止把任何已替换词改回源词。\n"
    "4. 台版用词清零：软体→软件、网路→网络、部落格→博客、计画→计划、"
    "滑鼠→鼠标、硬碟→硬盘、超商→小卖部、捷运→地铁、脚踏车→自行车、"
    "土司→吐司、社群软体→社交软件、防卫机能→防御功能；繁体字一律转简体。\n"
    "5. 严禁 AI 味与低质模板：不用「众所周知/值得一提的是/总而言之/归根结底」等公文模板；"
    "严禁「说穿了不过/美人的路数/腰是腰曲线是曲线/满打满算/碎成渣」等低俗油腻修辞；"
    "严禁「干净得连寒暄都懒得装/短得连标点都显得多余/发呆发得心虚」等做作感叹与注水垫话；"
    "第一人称守住主角冷淡、克制、死人脸、重度中二且极度自律的戏精特质，独白简练毒舌，反差冷幽默。\n"
    "6. 章末钩子：正文章节最后一段必须落在悬念、反转、威胁、新信息或情绪最高点，"
    "服务短剧断章；附录/后记不要求。\n"
)
_HARD_RULE7_STRICT = (
    "7. 保留事件图、人物、事件顺序、因果关系和世界观功能；不得增删或合并段落，"
    "段落数量不可变；段落内部可以润色和调整表达。\n"
)
_HARD_RULE7_EXPANDING = (
    "7. 保留事件图、人物、事件顺序、因果关系和世界观功能；不得增删或合并段落，"
    "段落数量不可变；段落内部允许润色、调整表达，并按【允许增写】白名单增写。\n"
)
HARD_INSTRUCTION = _HARD_INSTRUCTION_HEAD + _HARD_RULE7_STRICT


def hard_instruction(*, expanding: bool = False) -> str:
    """组装硬指令第 7 条：严格档（只许润色）/ 增写档（按白名单）。"""
    return _HARD_INSTRUCTION_HEAD + (
        _HARD_RULE7_EXPANDING if expanding else _HARD_RULE7_STRICT
    )


# 增写白名单：允许加什么、加多少。开关开启时注入。
#
# 实测依据（探针，10 源段）：授权后模型加出来的东西全部落在白名单内——
# 身体反应（「我把指尖搭在膝头，轻轻敲了两下」「忽然坐直身子」「我偏头」）、
# 感官（「车轮碾过石子的声响，一下一下填进沉默里」「声音压得很低」），
# 一件都没越到黑名单。同时抓到「拆段当复述」的注水苗头，故本批不放开拆段。
EXPANSION_PERMISSION_INSTRUCTION = (
    "【允许增写：白名单（不在名单内的增写一律禁止）】\n"
    "本次改写允许你在**不改情节**的前提下增写。只有下面三类可以加：\n"
    "1. 感官描写：气味、声音、温度、光线的具体质感。每个场景最多加 2 处。\n"
    "2. 身体反应：动作、神态、生理反应（手指收紧、喉结动了动、后退半步）。"
    "每个段落最多加 1 处。\n"
    "3. 主角内心吐槽：第一人称的短促评价，≤15 字，不新增信息，只加态度。"
    "每个段落最多加 1 处。\n"
    "\n"
    "【允许增写：上限】\n"
    "- 字数以【规则稿段落】里给出的**本段字数区间**为准（整章目标为源稿的 95%~120%）；"
    "低于下限会被判为写薄、整段作废重写；超过上限会被输出长度截断、同样作废\n"
    "- 单个段落的字数增幅 ≤ +50%，且不超过 200 字\n"
    "\n"
    "【仍然绝对禁止（一个字都不许变）】\n"
    "- 新增人物、新增事件、新增对白内容、新增设定、新增可读文字"
    "（招牌/碑文/书信内容）\n"
    "- 改变事件顺序、因果关系、人物目标、事件结果\n"
    "- 新增人物关系、时间、地点\n"
    "允许你加的是「怎么说」，不是「说什么」。只有名词、动词、形容词可以变多；"
    "谁做了什么、结果如何，一个字都不许变。\n"
)

_EXPANSION_ENV = "NOVEL_EXPANSION_ENABLED"
_TRUTHY = frozenset({"1", "true", "on", "yes"})


def expansion_enabled() -> bool:
    """增写通道是否启用；默认关闭（行为与改造前逐字一致）。"""
    return os.environ.get(_EXPANSION_ENV, "").strip().lower() in _TRUTHY


# 中国网文感的“建设性”改写指令：解决“只去台腔、只换词、没有网文味”
WEB_NOVEL_STYLE_INSTRUCTION = (
    "【中国网文感改写指令（不是建议，是本次改写的核心目标）】\n"
    "1. 你不是在清洗错词，你是在重写句子。禁止只替换词语、其余照抄原文语序；"
    "每段都要重新组织句式：长句拆短、短句收紧、动词前置、删掉空转修饰。\n"
    "2. 冲突与反转场景：若遇正规战斗或实力暴露，反差要拉满；但在日常/互动/误会喜剧场景中，"
    "严禁强行加戏或群演倒吸凉气，守住冷面与错位幽默。\n"
    "3. 表达自然生动：拒绝刻板翻译腔与机械说明文，心理活动尽量结合神态、眼神与动作呈现；"
    "对白符合角色身份，日常对白保持冷峻与荒诞的反差。\n"
    "4. 金句：每个情绪高潮至少写一句 ≤20 字、无语气词、脱离上下文也能立住的金句。\n"
    "5. 对话像人话：口语毛边、打断、口头禅、吐槽；不要人人都是礼貌完整的书面语。\n"
    "6. 细节具体化：把抽象形容词换成具体物件/动作/数字；少写「很/非常/十分」。\n"
    "7. 节奏变化：一段最多 4-5 个短句；高潮句独立成段；长描写后必接一句短句收束。\n"
)

# 节奏两极版：只在增写通道开启时替换上面第 1 条。
#
# 为什么必须换（2026-09-18 实测）：上面第 1 条的「长句拆短」与 HARD_INSTRUCTION
# 第 2 条的「改成中式短句直叙」是本流水线**自身的**两条向下压力。放开增写后
# 段落长度已经追平源稿（段长 32.9 vs 源稿 33.4），但句长只有 15.7（源稿 20.3）、
# 句长标准差 9.1（源稿 12.1）——即「段落够长、句子被切得太碎」，
# 节奏门剩下的 3 项不达标全集中在这一维。
# 目标取自索引库《龙族》风格卡自己的九轴观察：「长短两极。铺陈用 40~80 字的
# 逗号长句滚动推进；爆点前句子…」——那正是本项目的参照体裁。
WEB_NOVEL_STYLE_INSTRUCTION_EXPANDING = (
    "【中国网文感改写指令（不是建议，是本次改写的核心目标）】\n"
    "1. 你不是在清洗错词，你是在重写句子。禁止只替换词语、其余照抄原文语序；"
    "每段都要重新组织句式。**句子长度要两极分化**：铺陈、氛围、回忆用逗号串起的"
    "40~80 字长句滚动推进；爆点、动作、结论压到一句一顿。禁止把所有句子都压成"
    "同一个长度——通篇等长短句就是白开水。\n"
    "2. 冲突与反转场景：若遇正规战斗或实力暴露，反差要拉满；但在日常/互动/误会喜剧场景中，"
    "严禁强行加戏或群演倒吸凉气，守住冷面与错位幽默。\n"
    "3. 表达自然生动：拒绝刻板翻译腔与机械说明文，心理活动尽量结合神态、眼神与动作呈现；"
    "对白符合角色身份，日常对白保持冷峻与荒诞的反差。\n"
    "4. 金句：每个情绪高潮至少写一句 ≤20 字、无语气词、脱离上下文也能立住的金句。\n"
    "5. 对话像人话：口语毛边、打断、口头禅、吐槽；不要人人都是礼貌完整的书面语。\n"
    "6. 细节具体化：把抽象形容词换成具体物件/动作/数字；少写「很/非常/十分」。\n"
    "7. 节奏变化：一段最多 4-5 个短句；高潮句独立成段；长描写后必接一句短句收束。\n"
)


def web_novel_instruction(*, expanding: bool = False) -> str:
    """网文感改写指令：增写档把第 1 条的「长句拆短」换成「长短两极」。"""
    return (
        WEB_NOVEL_STYLE_INSTRUCTION_EXPANDING
        if expanding
        else WEB_NOVEL_STYLE_INSTRUCTION
    )

# 每个段落都必须重新表达，不允许“同句照抄”
_PER_PARAGRAPH_HEAD = (
    "【逐段重写硬约束】\n"
    "1. 禁止任何输出段落与规则稿段落逐字相同、仅改标点、或只替换几个词后其余照抄。\n"
    "2. 动作/叙述/对话每一类段落都要重写：至少改变语序、断句、用词或叙述角度。\n"
    "3. 如果某一段你感觉“已经很好了不用动”，也必须换一种中文说法表达同样信息。\n"
)
_PER_PARAGRAPH_4_STRICT = (
    "4. 重写不等于加戏：信息量、事件、因果、人物目标不得变化。\n"
)
_PER_PARAGRAPH_4_EXPANDING = (
    "4. 重写不等于加戏。加戏的边界是【允许增写】白名单："
    "白名单内允许（感官描写、身体反应、主角吐槽），"
    "白名单外一切新增（事件、对白内容、人物、设定、可读文字）禁止。"
    "信息量只许不变或变厚，不许丢。\n"
)
PER_PARAGRAPH_REWRITE_INSTRUCTION = _PER_PARAGRAPH_HEAD + _PER_PARAGRAPH_4_STRICT


def per_paragraph_instruction(*, expanding: bool = False) -> str:
    """逐段重写硬约束：第 4 条按开关切换「不得变化」/「只许变厚」。"""
    return _PER_PARAGRAPH_HEAD + (
        _PER_PARAGRAPH_4_EXPANDING if expanding else _PER_PARAGRAPH_4_STRICT
    )

# 翻译腔句式黑名单：不是单个词，而是句子骨架
TRANSLATION_SENTENCE_BLACKLIST = (
    "【翻译腔句式黑名单】\n"
    "1. 倘若/端看/我等/尔等/……而已 → 改为 如果/要是/看/我们/你们/删掉句尾“而已”。\n"
    "2. 所谓的……无关……而是在于…… → 拆成口语短句，如：强不强跟力量没关系，关键看你活成什么样。\n"
    "3. 只要是中文网文不会这么写的句式，必须重新组织，不允许只换一个词。\n"
    "4. 判断标准：把句子读出来，像不像中国人随口讲的；不像就重写。\n"
)

# 动作戏专项：短句、动作优先、删解释
ACTION_SCENE_INSTRUCTION = (
    "【动作戏专项指令（遇到战斗/危机/追逐段落时执行）】\n"
    "1. 一句只写一个动作，动词开头优先：冲进/矮身/反手/踏出。\n"
    "2. 删除解释型叙述：“他明白她马上要使出下一招，因此他踏出一步企图瓦解她的距离”"
    "→ 改写成动作本身：“他没退，反而往前踏了一步，贴进她的攻击死角。”\n"
    "3. 回合结束用拟声或极短结果句独立成段（轰！喀！一刀到底）。\n"
    "4. 战斗段落连续短段，每段不超过 3 句；禁止连续三句纯心理/纯解释。\n"
)

# 群戏对白声线：没有声线表时也强制差异化
VOICE_DIFFERENTIATION_INSTRUCTION = (
    "【对白声线硬约束】\n"
    "1. 多人对话时，每句台词必须让读者不看名字也知道是谁说的。\n"
    "2. 同一个人要有稳定句式习惯：有人爱反问、有人爱短句、有人爱书面语、有人爱口头禅。\n"
    "3. 如果两句台词“换个人说也没区别”，必须重写其中至少一句。\n"
)

# 章末钩子专项（来自索引库跨书共识，不是猜）
CHAPTER_HOOK_INSTRUCTION = (
    "【章末钩子专项指令（正文章节必执行）】\n"
    "1. 最后一句必须是钩子，禁止平静总结、禁止“今天结束了”式收尾。\n"
    "2. 钩子四选一：新问题/质问、新威胁/冲突预告、未完动作/反常细节、可单独截图的金句。\n"
    "3. 把下一章的冲突变量提前一句露面（新人物、新规则、新威胁），但不做预告式旁白。\n"
    "4. 钩子尽量控制在最后 30 字内，短句收束；可以省略号、拟声、定格动作收尾。\n"
    "5. 不能改变已确认事件图，只能强化原结尾的落点表达。\n"
)


def build_voice_hints_block(voice_hints: Any = None) -> str:
    """注入项目/全局角色声线表（可选）。voice_hints 为 dict 或可迭代 (角色, 声线)。"""
    if not voice_hints:
        return ""
    lines: list[str] = []
    if isinstance(voice_hints, dict):
        items = voice_hints.items()
    else:
        items = list(voice_hints)
    for name, voice in items:
        if name and voice:
            lines.append(f"- {name}={voice}")
    if not lines:
        return ""
    return "【角色声线表（群戏时逐人核对，非该角色台词不得套用）】\n" + "\n".join(lines[:50])


def load_voice_hints(workspace: Path | str | None = None) -> Any:
    """从工作区读取可选角色声线表（voice-hints.json）。

    这是“一本书一份”的项目级配置，不是全局强制表；没有文件就不注入。
    """
    if not workspace:
        return None
    path = Path(workspace) / "voice-hints.json"
    if not path.is_file():
        return None
    try:
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 配置损坏只降级为不注入
        return None

# (文件相对 skill 根, 抽取的二级标题关键词列表)
_LOCAL_RULE_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "references/中国化规则.md",
        ("语气词", "章末", "礼貌度", "机构与组织名", "自称", "叙事习惯"),
    ),
    (
        "references/风格基准.md",
        ("一、中国网文风格核心标签", "规则 1", "规则 2", "规则 3", "规则 4", "规则 5", "规则 7"),
    ),
    (
        "references/去AI味-问题清单.md",
        ("六根因", "机械检测层", "改写流程中的规避纪律"),
    ),
    (
        "references/质检规范.md",
        ("硬门槛", "台翻腔"),
    ),
)

_HEADING_RE = re.compile(r"^#{2,4}\s+(.+)$")
_BULLET_OR_TABLE_RE = re.compile(r"^(\s*(?:[-*]|\||[0-9]+[.、])\s*|\s*\|)")


def _section_lines(text: str, keyword: str, max_chars: int) -> str:
    """抽取含 keyword 的标题小节中的要点行（表格行/列表行）。"""
    lines = text.splitlines()
    inside = False
    collected: list[str] = []
    used = 0
    for line in lines:
        heading = _HEADING_RE.match(line)
        if heading:
            inside = keyword in heading.group(1)
            continue
        if not inside:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not _BULLET_OR_TABLE_RE.match(stripped):
            continue
        cleaned = stripped.lstrip("| -").strip()
        if not cleaned or cleaned.startswith("---"):
            continue
        if used + len(cleaned) > max_chars:
            break
        collected.append(cleaned)
        used += len(cleaned) + 1
    return "；".join(collected[:12])


def build_local_rule_block(*, max_chars: int = 1600) -> str:
    """读取本地规则资产并压缩为 prompt 可用的「本地规则摘录」。"""
    parts: list[str] = []
    used = 0
    for rel_path, keywords in _LOCAL_RULE_SOURCES:
        path = PROJECT_ROOT / rel_path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        excerpts: list[str] = []
        for keyword in keywords:
            snippet = _section_lines(text, keyword, max(240, 520 - len(parts) * 40))
            if snippet:
                excerpts.append(f"{keyword}: {snippet}")
        if not excerpts:
            continue
        block = f"[{rel_path}]\n" + "\n".join(excerpts)
        if used + len(block) > max_chars:
            block = block[: max(200, max_chars - used)].rstrip() + "…"
        parts.append(block)
        used += len(block) + 1
        if used >= max_chars:
            break
    return "\n".join(parts)


def build_system_prompt(
    local_rule_block: str = "",
    voice_hints: Any = None,
    *,
    expanding: bool | None = None,
) -> str:
    """组装改写 system prompt：硬指令 + 本地规则摘录 + 声线表（可选）。

    ``expanding=None``（默认）时跟随 ``NOVEL_EXPANSION_ENABLED`` 环境开关；
    显式传 True/False 可覆盖（测试与单章试跑用）。
    """
    if expanding is None:
        expanding = expansion_enabled()
    rules_section = ""
    if local_rule_block:
        rules_section = (
            "\n\n【本地规则摘录（压缩自 skill 内 references/，只作规则依据，禁止照抄句面）】\n"
            f"{local_rule_block}"
        )
    voice_section = build_voice_hints_block(voice_hints)
    expansion_section = (
        "\n\n" + EXPANSION_PERMISSION_INSTRUCTION if expanding else ""
    )
    return (
        web_novel_instruction(expanding=expanding)
        + "\n\n"
        + per_paragraph_instruction(expanding=expanding)
        + "\n\n"
        + TRANSLATION_SENTENCE_BLACKLIST
        + "\n\n"
        + ACTION_SCENE_INSTRUCTION
        + "\n\n"
        + VOICE_DIFFERENTIATION_INSTRUCTION
        + "\n\n"
        + CHAPTER_HOOK_INSTRUCTION
        + expansion_section
        + "\n\n"
        + hard_instruction(expanding=expanding)
        + rules_section
        + (("\n\n" + voice_section) if voice_section else "")
    )


def audit_payload(local_rule_block: str) -> dict[str, Any]:
    """生成本地规则注入审计字段（调用方写入 audit.jsonl）。"""
    return {
        "asset_kind": "local_rules",
        "asset_path": "references/（中国化规则|风格基准|去AI味-问题清单|质检规范）",
        "rule_chars": len(local_rule_block),
        "selection_reason": "v4.3.0 审查整改：本地规则默认注入改写 prompt",
        "copied_surface_text": False,
    }
