# -*- coding: utf-8 -*-
"""生成/检查规则的单一真相源（加速改法工单 T11 / 第二批）。

- `BASE_CHECKS` / `DERIV_CHECKS` **原样**自 `scripts/check_schedule.py` 迁入：
  检查器与生成端引用同一份常量，禁止任何一侧再自带副本。
- `generation_requirements(类型)` 供生成请求携带；`check_compliance` 供生成后就地自检。
- `parse_merged_sections` 同理：合并文档的分节解析只有这一份，生成端与检查器共用。
"""
from __future__ import annotations

import re

SCENE_LAYER_TOKENS = ("景别", "角度", "布光")          # 场景镜头体系（迁自 check_schedule）
THREE_VIEW_TOKENS = ("正面图", "侧面图", "背面图")      # 道具三视图
MAX_IDEOMATIC_MARKS = 10                                # 达到此数**即算违规**（判据是 count("、") >= 该值）
# 因此实际容许的单行顿号上限是这个数。写入生成请求的文案必须引用它，
# 否则模型照「不得超过 MAX_IDEOMATIC_MARKS 个」写，就会被同一份判据拒掉——实测平白制造返工轮次。
DUNHAO_ALLOWED_MAX = MAX_IDEOMATIC_MARKS - 1

# 抽象空洞词黑名单（借 minimax-h3-craft ANTISLOP 实战词表；「华丽」除外——
# 本流水线的服装强化方向体系合法使用该词）。
# 定义在这里（而不是校验器里）是为了让**生成请求、生成后自检、事后校验**引用同一份词表：
# 实测模型写过「极致/唯美」，生成时既没被告知也不自检，一路带到 verify 才被拦下。
ABSTRACT_WORDS = ("震撼", "绝美", "史诗感", "唯美", "大气磅礴", "很酷", "非常漂亮",
                  "惊人", "壮丽", "梦幻", "极致")
# 豁免行（与检查器一致）：负面清单天然是顿号链，机读元数据行不是正文。
# **必须先剥掉列表序号**：描述模板写的是 `7. 负面限制：负面提示词：无人物、无肢体、…`，
# 带序号时按前缀匹配会漏掉整条豁免，于是负面清单被当成"顿号罗列过密"命中
# （实测：道具描述.md#灯笼 就是这样被拦在 verify 的，属判据误报而非内容问题）。
_EXEMPT_LINE_RE = re.compile(r"^(?:\d+[.、)]\s*|[-*]\s*)*(?:负面提示词|负面限制|【)")


def is_exempt_line(line: str) -> bool:
    """该行是否豁免正文类判据（负面清单 / 机读元数据）。序号与项目符号前缀先剥掉再判。"""
    return bool(_EXEMPT_LINE_RE.match(line.strip()))


# 表格单元格里的「空值」唯一集合。
# 此前有三个读者各写一份（aggregate.parse_verdict / scan_registry.PLACEHOLDER_ALIAS /
# check_schedule.split_names），于是同一格写法一边认一边不认——实测「（空）」建索引时算空、
# 引用检查却当成资产名，报「第1集 场景「（空）」未在资产表建档」，白跑到 verify。
PLACEHOLDER_NAMES = frozenset({
    "", "无", "无出场", "未知", "未出现", "-", "—",
    "空", "（空）", "(无)", "（无）", "None", "无别名",
})

# 有衍生模板的类型：其余类型（如服装，走服饰设计线）不进 `--deriv` 通道。
# **生成侧（generate_deriv）与两道检查器（check_schedule / pipeline_state）共用这一份**——
# 只在生成侧略过、检查器照旧索要，就等于把失败从第 9 步搬到 verify（实测：CHAIN_RC=1 停在步骤 10）。
DERIV_CHANNEL_TYPES = ("人物", "场景", "道具")


def split_names(s: str | None) -> set[str]:
    """按顿号/斜杠/逗号切名字列表，丢掉明显的占位写法。"""
    out: set[str] = set()
    for n in re.split(r"[、/，,]", s or ""):
        n = n.strip()
        if n and n not in PLACEHOLDER_NAMES:
            out.add(n)
    return out


def parse_merged_sections(text: str) -> dict[str, str]:
    """解析合并提示词文档的 `## 001. 标题` 分节 → {标题: 正文}。

    单一真相源：**生成端（generate_prompts）与检查器（check_schedule）共用本函数**。
    此前两边各抄一份逐字节相同的实现——只要改一边（例如将来支持别的节标题正则），
    生成端写出的节名与检查器读到的节名就会对不上，表现为"产出的提示词被自己判成缺项"。
    """
    sections: dict[str, str] = {}
    cur_name: str | None = None
    cur_lines: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^## \d{3}\. (.+)$", line)
        if m:
            if cur_name is not None and cur_lines:
                sections[cur_name] = "\n".join(cur_lines).strip()
            cur_name = m.group(1).strip()
            cur_lines = []
            continue
        if cur_name is not None:
            if line.strip() == "---":
                if cur_lines:
                    sections[cur_name] = "\n".join(cur_lines).strip()
                cur_name = None
                cur_lines = []
            else:
                cur_lines.append(line)
    if cur_name is not None and cur_lines:
        sections[cur_name] = "\n".join(cur_lines).strip()
    return {k: v for k, v in sections.items() if v}


def style_lock_layer_checks(card: dict | None, type_key: str) -> list[tuple[str, tuple[str, ...]]]:
    """画风卡每个分层行 → 一条逐字必查（层名取分层行冒号前文字，如 建模层/镜头层）。

    单一真相源：校验器（`check_schedule.build_style_lock_checks`）与**生成侧自检**共用本函数。
    生成侧此前不查这条，于是模型漏抄一层（实测：`人物提示词.md#卖药少年: 缺画风锁定段-镜头`）
    要白跑到 verify 才发现——与"模板要求、校验器也查、唯独生成时不检查"是同一类接线缺口。
    道具跳过场景背景/镜头/构图/角色类分层，场景跳过角色类分层（SKIP_LAYERS_BY_TYPE）。
    """
    from .style import SKIP_LAYERS_BY_TYPE, render_lock_sentences

    checks: list[tuple[str, tuple[str, ...]]] = []
    for sentence in render_lock_sentences(card):
        name = re.sub(r"^\d+\.\s*", "", sentence.split("：", 1)[0].split(":", 1)[0].strip())
        tag = f"画风锁定段-{name}" if name else "渲染锁定段"
        if any(k in tag for k in SKIP_LAYERS_BY_TYPE.get(type_key, ())):
            continue
        checks.append((tag, (sentence,)))
    return checks


# 负面锁的等价写法组：要求里含斜杠的写法不是自然语言，模型会写成等价形式。
# 逐字匹配会把语义相同的内容判成「缺失」，实测在反代口径下导致场景 13 项全被丢弃。
# 只收**完整否定**的写法——若把「无低精度」与「塑料感」拆成两个相邻片段收进来，
# 「无人物、无低精度、塑料感明显、无光照均匀平坦」这种语义相反的文本也会过闸（对抗评审 B1）。
_PLASTIC_LOCK_FORMS = (
    "无低精度/塑料感", "无低精度塑料感", "无低精度、无塑料感",
)
_LIMB_LOCK_FORMS = (
    "无人物/肢体", "无人物肢体", "无人物、无肢体",
)

BASE_CHECKS: dict[str, list[tuple]] = {
    "人物": [
        ("身份锁", ("身份锁",)),
        ("三格图板", ("正面特写", "正视图", "后视图")),
        ("左面特写格", ("左面人物正面特写", "左面一格人物正面特写", "左面正面特写", "左面一格")),
        ("右面双视图格", ("右面正视图与后视图", "右面上下两格", "右面正视图", "右面两格")),
        ("负面段", ("负面提示词：",)),
        ("负面锁", ("后视图无面部", "无额外第四格", "无三格身份不一致", "无三格服装不一致"), "all"),
        ("素灰背景", ("素灰", "#B8B8B8")),
        ("无文字", ("图中不要有任何文字",)),
        ("三维渲染", ("国漫三维", "三维渲染", "物理渲染材质")),
    ],
    "场景": [
        ("镜头体系", SCENE_LAYER_TOKENS),
        ("负面段", ("负面提示词：",)),
        ("负面锁", ("无人物", _PLASTIC_LOCK_FORMS, "无光照均匀平坦"), "all"),
        ("无人", ("画面中无任何人物", "无任何人物", "画面中无人物", "无人物")),
        ("单画面", ("单画面", "主视图")),
        ("无文字", ("图中不要有任何文字",)),
        ("三维渲染", ("国漫三维", "三维渲染", "物理渲染材质")),
    ],
    "道具": [
        ("三视图", THREE_VIEW_TOKENS),
        ("负面段", ("负面提示词：",)),
        ("负面锁", (_LIMB_LOCK_FORMS, "无复杂场景背景"), "all"),
        ("素灰背景", ("素灰", "#B8B8B8")),
        ("无文字", ("图中不要有任何文字",)),
        ("材质", ("物理渲染材质", "材质纹理", "材质", "纹理")),
    ],
}

DERIV_CHECKS: dict[str, list[tuple]] = {
    "人物": list(BASE_CHECKS["人物"]),
    "场景": list(BASE_CHECKS["场景"]),
    "道具": list(BASE_CHECKS["道具"]) + [("三维渲染", ("国漫三维", "三维渲染", "物理渲染材质"))],
}

# 生成侧自检用的最小标记集（与 BASE_CHECKS 同源；顿号/雷同见文末）
_COMPLIANCE_MARKERS: dict[str, tuple[tuple[str, tuple[str, ...], str], ...]] = {
    "人物": (
        ("身份锁", ("身份锁",), "any"),
        ("三格图板", ("正面特写", "正视图", "后视图"), "all"),
        ("负面段", ("负面提示词：",), "any"),
        ("负面锁", ("后视图无面部", "无额外第四格", "无三格身份不一致", "无三格服装不一致"), "all"),
    ),
    "场景": (
        ("镜头体系", SCENE_LAYER_TOKENS, "all"),
        ("负面段", ("负面提示词：",), "any"),
        ("负面锁", ("无人物", _PLASTIC_LOCK_FORMS, "无光照均匀平坦"), "all"),
    ),
    "道具": (
        ("三视图", THREE_VIEW_TOKENS, "all"),
        ("负面段", ("负面提示词：",), "any"),
        ("负面锁", (_LIMB_LOCK_FORMS, "无复杂场景背景"), "all"),
    ),
}

_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "人物": (
        "【硬要求·人物】必须含身份锁段（「身份锁：」），三格图板齐全：左面一格人物正面特写、"
        "右面正视图与后视图两格；必须以「负面提示词：」起头的负面段，且负面锁四项齐全"
        "（后视图无面部/无额外第四格/无三格身份不一致/无三格服装不一致）。",
        f"【硬要求·标点】除负面提示词行外，任何一行顿号（、）不得超过 {DUNHAO_ALLOWED_MAX} 个，不得用顿号罗列堆砌。",
        "【硬要求·层次】各层次（远景/中景/特写）内容不得雷同，禁止换词复读同一句话。",
    ),
    "场景": (
        "【硬要求·场景】必须写全镜头体系三要素：景别、角度、布光，缺一不可；"
        "必须以「负面提示词：」起头的负面段，且负面锁三项齐全"
        "（无人物/无低精度塑料感/无光照均匀平坦）；画面中无任何人物，单画面。",
        f"【硬要求·标点】除负面提示词行外，任何一行顿号（、）不得超过 {DUNHAO_ALLOWED_MAX} 个，不得用顿号罗列堆砌。",
        "【硬要求·层次】各层次内容不得雷同，禁止换词复读同一句话。",
    ),
    "道具": (
        "【硬要求·道具】必须声明三视图保底：正面图、侧面图、背面图，缺一不可；"
        "必须以「负面提示词：」起头的负面段，且负面锁齐全（无人物/肢体、无复杂场景背景）；素灰背景。",
        f"【硬要求·标点】除负面提示词行外，任何一行顿号（、）不得超过 {DUNHAO_ALLOWED_MAX} 个，不得用顿号罗列堆砌。",
        "【硬要求·层次】各层次内容不得雷同，禁止换词复读同一句话。",
    ),
}


def abstract_word_hits(text: str) -> list[str]:
    """正文中命中的抽象空洞词（负面清单行与【】机读元数据行豁免，与检查器同口径）。"""
    body = "\n".join(line.strip() for line in text.splitlines()
                     if line.strip() and not is_exempt_line(line))
    return [w for w in ABSTRACT_WORDS if w in body]


# 用词硬要求：把词表原样告知模型（不告知就等于让它猜哪几个词会被拦）
_ABSTRACT_WORD_REQ = (
    "【硬要求·用词】正文（负面提示词行除外）不得出现抽象空洞词："
    + "、".join(ABSTRACT_WORDS)
    + "。这些词传达不了可渲染的视觉信息，一律换成具体的材质、形制、光比写法。"
)


def generation_requirements(asset_type: str) -> tuple[str, ...]:
    """返回该类型在生成阶段必须满足的硬要求（供生成请求携带）。

    用词要求对三类资产是**同一条**，故在此统一追加，不必在 _REQUIREMENTS 里抄三份。
    """
    return _REQUIREMENTS.get(asset_type, _REQUIREMENTS["人物"]) + (_ABSTRACT_WORD_REQ,)


def token_satisfied(item, text: str) -> bool:
    """单个要求项是否命中。

    item 是字面量串（原样包含即算命中），或**等价写法组**（任一命中即算命中）。
    生成闸（本模块）与事后检查器（`scripts/check_schedule.py`）共用本函数，
    保证「同一份规则、同一套匹配语义」。
    """
    if isinstance(item, str):
        return item in text
    return any(alt in text for alt in item)


def check_compliance(text: str, asset_type: str) -> tuple[bool, list[str]]:
    """生成后就地自检：返回 (是否合规, 缺失/违规标签列表)。与检查器同一份常量。"""
    missing: list[str] = []
    for item in _COMPLIANCE_MARKERS.get(asset_type, _COMPLIANCE_MARKERS["人物"]):
        label, tokens, mode = item[0], item[1], item[2]
        if mode == "all":
            if not all(token_satisfied(t, text) for t in tokens):
                missing.append(label)
        elif not any(token_satisfied(t, text) for t in tokens):
            missing.append(label)
    missing.extend(f"顿号超限: {line[:40]}" for line in dunhao_violations(text))
    return (not missing), missing


def extra_token_missing(text: str, asset_type: str, deriv: bool = False) -> list[str]:
    """**结构性标记**里生成侧尚未硬卡的那些，缺哪条就报哪条（软判据，重试后仍缺不丢产出）。

    与 `check_compliance` 的分工：那套是**硬**判据（缺了要退回/修复），这套是**软**判据
    （模板/画风块逐字注入、模型照抄即得，但实测确有漏抄——如锁定分层），
    来源是**校验器自己的常量** `BASE_CHECKS`/`DERIV_CHECKS`，不再手抄第二份清单。

    这样校验器新增一条 token 规则时，生成侧自动跟上，「规则分叉」这一类病不再复发
    （见 `测试/unit/test_rule_desync_detector.py`）。
    """
    hard = {item[0] for item in _COMPLIANCE_MARKERS.get(asset_type, _COMPLIANCE_MARKERS["人物"])}
    source = DERIV_CHECKS.get(asset_type, DERIV_CHECKS["人物"]) if deriv else \
        BASE_CHECKS.get(asset_type, BASE_CHECKS["人物"])
    missing: list[str] = []
    for item in source:
        label, tokens = item[0], item[1]
        if label in hard:
            continue
        mode = item[2] if len(item) > 2 else "any"
        ok = all(token_satisfied(t, text) for t in tokens) if mode == "all" else \
            any(token_satisfied(t, text) for t in tokens)
        if not ok:
            missing.append(label)
    return missing


def dunhao_violations(text: str) -> list[str]:
    """单行顿号超过 MAX_IDEOMATIC_MARKS 的行（负面清单行豁免，同检查器口径）。"""
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or is_exempt_line(s):
            continue
        if s.count("、") >= MAX_IDEOMATIC_MARKS:
            out.append(s)
    return out


# 描述文档（`--enrich` 产物）的必填标记。
# 原定义在 `scripts/check_schedule.py`（只被校验器使用）；提到这里是为了让**生成侧**
# 能复用同一份判据——否则模板要求、校验器也查，唯独生成时不检查，
# 漏项要白跑到 verify 才发现（实测：35 项漏 1 项「缺描述负面限制」）。
# 结构：类型 → [(缺失标签, 命中任一即算满足的标记组)]
DESC_COMPLIANCE_MARKERS: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "人物": [
        ("描述负面限制", ("负面限制",)),
        ("身份字段", ("面部骨相", "脸型", "肤色")),
    ],
    "场景": [
        ("描述负面限制", ("负面限制",)),
        ("镜头预写", ("景别", "角度", "布光")),
    ],
    "道具": [
        ("描述负面限制", ("负面限制",)),
        ("材质身份", ("材质身份", "材质四件套", "基材")),
    ],
}


def check_desc_compliance(text: str, asset_type: str) -> tuple[bool, list[str]]:
    """描述产物的生成侧自检：返回 (是否合规, 缺失标签列表)。与校验器同一份常量。

    覆盖**必填标记**与**单行顿号上限**；纯中文属文件级检查，仍在校验器里（见 check_schedule）。
    顿号这条是实测补上的：校验器对描述文档也按 AI 味口径查顿号（`scan_desc_pure_chinese`
    → `check_ai_flavor`），生成时却不查，于是「道具描述.md#灯笼: 命中顿号罗列过密」
    照样要白跑到 verify 才拦得住。
    """
    missing: list[str] = []
    for label, words in DESC_COMPLIANCE_MARKERS.get(asset_type, DESC_COMPLIANCE_MARKERS["人物"]):
        if not any(token_satisfied(w, text) for w in words):
            missing.append(label)
    missing.extend(f"顿号超限: {line[:40]}" for line in dunhao_violations(text))
    return (not missing), missing
