#!/usr/bin/env python3
"""Schedule integrity checker v2: verifies episode references, prompt coverage and style lock."""
import argparse
import os
import re
import sys
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))

from engine import style as style_lib
from engine.common import safe_name
from engine.style import content_lock_sentence

ASSET_LIB_DIR = Path(os.environ.get("ASSET_LIB_DIR", "").strip() or SKILL_DIR / "资产库")
FANTASY_GENRES = ("玄幻", "仙侠", "神魔", "修真", "奇幻")


def is_pure_fantasy(genre_text: str) -> bool:
    """True when 题材只由幻想题材词组成（允许连写/带分隔符）。

    「玄幻/仙侠」和「仙侠奇幻」算纯幻想；「都市玄幻」等混合题材不算，
    不强制注入 H7 内容语言段（ADR-0046 混合边界）。
    """
    if not genre_text.strip():
        return False
    residue = genre_text
    for g in FANTASY_GENRES:
        residue = residue.replace(g, "")
    residue = residue.replace("/", "").replace("、", "").replace(",", "").replace("，", "").strip()
    return not residue


def parse_project_genre(text: str) -> str:
    """Extract 题材类型 from the project base info table in 反推_最终.md."""
    in_basic = False
    for line in text.splitlines():
        if line.startswith("## ") and "项目基础信息" in line:
            in_basic = True
            continue
        if in_basic and line.startswith("#"):
            break
        if in_basic and line.startswith("| 题材类型"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            return cells[1].strip() if len(cells) >= 2 else ""
    return ""


# 检查规则单一真相源（工单 T11）：常量定义在 engine/prompt_rules.py，此处只引用
from engine.prompt_rules import (BASE_CHECKS, DERIV_CHECKS,          # noqa: F401
                                 SCENE_LAYER_TOKENS, MAX_IDEOMATIC_MARKS,
                                 token_satisfied)

# 描述文档的必填标记：同一份定义由生成侧（`--enrich` 的自检）与本校验器共用（工单 T2）
from engine.prompt_rules import DESC_COMPLIANCE_MARKERS as DESC_CHECKS

# 抽象空洞词黑名单（借 minimax-h3-craft ANTISLOP 实战词表；「华丽」除外——
# 本流水线的服装强化方向体系合法使用该词）
# 词表定义在 prompt_rules 单一真相源：生成请求与生成后自检引用同一份（工单 F6②）；
# 此处**原样再导出**，历史调用方与被测的 `from check_schedule import ABSTRACT_WORDS` 不受影响
from engine.prompt_rules import ABSTRACT_WORDS, abstract_word_hits as _abstract_word_hits  # noqa: F401
from engine.prompt_rules import is_exempt_line
from engine import prompt_rules

AI_DUNHAO_LIMIT = MAX_IDEOMATIC_MARKS   # 兼容别名（负面提示词行豁免——它合法地是整条顿号链）


def parse_asset_names(text: str) -> tuple[dict[str, set[str]], set[str], set[str]]:
    """Return ({type: primary names}, pending names, alias names) from the final file."""
    names: dict[str, set[str]] = {}
    alias_names: set[str] = set()
    pending_names: set[str] = set()
    cur = None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip()
            continue
        if not cur:
            continue
        if cur.endswith("资产提取表") and line.startswith("|") and not line.startswith("|---"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 3 and not cells[0].startswith("资产ID"):
                t = cur.replace("资产提取表", "").replace("物品", "").replace("妆造", "").strip()
                names.setdefault(t, set()).add(cells[1])
                if t == "人物" and len(cells) >= 3:
                    for a in split_names(cells[2]):
                        alias_names.add(a)
        elif "待确认" in cur and line.startswith("|") and not line.startswith("|---"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2 and cells[0] not in ("名称", "---"):
                pending_names.add(cells[0])
    return names, pending_names, alias_names


def split_names(s: str | None) -> set[str]:
    """Split a comma/slash separated name list, dropping obvious placeholders."""
    return prompt_rules.split_names(s)


# 占位字集合的单一真相源在 engine.prompt_rules（aggregate/scan_registry/本文件共用同一对象）
PLACEHOLDER_NAMES = prompt_rules.PLACEHOLDER_NAMES


LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
# ADR-0084 提示词节头机读元数据行豁免：【资产编号｜稳定键｜…】为中文键结构化元数据，不计入纯中文违规
HEADER_LINE_RE = re.compile(r"^【[^】]*】$")
MERGED_FILES = {"人物": "人物提示词.md", "场景": "场景提示词.md", "道具": "道具提示词.md"}
CROP_PERSON_FILE = "人物提示词_裁头版.md"


# AI 味机械拦截（ADR-0080，词表源自「去AI味」技能的禁用起手式与翻案腔清单，保守收录）
AI_FLAVOR_STARTERS = (
    "说白了", "说穿了", "先说结论", "值得一提的是", "不得不说", "总的来说",
    "综上所述", "总而言之", "换句话说", "可以看到", "我们知道",
    "众所周知", "毫无疑问", "不可否认", "完全可以说", "无法否认",
)
# 抽象空洞词黑名单：词表与命中判定都来自 prompt_rules（生成请求/生成自检/事后校验同一份）
AI_FLAVOR_PATTERNS = (
    re.compile(r"不是[^。，；]{1,20}，?而是"),
    re.compile(r"并非[^。，；]{1,20}，?而是"),
    re.compile(r"不在于[^。，；]{1,20}，?而在于"),
    re.compile(r"与其说[^。，；]{1,20}，?不如说"),
    re.compile(r"看似[^。，；]{1,20}，?实则"),
    re.compile(r"你以为[^。，；]{1,20}，?其实"),
)


# 「命中黑名单」类标签的统一前缀。AI 味拦截是**命中即报**（不是"缺一个词"），
# 而报告层历史上把它们和"缺必填标记"混在同一条「缺{}」通道里——实测报文明明写着
# 「缺抽象空洞词·极致」，读起来像少写了一个词，实际是文里出现了违禁词。
FLAVOR_HIT_PREFIX = "命中·"


def check_ai_flavor(content: str) -> list[str]:
    """AI 味机械拦截：起手式/翻案腔/抽象词 + 顿号罗列/省略号/转折堆砌/量化虚词（负面行豁免）。"""
    labels = []
    if any(w in content for w in AI_FLAVOR_STARTERS) or any(p.search(content) for p in AI_FLAVOR_PATTERNS):
        labels.append(FLAVOR_HIT_PREFIX + "AI味措辞")
    body_lines = [l.strip() for l in content.splitlines()
                  if l.strip() and not is_exempt_line(l)]
    body = "\n".join(body_lines)
    hits = _abstract_word_hits(content)
    if hits:
        labels.append(FLAVOR_HIT_PREFIX + f"抽象空洞词·{'、'.join(hits[:3])}")
    if any(l.count("、") >= AI_DUNHAO_LIMIT for l in body_lines):
        labels.append(FLAVOR_HIT_PREFIX + "顿号罗列过密")
    if body.count("……") >= 2:
        labels.append(FLAVOR_HIT_PREFIX + "过度省略号")
    if len(re.findall(r"微微|些许", body)) >= 3:
        labels.append(FLAVOR_HIT_PREFIX + "量化虚词堆砌")
    if len(re.findall(r"然而|但是|不过|可是", body)) >= 3:
        labels.append(FLAVOR_HIT_PREFIX + "转折堆砌")
    return labels


def format_content_bad_line(fname: str, labels: list[str]) -> str:
    """把「缺必填标记」与「命中黑名单」分两路写清楚（同一文件两类都有则分号并列）。"""
    missing = [l for l in labels if not l.startswith(FLAVOR_HIT_PREFIX)]
    hits = [l[len(FLAVOR_HIT_PREFIX):] for l in labels if l.startswith(FLAVOR_HIT_PREFIX)]
    parts = []
    if missing:
        parts.append("缺" + "、".join(missing))
    if hits:
        parts.append("命中" + "、".join(hits))
    return f"{fname}: " + "；".join(parts)


# 工单 F7：「已判定为非资产」的合法排除标记。与「跳过清单」的 LEGAL_SKIP_MARKERS 同一种语义
# ——已经判定「不建档」的东西，不该再被当成「忘了建档」。
NON_ASSET_MARKERS = ("非资产", "不单独建档", "不建档")


def parse_pending_rows(text: str) -> list[dict]:
    """待确认附录的原始行（名称/类型/说明）——判定理由的唯一出处。"""
    rows: list[dict] = []
    cur = None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip()
            continue
        if not cur or "待确认" not in cur:
            continue
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 3 and cells[0] not in ("名称", "---"):
            rows.append({"名称": cells[0], "类型": cells[1], "说明": cells[2]})
    return rows


def parse_pending_non_assets(text: str) -> set[str]:
    """待确认附录里**已判定为非资产**的名字（说明列写明），它们是分集引用检查的合法排除。"""
    return {r["名称"] for r in parse_pending_rows(text)
            if any(k in r["说明"] for k in NON_ASSET_MARKERS)}


def load_index_aliases(lib: Path) -> set[str]:
    """从**资产索引**取全部别名（结构化字段，不是猜自然语言备注）。

    场景/道具表**没有别名列**，被合并的旧名只落在索引的 `alias_display` 上。
    引用检查若只看 Markdown，就会把"已合并的旧名"误判成"未建档"——
    实测：裁决把「衙门口」并入 `SC_守备前厅`（别名列也写了），章节表按旧名引用照旧被判缺口。

    索引缺失/不可读一律返回空集：退回原有行为，不制造新失败。
    """
    try:
        import json
        data = json.loads((Path(lib) / ".process" / "资产索引.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    names: set[str] = set()
    for a in data.get("assets", []) or []:
        if isinstance(a, dict):
            names |= split_names(str(a.get("alias_display") or ""))
    return names


def check_episode_refs(text: str, extra_names: set[str] | None = None) -> tuple[list[str], list[str]]:
    """分集/章节资产使用表的引用判定：返回 (缺口明细, 已判定非资产的放行明细)。

    判定链（顺序即语义）：
      1) 命中正式表、别名，或 `extra_names`（索引别名）→ 通过；
      2) 已在待确认附录写明**非资产** → 合法排除，记入放行明细（**留痕**，不静默放过）；
      3) 其余 → 缺口：在待确认附录里的给人工出口提示，彻底没出现过的报「未在资产表建档」。
    """
    assets, pending_names, alias_names = parse_asset_names(text)
    table_names: set[str] = set()
    for s in assets.values():
        table_names |= s
    table_names |= alias_names
    # 索引别名（extra_names）只做**精确**命中：它在单独一个标签页里，出现短名（如「府」）时
    # 模糊匹配会把不相关的名字一起放行——实测「摄政王府」因此不再判缺口。
    exact_names = set(extra_names or set())
    non_assets = {r["名称"]: r["说明"] for r in parse_pending_rows(text)
                  if any(k in r["说明"] for k in NON_ASSET_MARKERS)}

    ref_missing: list[str] = []
    legal_skips: list[str] = []
    in_sched = False
    for line in text.splitlines():
        if line.startswith("## ") and "分集" in line:
            in_sched = True
            continue
        if in_sched and (line.startswith("## ") or line.startswith("# ")):
            break
        if in_sched and line.startswith("|") and not line.startswith("|---"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 6 and not cells[0].startswith("章节"):
                ch = cells[0]
                for idx, field in ((2, "人物"), (3, "场景"), (4, "道具"), (5, "服装")):
                    for n in split_names(cells[idx]):
                        matched = (n in table_names or n in exact_names
                                   or any(n in a or a in n for a in table_names))
                        if matched:
                            continue
                        if n in non_assets:
                            legal_skips.append(f"{ch} {field}「{n}」已判定非资产：{non_assets[n]}")
                            continue
                        hint = ("（见待确认附录，请人工决定补录或删除）" if n in pending_names
                                else "未在资产表建档")
                        ref_missing.append(f"{ch} {field}「{n}」{hint}")
    return ref_missing, legal_skips


def check_content(content: str, checks: list[tuple]) -> list[str]:
    """Return missing compliance labels for prompt text (incl. pure-Chinese rule).

    每项检查 = (标签, 词组) 或 (标签, 词组, "all")：默认 any 命中即过；
    "all" 模式要求词组全部出现（如人物负面锁四项缺一不可，ADR-0076）。
    词组元素可以是字面量串或等价写法组，匹配统一由 `token_satisfied` 完成。
    """
    missing = []
    for item in checks:
        label, words = item[0], item[1]
        mode = item[2] if len(item) > 2 else "any"
        if mode == "all":
            ok = all(token_satisfied(w, content) for w in words)
        else:
            ok = any(token_satisfied(w, content) for w in words)
        if not ok:
            missing.append(label)
    check_lines = [ln for ln in content.splitlines() if not HEADER_LINE_RE.match(ln.strip())]
    text_no_color = re.sub(r"#[0-9A-Fa-f]{3,8}", "", "\n".join(check_lines))
    if LATIN_WORD_RE.search(text_no_color):
        missing.append("纯中文")
    return missing


def check_prompt_content(path: Path, checks: list[tuple[str, tuple[str, ...]]]) -> list[str]:
    """Return missing compliance labels for a prompt file."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return ["文件不可读"]
    return check_content(content, checks)


# 合并文档分节解析：实现只有 prompt_rules 一份（此前本文件与 generate_prompts 各抄一份
# 逐字节相同的实现）。此处**原样再导出**，历史调用方与
# `from check_schedule import parse_merged_sections` 的测试不受影响。
from engine.prompt_rules import parse_merged_sections  # noqa: F401


def scan_content(dir_path: Path, checks_map: dict[str, list[tuple[str, tuple[str, ...]]]]) -> list[tuple[str, str, list[str]]]:
    """Scan legacy single prompt files; merged docs are handled separately."""
    bad = []
    if not dir_path.is_dir():
        return bad
    for p in dir_path.iterdir():
        if not p.is_file() or p.suffix != ".md" or p.name in MERGED_FILES.values():
            continue
        t = next((k for k in checks_map if p.name.startswith(k + "_")), None)
        if t is None:
            continue
        missing = check_prompt_content(p, checks_map[t])
        try:
            missing = missing + check_ai_flavor(p.read_text(encoding="utf-8"))
        except OSError:
            pass
        if missing:
            bad.append((p.name, t, missing))
    return bad


# DESC_CHECKS 由 engine.prompt_rules 提供（工单 T2：生成侧与校验器共用同一份判据）


def scan_desc_pure_chinese(desc_dir: Path) -> list[tuple[str, str, list[str]]]:
    """Enforce pure-Chinese rule + minimum required fields on merged description docs."""
    bad = []
    if not desc_dir.is_dir():
        return bad
    for t in ("人物", "场景", "道具"):
        desc_path = desc_dir / f"{t}描述.md"
        if not desc_path.is_file():
            continue
        for name, content in parse_merged_sections(desc_path.read_text(encoding="utf-8")).items():
            missing = []
            for label, words in DESC_CHECKS[t]:
                if not any(w in content for w in words):
                    missing.append(label)
            text_no_color = re.sub(r"#[0-9A-Fa-f]{3,8}", "", content)
            if LATIN_WORD_RE.search(text_no_color):
                missing.append("纯中文")
            missing = missing + check_ai_flavor(content)
            if missing:
                bad.append((f"{desc_path.name}#{name}", "描述", missing))
    return bad


def scan_merged_content(
    path: Path,
    type_key: str,
    checks: list[tuple[str, tuple[str, ...]]],
) -> list[tuple[str, str, list[str]]]:
    """Scan each section of a merged character prompt document."""
    bad = []
    if not path.is_file():
        return bad
    for name, content in parse_merged_sections(path.read_text(encoding="utf-8")).items():
        missing = check_content(content, checks) + check_ai_flavor(content)
        if missing:
            bad.append((f"{path.name}#{name}", type_key, missing))
    return bad


def _kw(s: str) -> str:
    """Stable keyword fragment for style-loss detection."""
    s = s.strip()
    if s and s[0].isascii() and not s[0].isdigit():
        m = re.match(r"[A-Za-z]+", s)
        if m:
            return m.group(0)
    return s[:2]


def style_keywords(style_arg: str | None) -> list[str]:
    """Return stable style keywords for the given style argument (ADR-0076 加固).

    从中文定性首行与分层行抽取 ≥6 字片段（截取前 10 字做子串必查），
    替换旧口径的「前 2 分句 × 前 2 字符」——2 字子串（如「写实」）形同虚设。
    """
    if not style_arg:
        return []
    card, mode = style_lib.resolve_style(None, style_arg)
    if mode == "card" and card:
        frags: list[str] = []
        zh = str(card.get("zh", ""))
        if zh:
            first = re.sub(r"^\d+\.\s*", "", zh.splitlines()[0].strip())
            for seg in re.split(r"[，,、/；;]", first):
                if len(seg.strip()) >= 6:
                    frags.append(seg.strip()[:10])
                    break
        for layer in card.get("layers") or []:
            if len(frags) >= 2:
                break
            body = re.sub(r"^[^：:]*[：:]\s*", "", layer.strip())
            for seg in re.split(r"[，,、/；;]", body):
                if len(seg.strip()) >= 6:
                    frag = seg.strip()[:10]
                    if frag not in frags:
                        frags.append(frag)
                    break
        if frags:
            return frags
        return [_kw(str(card.get("name", "")).strip())]
    return [style_arg.strip()[:8]]


_LENS_KEYWORDS = ("景别", "角度", "布光")


def _strip_lens_declarations(content: str) -> str:
    """剥掉「镜头体系声明」子句（景别/角度/布光），只留层次句。

    这三项是**结构化字段**，取值本来就来自小词表——10 个场景全都写「景别：远景全景」
    是**正确的**。而层次雷同判据要管的是层次**描写句**（前/中/后/远景写了什么）。
    旧实现直接拿整节正文去匹配 `(前景|中景|…)(...)`，会连带命中「景别：远景全景；」，
    于是把每个场景都误报成"层次复读"（实测：真实产出 10 节全部中招）。

    只在**子句起始**处剥离，避免把层次句里偶然出现的"角度"等词也切掉。
    """
    out: list[str] = []
    for line in content.splitlines():
        for piece in re.split(r"([。；;，,])", line):
            if piece.lstrip().startswith(_LENS_KEYWORDS):
                continue
            out.append(piece)
        out.append("\n")
    return "".join(out)


def check_scene_duplicates(base_dir: Path, deriv_dir: Path) -> list[str]:
    """场景层次雷同拦截（ADR-0077）：前/中/后景句跨节逐字重复即报，防套话填空。"""
    issues: list[str] = []
    for d in (base_dir, deriv_dir):
        path = d / "场景提示词.md"
        if not path.is_file():
            continue
        sections = parse_merged_sections(path.read_text(encoding="utf-8"))
        layer_map: dict[str, list[str]] = {}
        for name, content in sections.items():
            found: dict[str, str] = {}
            for m in re.finditer(r"(前景|中景|后景|远景)([^。，；;]{2,40})[，。；;]",
                                 _strip_lens_declarations(content)):
                layer = m.group(1)
                norm = re.sub(r"\s", "", m.group(0))
                # 同一节内同层取首次出现
                if layer not in found:
                    found[layer] = norm
            for layer, norm in found.items():
                layer_map.setdefault(f"{layer}「{norm}」", []).append(name)
        for key, names in layer_map.items():
            if len(names) > 1:
                preview = "、".join(names[:3]) + ("…" if len(names) > 3 else "")
                issues.append(f"{path.name} {key} 在 {len(names)} 节逐字重复（{preview}）——层次内容必须来自各自场景的视觉描述")
    return issues


def check_section_duplicates(pairs: list[tuple[Path, str]]) -> list[str]:
    """内容整节雷同拦截（ADR-0078）：合并文档中「去掉自身资产名后归一化正文」逐字重复即报。

    道具/场景的骨架句（三视图、素灰背景等）各节必然相同，故只在去掉节标题资产名、
    归一化后整体比对——骨架外的材质/痕迹/层次描写雷同才算雷同。
    """
    issues: list[str] = []
    for d, label in pairs:
        path = d / f"{label}提示词.md"
        if not path.is_file():
            continue
        sections = parse_merged_sections(path.read_text(encoding="utf-8"))
        seen: dict[str, str] = {}
        for name, content in sections.items():
            asset_name = re.sub(r"^\d+\.\s*", "", name).strip()
            norm = re.sub(r"[\s，。；;、,：:（）()#\-—…！!？?]", "", content.replace(asset_name, ""))
            if len(norm) < 40:
                continue
            if norm in seen:
                issues.append(
                    f"{path.name} 「{seen[norm]}」与「{name}」正文雷同（除资产名外逐字一致）——各资产描写必须来自各自视觉描述"
                )
            else:
                seen[norm] = name
    return issues


def check_emphasis_format(lib: Path) -> list[str]:
    """Validate optional 强调清单.md structure (ADR-0075)."""
    path = lib / "强调清单.md"
    if not path.exists():
        return []
    errors = []
    current = None
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped in ("## 必须强化", "## 必须避免"):
            current = stripped
            continue
        if stripped.startswith("## "):
            current = None
            continue
        if current and stripped and not stripped.startswith("- "):
            errors.append(f"{path.name}:{no} 在 {current} 分区内，行必须以 `- ` 开头")
    return errors


def check_start_state(lib: Path) -> list[str]:
    """Validate optional .start-state.json required fields (ADR-0075)."""
    path = lib / ".start-state.json"
    if not path.exists():
        return []
    try:
        import json as _json
        data = _json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f".start-state.json 解析失败: {e}"]
    required = ("input_source", "genre", "style", "emphasis_hash", "output_root", "last_confirmed_at")
    missing = [k for k in required if k not in data]
    if missing:
        return [f".start-state.json 缺字段: {', '.join(missing)}"]
    return []


def build_style_lock_checks(
    card: dict[str, str | list[str]] | None,
    type_key: str,
    genre_text: str = "",
) -> list[tuple]:
    """Build per-layer verbatim lock checks (ADR-0069/0070/0076).

    卡片每个分层行按层名建一条逐字必查（层名取分层行冒号前文字，如
    建模层/渲染层/介质层/上色层）；道具跳过场景背景/镜头/构图/角色类分层，
    场景跳过角色类分层。玄幻/仙侠/神魔/修真/奇幻题材的人物/场景另必查
    内容语言段（ADR-0046）。
    """
    checks: list[tuple] = []
    # 防偷懒绕过（ADR-0080）：画风卡按包解析一次，视图定性/负面项检查共用
    pack = style_lib.load_stylepack(str(card.get("pack", ""))) if card else None
    # 逐层锁定：与**生成侧自检**共用同一份实现（engine.prompt_rules），此处不再自带副本
    checks.extend(prompt_rules.style_lock_layer_checks(card, type_key))
    # 视图定性逐字必查（ADR-0077）：挂风格包的卡按类型查 环境/脸部+全身/物件 视图正文
    view_block = style_lib.build_view_block(pack, type_key) if pack else ""
    if view_block and "\n" in view_block:
        view_body = view_block.split("\n", 1)[1].strip()
        if view_body:
            checks.append(("视图定性段", (view_body,)))
    # 负面段画风项必查（ADR-0080）：组装块前 3 项任一必须出现在节内——
    # 防生成器漏掉「模板负面 + 画风卡/风格包负面并集」中的画风负面。
    if card:
        items = [i for i in re.split(r"[、，,]", style_lib.build_negative_block(card, pack, type_key)) if i][:3]
        if items:
            checks.append(("负面段含画风负面", tuple(items)))
    if type_key in ("人物", "场景") and is_pure_fantasy(genre_text):
        content_sentence = content_lock_sentence(card)
        if content_sentence:
            checks.append(("画风锁定段-内容语言", (content_sentence,)))
    return checks


def load_base_skip_text(base_dir: Path) -> str:
    """读取基础提示词跳过清单原文（行首为资产ID）；缺失返回空。"""
    p = base_dir / "跳过清单.md"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


# 工单 4：跳过清单只接受「明确非资产」或「用户明确取消」两种理由；P3/待定冲突不再是合法排除。
LEGAL_SKIP_MARKERS = ("非资产", "用户取消", "取消")


def legal_skip_ids(base_dir: Path) -> set[str]:
    """解析跳过清单，返回理由合法（非资产/用户取消）的资产 ID 集合。

    解析走 `engine.skip_list`（与覆盖率门同一份实现）：这里曾只认短横条目，而生成侧/旧库
    会写表格行，两条门对同一份产物给出相反结论——实测白跑一整趟真端点。
    """
    from engine.skip_list import parse_skip_entries
    entries = parse_skip_entries(load_base_skip_text(base_dir))
    return {aid for aid, reason in entries.items() if any(k in reason for k in LEGAL_SKIP_MARKERS)}


def deriv_prompt_gaps(lib: Path, base_dir: Path, deriv_dir: Path,
                      crop_person_exists: bool) -> list[str]:
    """调度清单里**该有**却缺的衍生提示词（工单：类型以资产索引为准）。

    行类型只信索引：模型把服装写成「道具」时，生成侧按索引识别为服装并记名略过（服装走
    服饰设计线，没有衍生模板），检查器若照旧索要，失败只是从第 9 步搬到 verify。
    """
    from engine.common import load_asset_types
    from engine.prompt_rules import DERIV_CHANNEL_TYPES
    sched_path = lib / "提示词" / "调度清单.md"
    if not sched_path.is_file():
        return []
    type_by_name = load_asset_types(lib)
    crop_deriv_path = deriv_dir / CROP_PERSON_FILE
    crop_deriv_sections = (
        parse_merged_sections(crop_deriv_path.read_text(encoding="utf-8"))
        if crop_person_exists and crop_deriv_path.exists()
        else {}
    )
    out: list[str] = []
    for line in sched_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| 类型"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4 or cells[0] not in MERGED_FILES:
            continue
        if (type_by_name.get(cells[1], "") or cells[0]) not in DERIV_CHANNEL_TYPES:
            continue      # 无衍生通道的类型（如服装）：生成侧已记名略过，这里不再索要
        merged_name = MERGED_FILES[cells[0]]
        deriv_sections = parse_merged_sections(
            (deriv_dir / merged_name).read_text(encoding="utf-8")
        ) if (deriv_dir / merged_name).exists() else {}
        title = f"{cells[1]}（{cells[2]}）"
        legacy = deriv_dir / f"{cells[0]}_{safe_name(cells[1])}_{safe_name(cells[2])}.md"
        if title not in deriv_sections and not legacy.exists():
            out.append(f"{cells[0]}「{cells[1]}」{cells[2]} 缺衍生提示词（{merged_name}）")
        if crop_person_exists and cells[0] == "人物" and title not in crop_deriv_sections:
            out.append(f"人物「{cells[1]}」{cells[2]} 缺裁头版衍生提示词（{CROP_PERSON_FILE}）")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("final", help="反推_最终.md 路径")
    parser.add_argument("--lib", default=str(ASSET_LIB_DIR))
    parser.add_argument("--style", help="画风（库内名称/编号或自由描述）——检查提示词是否保留画风锁定")
    parser.add_argument("--style-lib", action="store_true", help="附带画风库健康自检")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    lib = Path(args.lib)
    text = Path(args.final).read_text(encoding="utf-8")

    # 防偷懒绕过（ADR-0080）：未传 --style 时自动沿用启动状态文件的画风；都没有则显眼警告
    if not args.style:
        state_file = lib / ".start-state.json"
        if state_file.is_file():
            try:
                import json as _json
                args.style = (_json.loads(state_file.read_text(encoding="utf-8")).get("style") or "").strip() or None
                if args.style:
                    print(f"ℹ️ 未传 --style，已沿用启动状态文件画风: {args.style}", file=sys.stderr)
            except Exception:
                pass
    if not args.style:
        print("⚠️ 未提供 --style 且无 .start-state.json：画风锁定/关键词/负面项检查已跳过——请显式传 --style 后重跑", file=sys.stderr)

    # 1. episode reference completeness
    # 判定链（含「已判定非资产」的合法排除）在 check_episode_refs 里，可单独测试；
    # 别名来自索引（场景/道具表没有别名列，旧名只落在索引上），此处仍调一次
    # parse_asset_names 供后续覆盖率检查使用。
    ref_missing, ref_non_asset_skips = check_episode_refs(
        text, extra_names=load_index_aliases(lib))
    assets, pending_names, alias_names = parse_asset_names(text)

    # 2. base prompt coverage + content compliance
    # 工单 4：跳过清单只接受「非资产/用户取消」为合法排除；P3/待定冲突缺提示词一律判违规
    base_dir = lib / "提示词" / "基础"
    deriv_dir = lib / "提示词" / "衍生"
    legal_skips = legal_skip_ids(base_dir)
    id_prefix = {"人物": "CH_", "场景": "SC_", "道具": "PR_"}
    base_missing = []
    crop_person_path = base_dir / CROP_PERSON_FILE
    crop_person_exists = crop_person_path.is_file()
    crop_person_sections = parse_merged_sections(crop_person_path.read_text(encoding="utf-8")) if crop_person_exists else {}
    crop_deriv_exists = (deriv_dir / CROP_PERSON_FILE).is_file()
    for t, merged_name in MERGED_FILES.items():
        if t not in assets:
            continue
        sections = parse_merged_sections(
            (base_dir / merged_name).read_text(encoding="utf-8")
        ) if (base_dir / merged_name).exists() else {}
        for n in assets[t]:
            legacy = base_dir / f"{t}_{safe_name(n)}.md"
            if n not in sections and not legacy.exists():
                if f"{id_prefix[t]}{n}" in legal_skips:
                    continue  # 跳过清单已记录（非资产/用户取消），合法排除
                base_missing.append(f"{t}「{n}」缺基础提示词（{merged_name}）")
            if t == "人物" and crop_person_exists and n not in crop_person_sections:
                base_missing.append(f"人物「{n}」缺裁头版基础提示词（{CROP_PERSON_FILE}）")
            if t == "人物" and crop_deriv_exists and not crop_person_exists and n not in crop_person_sections:
                base_missing.append(f"人物「{n}」存在裁头版衍生但缺裁头版基础提示词（{CROP_PERSON_FILE}）")
    base_checks = {k: list(v) for k, v in BASE_CHECKS.items()}
    deriv_checks = {k: list(v) for k, v in DERIV_CHECKS.items()}
    if args.style:
        card, mode = style_lib.resolve_style(None, args.style)
        if mode == "card":
            genre_text = parse_project_genre(text)
            for t in MERGED_FILES:
                lock_checks = build_style_lock_checks(card, t, genre_text)
                base_checks[t].extend(lock_checks)
                deriv_checks[t].extend(lock_checks)
        # 基础与衍生的所有合并文档都必须保留至少一个画风关键词；
        # 不再只抽查衍生/旧单文件（防基础提示词偷偷丢画风）。
        words = style_keywords(args.style)
        if words:
            for t in MERGED_FILES:
                base_checks[t].append(("画风关键词", tuple(words)))
                deriv_checks[t].append(("画风关键词", tuple(words)))
    content_bad = scan_content(base_dir, base_checks) + scan_content(deriv_dir, deriv_checks)
    for t, merged_name in MERGED_FILES.items():
        content_bad += scan_merged_content(base_dir / merged_name, t, base_checks[t])
        content_bad += scan_merged_content(deriv_dir / merged_name, t, deriv_checks[t])
    if (base_dir / CROP_PERSON_FILE).exists():
        content_bad += scan_merged_content(base_dir / CROP_PERSON_FILE, "人物", base_checks["人物"])
    if (deriv_dir / CROP_PERSON_FILE).exists():
        content_bad += scan_merged_content(deriv_dir / CROP_PERSON_FILE, "人物", deriv_checks["人物"])
    # 描述层同样执行纯中文约束（v2.7.2 起脚本级强制）
    content_bad += scan_desc_pure_chinese(lib / "描述")

    # 3. schedule variant coverage
    deriv_missing = deriv_prompt_gaps(lib, base_dir, deriv_dir, crop_person_exists)

    emphasis_issues = check_emphasis_format(lib)
    state_issues = check_start_state(lib)
    scene_dup_issues = check_scene_duplicates(base_dir, deriv_dir)
    section_dup_issues = check_section_duplicates(
        [(base_dir, "场景"), (base_dir, "道具"), (deriv_dir, "场景"), (deriv_dir, "道具")]
    )
    summary = {
        "分集引用未建档": ref_missing,
        "缺基础提示词": base_missing,
        "缺衍生提示词": deriv_missing,
        "内容不合规": [format_content_bad_line(fname, m) for fname, t, m in content_bad],
        "场景层次雷同": scene_dup_issues,
        "内容整节雷同": section_dup_issues,
        "强调清单": emphasis_issues,
        "启动状态": state_issues,
        "合计缺失": len(ref_missing) + len(base_missing) + len(deriv_missing) + len(content_bad) + len(scene_dup_issues) + len(section_dup_issues) + len(emphasis_issues) + len(state_issues),
    }
    # 合法排除**不计入**合计缺失，但必须留痕：放行了什么、按什么理由放行（工单 F7）
    if ref_non_asset_skips:
        summary["分集引用·已判定非资产"] = ref_non_asset_skips
    if args.style_lib:
        sys.path.insert(0, str(SKILL_DIR / "scripts"))
        try:
            from check_style_lib import run_checks
            s_errors, s_warns, s_infos, _, _ = run_checks()
            if s_errors:
                summary["画风库问题"] = s_errors
                summary["合计缺失"] += len(s_errors)
            elif s_warns:
                summary.setdefault("画风库提示", s_warns)
        except Exception as e:
            summary["画风库提示"] = [f"自检异常: {e}"]
    if args.json:
        import json as _json
        print(_json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if not summary["合计缺失"] else 1

    print("🔍 调度完整性检查")
    print(
        f"  资产表: 人物 {len(assets.get('人物', []))} / "
        f"场景 {len(assets.get('场景', []))} / "
        f"道具 {len(assets.get('道具', []))} / "
        f"服装 {len(assets.get('服装', []))}"
    )
    print_items = [
        ("分集引用未建档", ref_missing),
        ("缺基础提示词", base_missing),
        ("缺衍生提示词", deriv_missing),
        ("内容不合规", summary["内容不合规"]),
        ("场景层次雷同", scene_dup_issues),
        ("内容整节雷同", section_dup_issues),
        ("强调清单", summary["强调清单"]),
        ("启动状态", summary["启动状态"]),
    ]
    if "画风库问题" in summary:
        print_items.append(("画风库问题", summary["画风库问题"]))
    if "画风库提示" in summary:
        print_items.append(("画风库提示", summary["画风库提示"]))
    for k, items in print_items:
        if items:
            print(f"  ❌ {k} ({len(items)} 项):")
            for it in items[:10]:
                print(f"     - {it}")
            if len(items) > 10:
                print(f"     ... 等 {len(items)} 项")
        else:
            print(f"  ✅ {k}: 无")
    # 合法排除单独一行、用 ℹ️ 不用 ❌：放行是有理由的，但必须看得见（工单 F7）
    for it in summary.get("分集引用·已判定非资产") or []:
        print(f"  ℹ️  分集引用·已判定非资产（合法排除）：{it}")
    if not summary["合计缺失"]:
        print("✅ 全部通过")
        return 0
    print(f"⚠️  共 {summary['合计缺失']} 项——缺失项运行 generate_prompts.py 补齐（--force 覆盖不合规文件）或人工处理")
    return 1


if __name__ == "__main__":
    sys.exit(main())
