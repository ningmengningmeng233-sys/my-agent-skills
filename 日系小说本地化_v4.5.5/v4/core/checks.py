"""事实与交付硬门槛 + 事件级对照 + 风格软评估（v4 spec §9/§10/§11.1）。

产物 ``chapters/<id>/checks.json`` 契约见 ``docs/v4-artifact-schemas.json`` §checks。

- 正文纯度：output.md 只允许章节标题与正文，禁止计划/质检/映射/状态/审计元数据
  （R04 / F-001 / F-002 / C-007 / C-083）；
- 分段 ID：基于正文规范化分段与顺序生成稳定 ``p-NNNN`` 段落 ID，仅作检查证据，不写入正文
  （spec §9.2）；
- 事件级对照：每个事件必须关联段落 ID 与证据，证据不得只写「见正文」（R11 / F-014）；
- 必替换残留分级：must_replace 未决残留 fail；功能适配未决 blocked（C 档）；
  用户保留决策覆盖后不误报失败但可追溯（R05 / C-049 / C-050）；
- 篇幅 warning：比例只作异常提示，不证明事实完整、不单独否决合法表达（R13 / C-047 / C-048）；
- 写作指导六项一致性旁车字段：剧情逻辑/人物目标/情绪关系/身体信息/场景转场/章末承接，
  默认 not_run（写作指导未启用）；启用后按 Enhance 只读结果记录，
  ≥2 项不稳时按写作指导规则 blocked。

结果语义：pass / blocked / fail / warning / not_run。
「不确定」只能 blocked，绝不自动写成 pass。
"""

from __future__ import annotations

import difflib
import json
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from . import content_coverage, decisions, events, fingerprints
from . import localization_tone
from . import long_novel_checks
from . import prompt_assets
from .audit import append_audit, now_iso
from .hashing import sha256_bytes, sha256_file, sha256_json
from .jsonio import read_json, write_json_atomic
from .manifest import ManifestStore
from .paths import RULES_PATH
from .schemas import SchemaError

ResultType = Literal["pass", "blocked", "fail", "warning", "not_run"]

# 写作指导六项一致性（structural-writing-checks.json consistency_review）
WRITING_GUIDE_DIMENSIONS = (
    "剧情逻辑",
    "人物目标",
    "情绪关系",
    "身体信息",
    "场景转场",
    "章末承接",
)

# 正文纯度：禁止出现在 output.md 的内部元数据 / 计划 / 质检标记
_FORBIDDEN_HEADING_KEYWORDS = (
    "写前计划", "本批爽点对照", "爽点清单", "质检", "检查结果", "映射说明",
    "状态", "审批", "审计", "盲审", "决策记录", "事件对照", "软评估",
)
_FORBIDDEN_INLINE = (
    "写前计划", "爽点清单", "质检结果", "映射说明", "盲审结论", "审计日志",
    "schema_version", "manifest_revision", "project_id", "状态字段", "审批通过",
)
_TABLE_SEPARATOR_RE = re.compile(r"^\|[-:\s|]+\|$")
_HEADING_RE = re.compile(r"^#{1,6}\s+\S")

_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")

# 篇幅比正常区间（C-047）
LENGTH_RATIO_MIN = 0.8
LENGTH_RATIO_MAX = 1.2


@dataclass
class CheckResult:
    name: str
    result: ResultType
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "result": self.result, "message": self.message}


# ── 分段 ID ────────────────────────────────────────────────────────────


def normalize_paragraphs(text: str) -> list[str]:
    """按空行把正文规范化为非空段落列表（确定性顺序）。"""
    normalized = text.replace("\r\n", "\n")
    blocks = _PARAGRAPH_SPLIT_RE.split(normalized)
    paragraphs = [re.sub(r"[ \t]+", " ", block.strip()) for block in blocks]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs and text.strip():
        return [text.strip()]
    return paragraphs


def paragraph_ids(text: str) -> list[tuple[str, str]]:
    """返回 [(段落 ID, 段落文本), ...]；ID 为 ``p-NNNN``，不写入展示正文。"""
    return [
        (f"p-{index:04d}", paragraph)
        for index, paragraph in enumerate(normalize_paragraphs(text), start=1)
    ]


# ── 正文纯度 ────────────────────────────────────────────────────────────


def purity_issues(output_text: str) -> list[str]:
    """返回正文纯度问题列表（空 = 纯正文）。output.md 只允许章节标题+正文。"""
    issues: list[str] = []
    if not isinstance(output_text, str):
        return ["output 不是文本"]
    if output_text.startswith("---"):
        issues.append("正文以 frontmatter 元数据开头")
    lines = output_text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if _HEADING_RE.match(stripped):
            if index == 0 and len(lines) > 1:
                # 首行允许章节标题（仅当其后还有正文，且标题不携带禁用词）
                if any(
                    keyword in stripped for keyword in _FORBIDDEN_HEADING_KEYWORDS
                ):
                    issues.append(f"第 {index + 1} 行章节标题携带禁用词: {stripped[:40]}")
                continue
            issues.append(f"第 {index + 1} 行出现正文之外的标题/小节: {stripped[:40]}")
        if _TABLE_SEPARATOR_RE.match(stripped):
            issues.append(f"第 {index + 1} 行疑似质检表分隔行: {stripped[:40]}")
        for marker in _FORBIDDEN_INLINE:
            if marker in stripped:
                issues.append(f"第 {index + 1} 行出现内部元数据/质检标记「{marker}」")
                break
    return issues


def purity_check(output_text: str) -> CheckResult:
    issues = purity_issues(output_text)
    if issues:
        return CheckResult(
            "output_purity",
            "fail",
            "正文纯度失败：" + "；".join(issues[:5]),
        )
    return CheckResult(
        "output_purity",
        "pass",
        "正文仅含章节标题与正文，无计划/质检/状态/审计元数据",
    )


# ── 必替换残留分级 ──────────────────────────────────────────────────────


def load_rules_tiers() -> dict[str, list[str]]:
    """读取 ``v4/rules/dictionary.json`` 的三级策略表。"""
    doc = read_json(RULES_PATH)
    return doc.get("tiers", {})


def _covered_by_decisions(token: str, accepted_entries: list[dict[str, Any]]) -> list[str]:
    """返回覆盖该残留词的已接受决策 ID 列表。"""
    return [
        entry["decision_id"]
        for entry in accepted_entries
        if token in (entry.get("target") or "")
    ]


def _style_normalize_for_paragraph(
    paragraph: str,
    terms: set[str],
) -> str:
    """把一段文字压成用于“是否原句照抄”的指纹：去空白/去映射词/去标点。"""
    value = paragraph.strip()
    for term in sorted(terms, key=len, reverse=True):
        value = value.replace(term, "X")
    value = re.sub(r"[喔耶喽呐啦嘛呢的说呀]+", "", value)
    value = re.sub(r"[，。！？；：、…—\-\"\"''「」『』【】（）()\[\]]", "", value)
    return re.sub(r"\s+", "", value)


def unchanged_paragraph_ratio(
    source_text: str,
    output_text: str,
    *,
    rules_tiers: dict[str, list[str]] | None = None,
    project_decisions: dict[str, Any] | None = None,
    chapter_decisions: dict[str, Any] | None = None,
) -> float:
    """统计输出中有多少段与源稿“压掉映射词/标点后逐字相同”。

    用于拦截“同一章里有的段改得很活，有的段一字未改”的不均匀问题。
    """
    terms: set[str] = set()
    for tier in ("must_replace", "functional_adaptation"):
        for entry in (rules_tiers or {}).get(tier, []):
            if isinstance(entry, dict):
                for key in ("source", "target"):
                    value = entry.get(key)
                    if isinstance(value, str) and len(value) > 1:
                        terms.add(value)
    for doc in (project_decisions or {}, chapter_decisions or {}):
        for entry in doc.get("entries", []):
            if not isinstance(entry, dict) or entry.get("status") != "accepted":
                continue
            for key in ("target", "choice"):
                value = entry.get(key)
                if isinstance(value, str) and len(value) > 1:
                    terms.add(value)

    source_paragraphs = [
        re.sub(r"\s+", "", p)
        for p in re.split(r"\n\s*\n", source_text.replace("\r\n", "\n"))
        if p.strip()
    ]
    source_fingerprints = {
        _style_normalize_for_paragraph(p, terms) for p in source_paragraphs
    }
    output_paragraphs = [
        p
        for p in re.split(r"\n\s*\n", output_text.replace("\r\n", "\n"))
        if p.strip()
    ]
    if not output_paragraphs:
        return 1.0
    unchanged = sum(
        1
        for p in output_paragraphs
        if _style_normalize_for_paragraph(p, terms) in source_fingerprints
    )
    return unchanged / len(output_paragraphs)


def unchanged_paragraph_indexes(
    source_text: str,
    output_text: str,
    *,
    rules_tiers: dict[str, list[str]] | None = None,
    project_decisions: dict[str, Any] | None = None,
    chapter_decisions: dict[str, Any] | None = None,
) -> list[int]:
    """返回输出中与源稿“压掉映射词/标点后逐字相同”的段落下标（供定向返修）。"""
    terms: set[str] = set()
    for tier in ("must_replace", "functional_adaptation"):
        for entry in (rules_tiers or {}).get(tier, []):
            if isinstance(entry, dict):
                for key in ("source", "target"):
                    value = entry.get(key)
                    if isinstance(value, str) and len(value) > 1:
                        terms.add(value)
    for doc in (project_decisions or {}, chapter_decisions or {}):
        for entry in doc.get("entries", []):
            if not isinstance(entry, dict) or entry.get("status") != "accepted":
                continue
            for key in ("target", "choice"):
                value = entry.get(key)
                if isinstance(value, str) and len(value) > 1:
                    terms.add(value)

    source_paragraphs = [
        p for p in re.split(r"\n\s*\n", source_text.replace("\r\n", "\n")) if p.strip()
    ]
    source_fingerprints = {
        _style_normalize_for_paragraph(p, terms) for p in source_paragraphs
    }
    output_paragraphs = [
        p for p in re.split(r"\n\s*\n", output_text.replace("\r\n", "\n")) if p.strip()
    ]
    return [
        index for index, p in enumerate(output_paragraphs)
        if _style_normalize_for_paragraph(p, terms) in source_fingerprints
    ]


def unchanged_paragraph_check(ratio: float, *, required: bool) -> CheckResult:
    if not required:
        return CheckResult(
            "unchanged_paragraphs",
            "not_run",
            "未重写段检查未启用（placeholder 后端或旧工作区）",
        )
    if ratio >= 0.35:
        return CheckResult(
            "unchanged_paragraphs",
            "fail",
            f"未重写段占比 {ratio:.1%}，超过 35%：大量段落仍照抄原文，必须整章重写",
        )
    if ratio >= 0.20:
        return CheckResult(
            "unchanged_paragraphs",
            "blocked",
            f"未重写段占比 {ratio:.1%}，超过 20%：改写深度不均匀，需优先返修原句段",
        )
    if ratio >= 0.10:
        return CheckResult(
            "unchanged_paragraphs",
            "warning",
            f"未重写段占比 {ratio:.1%}，建议检查是否还有漏网原句",
        )
    return CheckResult(
        "unchanged_paragraphs",
        "pass",
        f"未重写段占比 {ratio:.1%}，改写深度均匀",
    )


def style_depth_ratio(
    source_text: str,
    output_text: str,
    *,
    rules_tiers: dict[str, list[str]] | None = None,
    project_decisions: dict[str, Any] | None = None,
    chapter_decisions: dict[str, Any] | None = None,
    chapter_title: str = "",
) -> float:
    """计算「去映射词+去语气词+去标点」后的源稿/成品相似度。

    相似度越高，说明改写越浅：只换译名、删台版语气词，句式仍是原文。
    v4.3.0 起作为改写深度门：placeholder 不启用；LLM 后端默认启用。
    """
    terms: set[str] = set()
    for tier in ("must_replace", "functional_adaptation"):
        for entry in (rules_tiers or {}).get(tier, []):
            if isinstance(entry, dict):
                for key in ("source", "target"):
                    value = entry.get(key)
                    if isinstance(value, str) and len(value) > 1:
                        terms.add(value)
    for doc in (project_decisions or {}, chapter_decisions or {}):
        for entry in doc.get("entries", []):
            if not isinstance(entry, dict) or entry.get("status") != "accepted":
                continue
            for key in ("target", "choice"):
                value = entry.get(key)
                if isinstance(value, str) and len(value) > 1:
                    terms.add(value)

    def normalize(raw: str) -> str:
        paragraphs = re.split(r"\n\s*\n", raw.replace("\r\n", "\n"))
        paragraphs = [part.strip() for part in paragraphs if part.strip()]
        if chapter_title and paragraphs and paragraphs[0].strip() == chapter_title.strip():
            paragraphs = paragraphs[1:]
        value = "".join(paragraphs)
        for term in sorted(terms, key=len, reverse=True):
            value = value.replace(term, "X")
        value = re.sub(r"[喔耶喽呐啦嘛呢的说呀]+", "", value)
        value = re.sub(r"[，。！？；：、…—\-\"\"''「」『』【】（）()\[\]]", "", value)
        return value

    a, b = normalize(source_text), normalize(output_text)
    if not a or not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def style_depth_check(ratio: float, *, required: bool) -> CheckResult:
    """改写深度门：只换词/只删语气词的浅改写会被拦下。"""
    if not required:
        return CheckResult(
            "style_depth",
            "not_run",
            "改写深度门未启用（placeholder 后端或旧工作区；LLM 后端默认启用）",
        )
    if ratio >= 0.96:
        return CheckResult(
            "style_depth",
            "fail",
            f"改写深度不足：去映射词后与原文相似度 {ratio:.3f}，基本只是词语替换/删语气词，句式未重写",
        )
    if ratio >= 0.90:
        return CheckResult(
            "style_depth",
            "blocked",
            f"改写深度偏低：去映射词后与原文相似度 {ratio:.3f}，仍接近翻译稿，需按网文感重写句式",
        )
    if ratio >= 0.80:
        return CheckResult(
            "style_depth",
            "warning",
            f"改写深度一般：去映射词后相似度 {ratio:.3f}，建议检查是否还有翻译腔句架",
        )
    return CheckResult(
        "style_depth",
        "pass",
        f"改写深度合格：去映射词后相似度 {ratio:.3f}，句式已发生实质重写",
    )


# ── 节奏（质感）门 ──────────────────────────────────────────────────────
#
# 为什么需要这道门：2026-09-17 实测发现，改写器是**均匀化算子**——全章 301 段
# 尺度上与源稿相比，句长 20.3→13.8、句长标准差 12.1→7.3、段长 33.4→25.8、
# 长段(≥100字)占比 4.3%→0.3%。即：源稿本来有的起伏被磨平了。
# 网文质感的大头就是长短交替的起伏（龙族·第三幕 句长标准差 27.0、长段 22.1%）。
#
# 前车之鉴：节奏此前只写在 prompt 里（「不得为了简洁而牺牲描写」），而该句
# 从未生效——没有门守着的东西一定会被磨掉，因为磨掉最省力。
#
# 绝对下限取自三份实测语料的下包络（源稿台版 / 龙族·第三幕 / 全职法师），
# 门只拦「比源稿更平」，不拦源稿本身。
TEXTURE_SENTENCE_MEAN = 18.0
TEXTURE_SENTENCE_STDEV = 11.0
TEXTURE_PARAGRAPH_MEAN = 32.0
TEXTURE_PARAGRAPH_STDEV = 25.0
TEXTURE_LONG_PARAGRAPH_MIN_RATIO = 0.035
TEXTURE_SHORT_PARAGRAPH_MAX_RATIO = 0.35
TEXTURE_LONG_PARAGRAPH_MIN_CHARS = 100
TEXTURE_SHORT_PARAGRAPH_MAX_CHARS = 15

_SENTENCE_END_RE = re.compile(r"[。！？!?…]")


@dataclass
class TextureMetrics:
    """节奏指纹：段落/句子的长度均值与离散度。"""

    sentence_mean: float
    sentence_stdev: float
    paragraph_mean: float
    paragraph_stdev: float
    long_paragraph_ratio: float
    short_paragraph_ratio: float

    def to_dict(self) -> dict[str, float]:
        return {
            "sentence_mean": round(self.sentence_mean, 2),
            "sentence_stdev": round(self.sentence_stdev, 2),
            "paragraph_mean": round(self.paragraph_mean, 2),
            "paragraph_stdev": round(self.paragraph_stdev, 2),
            "long_paragraph_ratio": round(self.long_paragraph_ratio, 4),
            "short_paragraph_ratio": round(self.short_paragraph_ratio, 4),
        }


def texture_metrics(text: str) -> TextureMetrics:
    """计算节奏指纹（确定性，无 LLM、无阈值判断）。"""
    paragraphs = normalize_paragraphs(text or "")
    paragraph_lengths = [len(p) for p in paragraphs] or [0]
    sentences = [
        s.strip() for s in _SENTENCE_END_RE.split(text or "") if s.strip()
    ] or ([text.strip()] if (text or "").strip() else [""])
    sentence_lengths = [len(s) for s in sentences] or [0]
    count = len(paragraph_lengths)
    return TextureMetrics(
        sentence_mean=statistics.mean(sentence_lengths),
        sentence_stdev=(
            statistics.pstdev(sentence_lengths) if len(sentence_lengths) > 1 else 0.0
        ),
        paragraph_mean=statistics.mean(paragraph_lengths),
        paragraph_stdev=(
            statistics.pstdev(paragraph_lengths) if len(paragraph_lengths) > 1 else 0.0
        ),
        long_paragraph_ratio=sum(
            1 for p in paragraph_lengths if p >= TEXTURE_LONG_PARAGRAPH_MIN_CHARS
        )
        / count,
        short_paragraph_ratio=sum(
            1 for p in paragraph_lengths if p <= TEXTURE_SHORT_PARAGRAPH_MAX_CHARS
        )
        / count,
    )


def _relaxed_floor(floor: float, source_value: float | None) -> float:
    """源稿本身低于绝对下限时，下限按 0.9×源值放宽（避免本就平的源稿误报）。"""
    if source_value is None:
        return floor
    return min(floor, source_value * 0.9)


def _relaxed_ceiling(ceiling: float, source_value: float | None) -> float:
    """源稿本身就高于绝对上限时，上限按 1.1×源值放宽。"""
    if source_value is None:
        return ceiling
    return max(ceiling, source_value * 1.1)


def texture_check(
    metrics: TextureMetrics,
    source_metrics: TextureMetrics | None = None,
    *,
    required: bool = True,
) -> CheckResult:
    """节奏门：拦下「比源稿更平」的成品。

    分级按违反项数：0 项 pass；1~2 项 warning；3~4 项 blocked；≥5 项 fail。
    单项违规不足以否决合法改写（例如全对话章天然短段多），多点同时塌陷才判失败。
    """
    if not required:
        return CheckResult(
            "texture",
            "not_run",
            "节奏门未启用（placeholder 后端或旧工作区；LLM 后端默认启用）",
        )
    src = source_metrics
    violations: list[str] = []

    floor = _relaxed_floor(TEXTURE_SENTENCE_MEAN, src.sentence_mean if src else None)
    if metrics.sentence_mean < floor:
        violations.append(
            f"句长均 {metrics.sentence_mean:.1f} < {floor:.1f}（句子被磨成一律的短句）"
        )

    floor = _relaxed_floor(TEXTURE_SENTENCE_STDEV, src.sentence_stdev if src else None)
    if metrics.sentence_stdev < floor:
        violations.append(
            f"句长标准差 {metrics.sentence_stdev:.1f} < {floor:.1f}（句子长短没有起伏）"
        )

    floor = _relaxed_floor(TEXTURE_PARAGRAPH_MEAN, src.paragraph_mean if src else None)
    if metrics.paragraph_mean < floor:
        violations.append(
            f"段长均 {metrics.paragraph_mean:.1f} < {floor:.1f}（段落被压薄）"
        )

    floor = _relaxed_floor(TEXTURE_PARAGRAPH_STDEV, src.paragraph_stdev if src else None)
    if metrics.paragraph_stdev < floor:
        violations.append(
            f"段长标准差 {metrics.paragraph_stdev:.1f} < {floor:.1f}（段落长短没有起伏）"
        )

    floor = _relaxed_floor(
        TEXTURE_LONG_PARAGRAPH_MIN_RATIO,
        src.long_paragraph_ratio if src else None,
    )
    if metrics.long_paragraph_ratio < floor:
        violations.append(
            f"长段(≥{TEXTURE_LONG_PARAGRAPH_MIN_CHARS}字)占比 "
            f"{metrics.long_paragraph_ratio:.1%} < {floor:.1%}（没有舒展的长段）"
        )

    ceiling = _relaxed_ceiling(
        TEXTURE_SHORT_PARAGRAPH_MAX_RATIO,
        src.short_paragraph_ratio if src else None,
    )
    if metrics.short_paragraph_ratio > ceiling:
        violations.append(
            f"短段(≤{TEXTURE_SHORT_PARAGRAPH_MAX_CHARS}字)占比 "
            f"{metrics.short_paragraph_ratio:.1%} > {ceiling:.1%}（碎段过多）"
        )

    fingerprint = (
        f"句长 {metrics.sentence_mean:.1f}±{metrics.sentence_stdev:.1f}，"
        f"段长 {metrics.paragraph_mean:.1f}±{metrics.paragraph_stdev:.1f}，"
        f"长段 {metrics.long_paragraph_ratio:.1%} / 短段 {metrics.short_paragraph_ratio:.1%}"
    )
    if not violations:
        return CheckResult("texture", "pass", f"节奏合格：{fingerprint}")
    detail = "；".join(violations[:5])
    if len(violations) >= 5:
        return CheckResult(
            "texture",
            "fail",
            f"节奏塌陷（{len(violations)} 项不达标）：{detail}",
        )
    if len(violations) >= 3:
        return CheckResult(
            "texture",
            "blocked",
            f"节奏偏平（{len(violations)} 项不达标）：{detail}",
        )
    return CheckResult(
        "texture",
        "warning",
        f"节奏略平（{len(violations)} 项不达标）：{detail}",
    )


def _tier_token_source(token: Any) -> str | None:
    """统一把字符串 token 或 {source,target,...} 对象解析为用于匹配的源词。"""
    if isinstance(token, str):
        return token
    if isinstance(token, dict):
        return token.get("source") or None
    return None


def _is_identity_token(token: Any) -> bool:
    """自身映射（source == target）：替换结果就是源词，约束永不可满足。

    与 ``long_novel_checks.terminology_gate`` 对同名映射的处理保持一致：
    跳过而不报残留，否则会把正确译文判为失败。保留类意图请用 default_keep
    或用户决策承载。
    """
    if not isinstance(token, dict):
        return False
    source = token.get("source")
    target = token.get("target")
    return bool(source) and source == target


# 单字必替换词在常见中文词中出现时不应判为日文残留（C-049 语境豁免）。
_SINGLE_CHAR_COMPOUND_EXCEPTIONS: dict[str, tuple[str, ...]] = {
    "君": (
        "君子", "君主", "君临", "郎君", "夫君", "帝君", "魔君", "星君",
        "仙君", "神君", "真君", "国君", "君权", "君臣", "君上", "府君",
        "储君", "暴君", "昏君", "明君", "君王", "主君", "君士坦丁",
    ),
    "桑": ("桑叶", "桑树", "桑拿", "沧桑", "扶桑", "桑田"),
    "酱": ("酱油", "酱料", "酱汁", "果酱", "炸酱", "豆瓣酱", "芝麻酱"),
}


_MULTI_CHAR_COMPOUND_EXCEPTIONS: dict[str, tuple[str, ...]] = {
    "软体": ("软体动物",),
    "土司": ("土司城", "土司府", "土司官寨", "土司制度", "土司王"),
    "计画": ("计划",),
}


def compound_exceptions(source: str) -> tuple[str, ...]:
    """返回单字与多字源词的全部复合词豁免表。"""
    return _SINGLE_CHAR_COMPOUND_EXCEPTIONS.get(source, ()) + _MULTI_CHAR_COMPOUND_EXCEPTIONS.get(source, ())


def has_residue(source: str, text: str) -> bool:
    """公开的残留判断：与 :func:`_has_residue` 同语义。"""
    return _has_residue(source, text)


def _residue_needles(source: str) -> tuple[str, ...]:
    """返回一个词条的**残留检测针**：完整条目 + 长条目的核心词干。

    为什么需要词干：词典声明的是 `圣地林德布尔姆 → 圣辉城`，但译文里源语地名
    可能**不带前缀**单独出现（实测「我站在林德布尔姆的钟塔上」）。只拿完整条目
    去比就查不到 → 残留门放行 → 成品带着未本地化地名交付，并进一步把事件判官
    引到错误段落、触发顺序门 blocked。

    词干取法保守：仅当条目长度 ≥5 时，去掉前 2 字（地名/属性前缀，如「圣地」
    「旧城区」），且剩余长度 ≥3 才作为检测针——避免短词被截成单字到处误报。
    """
    needles = [source]
    if len(source) >= 5:
        stem = source[2:]
        if len(stem) >= 3:
            needles.append(stem)
    return tuple(needles)


def _has_residue(source: str, text: str) -> bool:
    """判断 source 是否作为待替换残留出现在文本中。

    单字与多字词均先掩蔽常见复合词：
    「君」不误报「君子/夫君」；「软体」不误报「软体动物」；「土司」不误报「土司制度」。

    另外，长词条（≥5 字）还要检查其**核心词干**：词典声明 `圣地林德布尔姆 →
    圣辉城`，而译文里源语地名可能不带前缀单独出现（实测漏译处正是「林德布尔姆
    的钟塔」）。只查完整条目会漏检，成品便带着未本地化内容过门。
    """
    if not source:
        return False
    for needle in _residue_needles(source):
        if needle not in text:
            continue
        exceptions = compound_exceptions(needle)
        if not exceptions:
            return True
        masked = text
        for exception in exceptions:
            masked = masked.replace(exception, "Ｘ" * len(exception))
        if needle in masked:
            return True
    return False



def residue_check(
    output_text: str,
    tiers: dict[str, list[str]] | None = None,
    decision_entries: list[dict[str, Any]] | None = None,
) -> tuple[CheckResult, list[CheckResult]]:
    """必替换残留分级检查。

    - must_replace 残留且无用户保留决策 → ``fail``（C-049 硬门槛失败）；
    - functional_adaptation 元素出现但无 C 档决策 → ``blocked``（C-045 / F-015）；
    - 有 accepted 决策覆盖 → 不误报失败，记 ``warning`` 并保留 decision_id（C-050）；
    - default_keep 元素出现属默认保留 → 不阻塞。
    返回 (主结果, 附加 warning 列表)。
    """
    tiers = tiers if tiers is not None else load_rules_tiers()
    accepted = [
        entry
        for entry in (decision_entries or [])
        if isinstance(entry, dict) and entry.get("status") == "accepted"
    ]
    warnings: list[CheckResult] = []
    failures: list[str] = []
    blocks: list[str] = []
    for token in tiers.get("must_replace", []):
        if _is_identity_token(token):
            continue
        source = _tier_token_source(token)
        if source and _has_residue(source, output_text):
            covering = _covered_by_decisions(source, accepted)
            if covering:
                warnings.append(
                    CheckResult(
                        "residue_covered",
                        "warning",
                        f"必替换词「{source}」残留但已有用户保留决策 "
                        f"({'、'.join(covering[:3])})；可追溯，不误报为失败",
                    )
                )
            else:
                failures.append(f"必替换词「{source}」残留且无用户决策")
    for token in tiers.get("functional_adaptation", []):
        source = _tier_token_source(token)
        if source and source in output_text and not _covered_by_decisions(source, accepted):
            blocks.append(f"功能适配元素「{source}」出现但无 C 档决策")
    if failures:
        result = CheckResult("residue_tier", "fail", "；".join(failures[:5]))
    elif blocks:
        result = CheckResult("residue_tier", "blocked", "；".join(blocks[:5]))
    else:
        result = CheckResult(
            "residue_tier",
            "pass",
            "必须替换残留均已处理或由用户决策覆盖；默认保留项未机械中国化",
        )
    return result, warnings


# ── 篇幅 ────────────────────────────────────────────────────────────────


def length_check(source_text: str, output_text: str) -> CheckResult:
    """篇幅比只作异常提示（C-047/C-048），不构成事实通过/否决。"""
    source_len = len(source_text.strip())
    output_len = len(output_text.strip())
    if source_len == 0:
        return CheckResult("length_ratio", "warning", "原文为空，无法计算篇幅比")
    ratio = output_len / source_len
    if LENGTH_RATIO_MIN <= ratio <= LENGTH_RATIO_MAX:
        return CheckResult(
            "length_ratio",
            "pass",
            f"篇幅比 {ratio:.2f}（{int(LENGTH_RATIO_MIN * 100)}%~"
            f"{int(LENGTH_RATIO_MAX * 100)}% 区间内），无异常提示；"
            f"比例本身不构成事实完整证明",
        )
    return CheckResult(
        "length_ratio",
        "warning",
        f"篇幅比 {ratio:.2f} 超出 {int(LENGTH_RATIO_MIN * 100)}%~"
        f"{int(LENGTH_RATIO_MAX * 100)}%，请检查漏写或加戏/重复；"
        f"比例不单独否决合法的中文化表达",
    )


# ── 事件级对照 ──────────────────────────────────────────────────────────


def _split_terms(text: str, min_len: int = 2, max_len: int = 30) -> list[str]:
    parts = re.split(r"[，。！？；、：\s,.;:!?()（）「」『』“”‘’…—\-]+", text)
    return [part for part in parts if min_len <= len(part) <= max_len]


def _locate_event(
    event: dict[str, Any],
    pid_list: list[tuple[str, str]],
    preferred_pid: str | None = None,
) -> list[tuple]:
    """????/??/??????????????

    ``preferred_pid`` ??????????????????/??????
    ??????????????????C-043 ????
    """
    participants = [p for p in event.get("participants", []) if p]
    action_terms = _split_terms(event.get("action", ""))
    result_terms = _split_terms(event.get("result", ""))
    candidates: list[tuple[int, str, str, list[str], list[str], list[str]]] = []
    for pid, paragraph in pid_list:
        hit_p = [p for p in participants if p and p in paragraph]
        hit_a = [term for term in action_terms if term and term in paragraph]
        hit_r = [term for term in result_terms if term and term in paragraph]
        if hit_p or hit_a or hit_r:
            score = len(hit_p) * 3 + len(hit_a) * 2 + len(hit_r)
            if preferred_pid and pid == preferred_pid:
                score += 1000
            candidates.append((score, pid, paragraph, hit_p, hit_a, hit_r))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates


def _evidence_text(candidates: list[tuple]) -> str:
    details: list[str] = []
    for _score, pid, paragraph, hit_p, hit_a, hit_r in candidates[:3]:
        tags: list[str] = []
        if hit_p:
            tags.append("参与者:" + "、".join(hit_p[:3]))
        if hit_a:
            tags.append("动作:" + "、".join(hit_a[:2]))
        if hit_r:
            tags.append("结果:" + "、".join(hit_r[:2]))
        details.append(f"段落 {pid}「{paragraph[:20]}」命中 {' '.join(tags)}")
    return "；".join(details) + "。"


_UNRESOLVED_PARTICIPANTS = frozenset({"待解析角色"})

# 低置信事件的三态记录阈值：仅用于**标注**（写入 evidence 供人工优先复核），
# 不影响门禁判定强度。实测：低置信景物句常常正是译者漏译的句子，
# 因此绝不能因为「抽取器置信度低」就放行整章（见下方 event_comparison_check 注释）。
_LOW_CONFIDENCE_THRESHOLD = 0.5


def _is_structural_event(event: dict[str, Any]) -> bool:
    """事件是否对应非叙事结构段（分隔符/插图标注/标题行）。

    抽取器把这类段落标成 ``_STRUCTURAL_ACTION``；此处据此跳过核验。
    同时兼容历史事件图：老版本没有该标记，但动作本身就是分隔符形状。
    """
    action = str(event.get("action", "") or "").strip()
    if action == events._STRUCTURAL_ACTION:
        return True
    return events.is_structural_paragraph(action)


def _build_event_comparison(
    event: dict[str, Any],
    located: dict[str, tuple],
    prev_pos: int,
    judge: Callable | None,
    judged: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成单事件对照记录。

    ``judged`` 非空表示该事件由判官补位定位（清单 P0 路线 A，跨语言场景）：
    参与者/结果/世界观功能三项由判官裁定为「已被目标语言段落覆盖」，但
    **事件顺序与因果链仍由段落真实位置确定性裁决**，判官不能翻案。
    """
    event_id = event.get("event_id", "")
    candidates = located.get(event_id, (None, -1, []))[2]
    participants = [p for p in event.get("participants", []) if p]
    primary_pos = -1
    if candidates:
        primary_pos = located[event_id][1]
    if not candidates:
        # 非叙事结构段（分隔符/插图标注/纯标题）没有可核验的叙事内容：
        # 判 pass 而非 fail。真实稿实测：把 `■`、`插图p037` 当事件核验时
        # 永远定位不到，白锁整章；跳过它们不影响「叙事事实是否被覆盖」的职责
        # ——那些由正文事件与逐句覆盖门承担。
        if _is_structural_event(event):
            return {
                "event_id": event_id,
                "paragraph_ids": [],
                "participants_consistent": True,
                "order_consistent": True,
                "result_consistent": True,
                "causal_consistent": True,
                "cultural_function_preserved": True,
                "plot_change": False,
                "evidence": "非叙事结构段（分隔符/插图标注/标题行），不参与事件核验",
                "result": "pass",
            }
        # 诊断文案必须同时覆盖两种源语言方向。此处原为「若为日语等跨语言源稿…」，
        # 但本项目正式输入是**中文**台版稿（SKILL.md：不负责日译中），中文稿同样
        # 会因"改写后措辞变化 + 参与者不可解析"而定位失败——旧文案把中文用户
        # 引到日语通道上，属误诊。
        cross_language_hint = (
            "；常见原因：① 该事件参与者未被抽取器解析（显示为「待解析角色」），"
            "可用 --event-engine llm 重抽事件图；② 本地化改写了措辞，确定性短语定位失效，"
            "可启用判官通道（NOVEL_EVENT_JUDGE=1 / NOVEL_LLM_ENABLED）；"
            "③ 源稿确已变更，运行 novel align-events --confirm 重定事件图基准"
        )
        return {
            "event_id": event_id,
            "paragraph_ids": [],
            "participants_consistent": False,
            "order_consistent": False,
            "result_consistent": False,
            "causal_consistent": False,
            "cultural_function_preserved": False,
            "plot_change": False,
            "evidence": (
                f"正文中未定位到事件「{event.get('action', '')}」的参与者"
                f"「{'、'.join(participants)}」对应段落（无段落 ID 证据，F-014）"
                + cross_language_hint
            ),
            "result": "fail",
        }
    union_paras = "".join(item[2] for item in candidates[:3])
    # 未解析哨兵不参与一致性要求：抽取器识别不出主语时写入「待解析角色」，
    # 而不是用 paragraph[:8] 伪造一个源段落子串（那会构造性地必然匹配）。
    # 与「无明确结果」同理——不伪造，也不强求；对照职责由动作/结果承担。
    resolved_participants = [p for p in participants if p not in _UNRESOLVED_PARTICIPANTS]
    participants_consistent = all(p in union_paras for p in resolved_participants)
    order_consistent = primary_pos >= prev_pos
    result_value = event.get("result", "")
    if result_value == "无明确结果":
        # 规则抽取器对单句段落不伪造结果；无结果要求可免于结果定位检查。
        result_consistent = True
    else:
        result_terms = _split_terms(result_value)
        result_consistent = result_value in union_paras or (
            bool(result_terms) and any(term in union_paras for term in result_terms)
        )
    # 因果链：前置事件必须先于当前事件且已定位
    causal_consistent = True
    for prereq in event.get("prerequisites", []):
        prereq_pid, prereq_pos = located.get(prereq, (None, -1))[:2]
        if prereq_pid is None or prereq_pos == -1:
            causal_consistent = False
            break
        if prereq_pos > primary_pos:
            causal_consistent = False
            break
    world_function = event.get("world_function", "无") or "无"
    cultural_function_preserved = True
    if world_function not in {"无", "待确认"}:
        cultural_function_preserved = (world_function in union_paras) or any(
            term in union_paras for term in _split_terms(world_function)
        )
    # 跨语言补位：参与者/结果/世界观功能三项由判官裁定为已覆盖；
    # 顺序与因果链仍由上面的确定性计算裁决，判官不得翻案。
    if judged is not None:
        participants_consistent = True
        result_consistent = True
        cultural_function_preserved = True
    plot_change = False
    result: ResultType = "pass"
    reason = ""
    if not participants_consistent:
        result, reason = "fail", "参与者不一致，事件可能缺失或改换人物"
    elif not order_consistent:
        result, reason = "blocked", "事件顺序疑似改变或定位不唯一，需人工确认（C-043 降级）"
    elif not result_consistent:
        result, reason = "blocked", "事件结果关键词未定位，疑似结果改变，需剧情级变更决策（C-044）"
    elif not causal_consistent:
        result, reason = "blocked", "因果链证据缺失或前置事件顺序反转（F-015）"
    elif not cultural_function_preserved:
        result, reason = "blocked", "世界观功能未被保留（C-045）"
    comparison: dict[str, Any] = {
        "event_id": event_id,
        "paragraph_ids": [item[1] for item in candidates[:3]],
        "participants_consistent": participants_consistent,
        "order_consistent": order_consistent,
        "result_consistent": result_consistent,
        "causal_consistent": causal_consistent,
        "cultural_function_preserved": cultural_function_preserved,
        "plot_change": plot_change,
        "evidence": _evidence_text(candidates) + reason,
        "result": result,
    }
    if judged is not None:
        note = str(judged.get("reason", "")) or "该事件已被目标语言段落覆盖"
        tag = "判官补位（跨语言）"
        comparison["evidence"] = (
            f"{tag}：「{event.get('action', '')}」← {note}；" + comparison["evidence"]
        )
    return comparison


def approved_exemption_ids(
    project_decisions: dict[str, Any] | None,
    chapter_decisions: dict[str, Any] | None,
) -> set[str]:
    """收集「精确指向某个覆盖项」的已批准豁免决策 ID 集合。

    只有显式接受、且带 ``requirement_id`` 指向的决策才算豁免；缺 ID 的一律不认，
    因此 `exempted` 不会因「有条决策」而蒙混通过。
    """
    ids: set[str] = set()
    for doc in (project_decisions or {}, chapter_decisions or {}):
        for entry in (doc.get("entries") or []):
            if not isinstance(entry, dict):
                continue
            if str(entry.get("status") or "") not in {"accepted", "approved"}:
                continue
            rid = entry.get("requirement_id") or entry.get("coverage_requirement_id")
            if rid:
                ids.add(str(rid))
    return ids


def exemplar_copy_check(
    output_text: str,
    exemplars: list[str] | None,
    *,
    min_overlap_chars: int = 12,
) -> dict[str, Any]:
    """文风范例防抄：正文不得与注入的范例连续重合 ≥ ``min_overlap_chars`` 字。

    未注入范例时不适用（返回 ``not_applicable``，不阻断也不报警）——否则离线、
    无索引库等场景会凭空多出一条永不触发的门。
    """
    from .exemplar_guard import copy_report

    rows = [row for row in (exemplars or []) if str(row).strip()]
    if not rows:
        return {
            "result": "not_applicable",
            "checked": False,
            "threshold": min_overlap_chars,
            "exemplar_count": 0,
            "longest_overlap_chars": 0,
            "spans": [],
        }
    report = copy_report(output_text, rows, n=min_overlap_chars)
    return {
        "result": "fail" if report["has_copy"] else "pass",
        "checked": True,
        "threshold": min_overlap_chars,
        "exemplar_count": len(rows),
        "longest_overlap_chars": report["longest_overlap_chars"],
        "spans": report["verbatim_spans"],
    }


def content_coverage_check(
    requirements: list[Any],
    pid_list: list[tuple[str, str]],
    *,
    judge: Callable | None = None,
    approved_exemption_ids: set[str] | None = None,
    required: bool = False,
) -> tuple[list[Any], str, list[dict[str, Any]]]:
    """逐句内容覆盖检查：返回 (findings, 聚合结果, 阻断项)。

    - 判官可用 → 逐句判定，``absent``/``partial`` 判 fail、``uncertain``/漏答 判 blocked；
    - 判官未配置且 ``required=False``（离线/确定性运行）→ ``not_run``，不阻断既有流程；
    - 判官未配置且 ``required=True``（跨语言正式交付）→ ``blocked``（fail-closed：
      内容完整性无人验证时，不得交付）。
    """
    from . import content_coverage as _cc

    exemptions = approved_exemption_ids or set()
    if judge is None and not required:
        return [], "not_run", []
    if judge is None:
        return (
            [],
            "blocked",
            [
                {
                    "requirement_id": "",
                    "status": "uncertain",
                    "evidence": "覆盖判官未配置：无法验证内容完整性，按 blocked 处理（fail-closed）",
                }
            ],
        )
    verdicts: dict[str, Any] = {}
    try:
        verdicts = judge(requirements, pid_list) or {}
    except Exception:  # noqa: BLE001 - 判官故障不得中断 check
        verdicts = {}
    findings = []
    for requirement in requirements:
        verdict = verdicts.get(requirement.id)
        if not isinstance(verdict, dict):
            findings.append(
                _cc.CoverageFinding(
                    requirement_id=requirement.id,
                    status="uncertain",
                    evidence="未取得覆盖判定（判官漏答或不可用）",
                )
            )
            continue
        findings.append(
            _cc.CoverageFinding(
                requirement_id=requirement.id,
                status=str(verdict.get("status", "uncertain")),
                output_paragraph_ids=tuple(verdict.get("paragraph_ids") or ()),
                evidence=str(verdict.get("evidence", "") or ""),
            )
        )
    result, blocking = _cc.aggregate_coverage(
        requirements, findings, approved_exemption_ids=exemptions
    )
    return findings, result, blocking


def _call_event_judge(
    judge: Callable | None,
    events: list[dict[str, Any]],
    pid_list: list[tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    """安全调用判官；任何异常/非法返回都降级为空裁定（绝不崩溃、绝不静默放行）。

    判官协议（v4.5.0-dev.3 跨语言补位）::

        judge(未定位事件列表, 全部段落列表) -> {event_id: {"paragraph_ids": [...], "reason": "..."}}

    只接受真实存在的段落 ID：编造 ID 视为无效裁定，不产生任何放行效果。
    """
    if judge is None or not events:
        return {}
    try:
        verdicts = judge(events, pid_list)
    except Exception:  # noqa: BLE001 - 判官后端不可用时退回确定性判定
        return {}
    if not isinstance(verdicts, dict):
        return {}
    valid_pids = {pid for pid, _para in pid_list}
    cleaned: dict[str, dict[str, Any]] = {}
    for eid, verdict in verdicts.items():
        if not isinstance(verdict, dict):
            continue
        pids = verdict.get("paragraph_ids")
        if not isinstance(pids, list):
            continue
        kept = [p for p in pids if isinstance(p, str) and p in valid_pids]
        if not kept:
            continue
        cleaned[str(eid)] = {
            "paragraph_ids": kept,
            "reason": str(verdict.get("reason", "") or ""),
        }
    return cleaned


def event_comparison_check(
    events_graph: dict[str, Any],
    pid_list: list[tuple[str, str]],
    *,
    judge: Callable | None = None,
) -> tuple[list[dict[str, Any]], ResultType]:
    """为事件图每个事件生成对照记录；整体语义 pass/blocked/fail。

    跨语言（如日语源 → 中文正文）时事件术语与目标正文零交集，确定性定位必然
    失败。此时候选为空的**未定位事件**交给 ``judge`` 补位裁定（清单 P0 路线 A）；
    子串定位成功的事件一律由确定性规则裁决，判官不参与，避免削弱原硬门槛。
    """
    events_sorted = sorted(
        events_graph.get("events", []), key=lambda e: e.get("ordinal", 0)
    )
    pos_map = {pid: index for index, (pid, _para) in enumerate(pid_list)}
    para_map = dict(pid_list)
    located: dict[str, tuple] = {}
    for event in events_sorted:
        eid = event.get("event_id", "")
        ordinal = event.get("ordinal")
        preferred_pid = f"p-{ordinal:04d}" if isinstance(ordinal, int) and ordinal >= 1 else None
        candidates = _locate_event(event, pid_list, preferred_pid)
        if candidates:
            located[eid] = (candidates[0][1], pos_map.get(candidates[0][1], -1), candidates)
        else:
            located[eid] = (None, -1, [])

    # 判官补位：确定性判定「未定位」「参与者对不上」或「结果关键词对不上」的事件。
    # 后两类对应跨语言真实场景——源语言的参与者/结果短语在目标语言正文里自然
    # 找不到：章节标题本地化后标题事件参与者含源语言词；日语结果短语（如
    # 「黒い染みだけが残った」）也不会出现在中文里。
    def _needs_judge(event: dict[str, Any]) -> bool:
        candidates = located.get(event.get("event_id", ""), (None, -1, []))[2]
        if not candidates:
            return True
        union = "".join(item[2] for item in candidates[:3])
        if any(p and p not in union for p in event.get("participants", []) or []):
            return True
        result_value = str(event.get("result", "") or "")
        if result_value in {"", "无明确结果"}:
            return False
        result_terms = _split_terms(result_value)
        return not (
            result_value in union
            or (bool(result_terms) and any(term in union for term in result_terms))
        )

    needs_judge = [event for event in events_sorted if _needs_judge(event)]
    judged: dict[str, dict[str, Any]] = _call_event_judge(judge, needs_judge, pid_list)
    # 判官可能给出多个候选段落（正文里重复出现的分隔符 `■`、重复的寒暄语等）。
    # 逐事件按**事件序**挑选"第一个不早于前序事件位置"的候选，把「定位不唯一」
    # 确定性地收敛为唯一位置；若没有任何候选能满足顺序，则保持不满足、
    # 由顺序门判 blocked——判官依旧不得翻案（顺序仍由确定性规则裁决）。
    judge_paragraphs: dict[str, list[str]] = {}
    for eid, verdict in judged.items():
        pids = sorted(verdict["paragraph_ids"], key=lambda p: pos_map.get(p, -1))
        judge_paragraphs[eid] = [pid for pid in pids if pid in para_map]

    results: list[dict[str, Any]] = []
    prev_pos = -1
    for event in events_sorted:
        eid = event.get("event_id", "")
        ordered_pids = judge_paragraphs.get(eid)
        if ordered_pids:
            chosen = next(
                (pid for pid in ordered_pids if pos_map.get(pid, -1) >= prev_pos),
                ordered_pids[0],
            )
            rest = [pid for pid in ordered_pids if pid != chosen]
            seq = [chosen] + rest
            synthesized = [(0, pid, para_map[pid], [], [], []) for pid in seq]
            located[eid] = (synthesized[0][1], pos_map.get(synthesized[0][1], -1), synthesized)
        comparison = _build_event_comparison(
            event, located, prev_pos, None, judged.get(eid)
        )
        results.append(comparison)
        primary_pid = located.get(eid, (None, -1, []))[0]
        if primary_pid is not None:
            prev_pos = pos_map.get(primary_pid, -1)
    # 低置信事件**不**降级：实测证据（10 章真实批次）——6 个被降级事件里 5 个的
    # 内容在译文中完全不存在、1 个部分存在，即这些「景物句」是译者真的漏译了，
    # 门禁拦截是正确的。曾按「抽取器自己标了低置信就不该否决整章」的推理放宽为
    # warning，结果是把真实内容丢失静默放行（9/10 的通过率有相当部分来自这个放宽）。
    # 结论：门禁强度不随置信度放宽；修正方向是让译者补齐漏译，而不是放松门禁。
    # 标注（不改判定）：低置信事件实测最常是译者漏译的句子，写入证据供人工优先复核。
    confidence_by_id = {
        str(event.get("event_id", "")): event.get("confidence")
        for event in events_sorted
    }
    for record in results:
        try:
            confidence = float(confidence_by_id.get(str(record.get("event_id", ""))))
        except (TypeError, ValueError):
            continue
        if confidence < _LOW_CONFIDENCE_THRESHOLD:
            record["confidence"] = confidence
            record["evidence"] = (
                f"低置信抽取（confidence={confidence:.1f}，参与者不可解析；"
                "实测此类景物/叙述句最常被漏译，请优先人工复核）："
            ) + str(record.get("evidence", ""))

    if any(r["result"] == "fail" for r in results):
        aggregate: ResultType = "fail"
    elif any(r["result"] == "blocked" for r in results):
        aggregate = "blocked"
    else:
        aggregate = "pass"
    return results, aggregate


# ── 写作指导六项一致性旁车字段 ────────────────────────────────────────────


def writing_guide_consistency(
    provider: Callable | None = None,
    *,
    output_text: str = "",
    chapter_source_text: str = "",
) -> list[CheckResult]:
    """写作指导六项一致性旁车字段（Enhance 只读适配器可选注入）。

    默认 not_run（写作指导未启用）；启用后按 provider 返回的
    [(dimension, result, evidence)] 记录，≥2 项不稳时按写作指导硬规则 blocked。
    本项不替代 Core 事实硬门槛（spec §10.2 / §14）。
    """
    if provider is None:
        return [
            CheckResult(
                f"writing_guide_consistency/{dimension}",
                "not_run",
                "写作指导（Enhance 只读适配器）未启用；本项为可选旁车，不参与 Core 硬门槛",
            )
            for dimension in WRITING_GUIDE_DIMENSIONS
        ]
    raw = provider(
        dimensions=WRITING_GUIDE_DIMENSIONS,
        output_text=output_text,
        source_text=chapter_source_text,
    )
    items: list[CheckResult] = []
    unstable = 0
    for row in raw or []:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        dimension, result, evidence = row[0], row[1], row[2]
        if result not in {"pass", "blocked", "fail", "warning", "not_run"}:
            result = "not_run"
        items.append(
            CheckResult(
                f"writing_guide_consistency/{dimension}",
                result,
                evidence or "写作指导侧车无额外证据",
            )
        )
        if result in {"fail", "blocked"}:
            unstable += 1
    if unstable >= 2:
        items.append(
            CheckResult(
                "writing_guide_consistency/aggregate",
                "blocked",
                f"写作指导硬规则「任意两项一致性不稳不得交付」：当前 {unstable} 项不稳",
            )
        )
    elif unstable == 1:
        items.append(
            CheckResult(
                "writing_guide_consistency/aggregate",
                "warning",
                "1 项一致性不稳，需优先修复后再交付",
            )
        )
    return items


# ── 汇总组装 ────────────────────────────────────────────────────────────


def run_checks(
    *,
    chapter_id: str,
    source_text: str,
    output_text: str,
    events_graph: dict[str, Any],
    chapter_title: str = "",
    is_appendix: bool = False,
    style_depth_required: bool = False,
    texture_required: bool | None = None,
    project_decisions: dict[str, Any],
    chapter_decisions: dict[str, Any],
    input_hashes: dict[str, str] | None = None,
    rules_tiers: dict[str, list[str]] | None = None,
    writing_guide_provider: Callable | None = None,
    soft_evaluations: list[dict[str, Any]] | None = None,
    judge: Callable | None = None,
    coverage_judge: Callable | None = None,
    coverage_required: bool = False,
    exemplars: list[str] | None = None,
    generated_at: str | None = None,
    continuity_snapshots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """运行全套检查，返回符合 checks 契约的 checks 文档。

    ``input_hashes`` 为输入指纹（source/events/project_decisions/chapter_decisions/rules），
    用于生成 ``input_fingerprint``；缺省时按内存内容计算（record_checks 会与 manifest 核对）。

    ``texture_required`` 为节奏门开关；``None``（默认）时跟随改写侧是否放开了
    增写通道——门只在工具被允许做好的时候才拦人，否则会把改造前产出的章节
    全部判死，那不公平也修不了。
    """
    generated_at = generated_at or now_iso()
    pid_list = paragraph_ids(output_text)
    if texture_required is None:
        texture_required = prompt_assets.expansion_enabled()

    event_errors: list[str] = []
    try:
        events.validate_event_graph(events_graph)
    except SchemaError as exc:
        event_errors.append(str(exc))

    purity = purity_check(output_text)
    tone, tone_warnings, tone_report = localization_tone.tone_check(output_text)
    hook = localization_tone.hook_signal_check(output_text, is_appendix=is_appendix)
    style_ratio = style_depth_ratio(
        source_text,
        output_text,
        rules_tiers=rules_tiers,
        project_decisions=project_decisions,
        chapter_decisions=chapter_decisions,
        chapter_title=chapter_title,
    )
    style_depth = style_depth_check(style_ratio, required=style_depth_required)
    output_texture = texture_metrics(output_text)
    texture = texture_check(
        output_texture,
        texture_metrics(source_text),
        required=texture_required,
    )
    unchanged_ratio = unchanged_paragraph_ratio(
        source_text,
        output_text,
        rules_tiers=rules_tiers,
        project_decisions=project_decisions,
        chapter_decisions=chapter_decisions,
    )
    unchanged_result = unchanged_paragraph_check(
        unchanged_ratio, required=style_depth_required
    )
    residue, residue_warnings = residue_check(
        output_text,
        rules_tiers,
        list(project_decisions.get("entries", []))
        + list(chapter_decisions.get("entries", [])),
    )
    length = length_check(source_text, output_text)
    dec_result, dec_issues = decisions.closure_status(
        project_decisions, chapter_decisions, events_graph
    )
    if event_errors:
        event_results: list[dict[str, Any]] = []
        event_aggregate: ResultType = "fail"
    else:
        event_results, event_aggregate = event_comparison_check(
            events_graph, pid_list, judge=judge
        )
    wg_items = writing_guide_consistency(
        writing_guide_provider,
        output_text=output_text,
        chapter_source_text=source_text,
    )

    failures: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if event_errors:
        failures.append(
            CheckResult("event_graph", "fail", "事件图校验失败：" + "; ".join(event_errors[:3])).to_dict()
        )
    if purity.result == "fail":
        failures.append(purity.to_dict())
    if residue.result in {"fail", "blocked"}:
        failures.append(residue.to_dict())
    if dec_result in {"fail", "blocked"}:
        failures.append(
            CheckResult("decision_closure", dec_result, "；".join(dec_issues[:5])).to_dict()
        )
    warnings.append(length.to_dict())
    warnings.extend(item.to_dict() for item in residue_warnings)
    if tone.result in {"fail", "blocked"}:
        failures.append(tone.to_dict())
    else:
        warnings.append(tone.to_dict())
    warnings.extend(item.to_dict() for item in tone_warnings)
    warnings.append(hook.to_dict())
    if style_depth.result in {"fail", "blocked"}:
        failures.append(style_depth.to_dict())
    else:
        warnings.append(style_depth.to_dict())
    if texture.result in {"fail", "blocked"}:
        failures.append(texture.to_dict())
    else:
        warnings.append(texture.to_dict())
    if unchanged_result.result in {"fail", "blocked"}:
        failures.append(unchanged_result.to_dict())
    else:
        warnings.append(unchanged_result.to_dict())
    for item in wg_items:
        if item.result in {"fail", "blocked"}:
            failures.append(item.to_dict())
        else:
            warnings.append(item.to_dict())

    # v4.5.0-dev.3（Batch 7）：五项长篇质量门；blocked 级并入 failures（blocker 链路）。
    long_novel_findings = long_novel_checks.evaluate_long_novel_gates(
        output_text=output_text,
        earlier_snapshots=continuity_snapshots or [],
        project_decisions=project_decisions,
        chapter_decisions=chapter_decisions,
        chapter_id=chapter_id,
        events_graph=events_graph,
    )
    for finding in long_novel_findings:
        if finding.severity == "blocked":
            failures.append(
                CheckResult(
                    f"long_novel_{finding.gate}", "blocked", finding.message
                ).to_dict()
            )

    # Task 9.9.3：逐句内容覆盖门。事件图只覆盖被抽取成事件的情节句，景物句/普通句
    # 没有兜底——真实批次里译者漏掉的正是这类句子（ch-0006 末句连续 8 版全漏）。
    # 因此对**每个源句**建必需项；absent/partial → fail，uncertain/漏检 → blocked。
    coverage_requirements = content_coverage.build_coverage_requirements(
        chapter_id=chapter_id, source_text=source_text, events_graph=events_graph
    )
    coverage_findings, coverage_aggregate, coverage_blocking = content_coverage_check(
        coverage_requirements,
        pid_list,
        judge=coverage_judge,
        approved_exemption_ids=approved_exemption_ids(project_decisions, chapter_decisions),
        required=coverage_required,
    )
    if coverage_aggregate == "not_run":
        warnings.append(
            CheckResult(
                "content_coverage",
                "not_run",
                f"{len(coverage_requirements)} 个源句已建清单，但未配置覆盖判官（离线运行）",
            ).to_dict()
        )
    elif coverage_aggregate in {"fail", "blocked"}:
        head = coverage_blocking[:3]
        detail = "；".join(
            f"{item.get('requirement_id')}[{item.get('status')}] {str(item.get('source_text') or '')[:24]}"
            for item in head
        )
        failures.append(
            CheckResult(
                "content_coverage",
                coverage_aggregate,
                f"{len(coverage_blocking)} 个源句未被完整覆盖：{detail}",
            ).to_dict()
        )
    else:
        warnings.append(
            CheckResult(
                "content_coverage", "pass", f"{len(coverage_requirements)} 个源句全部有覆盖判定"
            ).to_dict()
        )

    # 文风范例防抄：范例是"学笔触"的通道，但整句照抄既属抄袭也会把别的小说
    # 的句子搬进成品。只在本次改写确实注入过范例时才有意义。
    exemplar_copy = exemplar_copy_check(output_text, exemplars)
    if exemplar_copy["result"] == "fail":
        failures.append(
            CheckResult(
                "exemplar_copy",
                "fail",
                "正文与文风范例存在连续 {n} 字以上照抄：{detail}".format(
                    n=exemplar_copy["threshold"],
                    detail="；".join(
                        f"「{span['text'][:40]}」({span['chars']}字)"
                        for span in exemplar_copy["spans"][:2]
                    ),
                ),
            ).to_dict()
        )
    elif exemplar_copy["result"] == "pass" and exemplar_copy.get("checked"):
        warnings.append(
            CheckResult(
                "exemplar_copy",
                "pass",
                f"已比对 {exemplar_copy['exemplar_count']} 段范例，最长重合 "
                f"{exemplar_copy['longest_overlap_chars']} 字（阈值 {exemplar_copy['threshold']}）",
            ).to_dict()
        )

    has_fail = any(item["result"] == "fail" for item in failures)
    has_blocked = any(item["result"] == "blocked" for item in failures)
    if event_aggregate == "fail" or has_fail:
        hard_gate: ResultType = "fail"
    elif event_aggregate == "blocked" or has_blocked:
        hard_gate = "blocked"
    else:
        hard_gate = "pass"

    if input_hashes is None:
        input_hashes = {
            "source": events_graph.get("source_sha256") or "0" * 64,
            "events": sha256_json(events_graph),
            "project_decisions": sha256_json(project_decisions),
            "chapter_decisions": sha256_json(chapter_decisions),
            "rules": sha256_json(rules_tiers or {}),
        }
    input_fingerprint = sha256_json(input_hashes)

    return {
        "schema_version": "v4-checks-1",
        "chapter_id": chapter_id,
        "input_fingerprint": input_fingerprint,
        "output_sha256": sha256_bytes(output_text.encode("utf-8")),
        "hard_gate": hard_gate,
        "event_comparison": event_aggregate,
        "event_results": event_results,
        "decision_result": dec_result,
        "purity_result": purity.result,
        "tone_result": tone.result,
        "tone_findings": tone_report.all_findings(40),
        "style_depth_result": style_depth.result,
        "style_depth_ratio": round(style_ratio, 4),
        "texture_result": texture.result,
        "texture_metrics": output_texture.to_dict(),
        "unchanged_paragraph_ratio": round(unchanged_ratio, 4),
        "long_novel_findings": [finding.to_dict() for finding in long_novel_findings],
        "content_coverage": coverage_aggregate,
        "content_coverage_findings": [finding.to_dict() for finding in coverage_findings],
        "content_coverage_blocking": coverage_blocking,
        "content_coverage_summary": {
            "requirements": len(coverage_requirements),
            "blocking": len(coverage_blocking),
            "high_priority": sum(
                1 for r in coverage_requirements if r.manual_review_priority == "high"
            ),
        },
        "exemplar_copy": exemplar_copy["result"],
        "exemplar_copy_findings": exemplar_copy["spans"],
        "exemplar_copy_summary": {
            "checked": exemplar_copy["checked"],
            "exemplar_count": exemplar_copy["exemplar_count"],
            "longest_overlap_chars": exemplar_copy["longest_overlap_chars"],
            "threshold": exemplar_copy["threshold"],
        },
        "warnings": warnings,
        "failures": failures,
        "soft_evaluations": soft_evaluations or [],
        "generated_at": generated_at,
    }


def _latest_version_id(chapter_dir: Path) -> str:
    """读取章节 current.json 的最新版本号；无版本记录返回 v000。"""
    current_path = chapter_dir / "current.json"
    try:
        doc = json.loads(current_path.read_text(encoding="utf-8"))
        return f"v{int(doc.get('latest_version', 0)):03d}"
    except (OSError, ValueError, TypeError):
        return "v000"


def _latest_prompt_run_id(chapter_dir: Path) -> str | None:
    """拾取本章最新一次 PromptManifest 的 prompt_run_id（按修改时间）。"""
    prompts_dir = chapter_dir / "prompts"
    if not prompts_dir.is_dir():
        return None
    candidates = sorted(prompts_dir.glob("pr-*.json"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        return None
    try:
        doc = json.loads(candidates[-1].read_text(encoding="utf-8"))
        run_id = doc.get("prompt_run_id")
        return str(run_id) if run_id else None
    except (OSError, ValueError):
        return None


def record_checks(workspace: Path, chapter_id: str, checks_doc: dict[str, Any]) -> dict[str, Any]:
    """原子写 checks.json 与版本绑定报告 checks/vNNN.json，更新 manifest 摘要并审计。

    - ``checks.json`` 是「最新一次检查」的派生视图；
    - ``chapters/<id>/checks/vNNN.json`` 是与不可变版本强绑定的权威报告，
      记录 ``version`` / ``output_sha256`` / ``prompt_run_id`` / ``ruleset_sha256``，
      供 ``approve`` 前置核验（版本正文哈希必须与报告一致）。
    若 ``checks.input_fingerprint`` 与 manifest 记录的五项输入指纹不一致则抛
    :class:`SchemaError`（防止用旧输入为新正文背书）。
    """
    workspace = Path(workspace)
    store = ManifestStore(workspace)
    manifest = store.load()
    chapter = next((item for item in manifest["chapters"] if item["id"] == chapter_id), None)
    if chapter is None:
        raise KeyError(f"章节不存在: {chapter_id}")
    # 先核对输入指纹，避免用旧输入为新正文背书；不一致时不得写入 checks.json
    recorded = chapter.get("fingerprints", {})
    expected = sha256_json(
        {
            key: recorded.get(key, "0" * 64)
            for key in ("source", "events", "project_decisions", "chapter_decisions", "rules")
        }
    )
    if expected != checks_doc["input_fingerprint"]:
        raise SchemaError(
            f"checks.input_fingerprint 与 manifest 指纹记录不一致（{expected[:12]}… vs "
            f"{checks_doc['input_fingerprint'][:12]}…），请先同步输入指纹再记录检查"
        )
    checks_path = workspace / chapter["paths"]["checks"]
    write_json_atomic(checks_path, checks_doc)

    # 版本绑定报告：checks/vNNN.json（approve 核验的唯一依据）
    chapter_dir = (workspace / chapter["paths"]["output"]).parent
    version_id = _latest_version_id(chapter_dir)
    binding_doc = {
        **checks_doc,
        "version": version_id,
        "output_sha256": checks_doc.get("output_sha256"),
        "prompt_run_id": _latest_prompt_run_id(chapter_dir),
        "ruleset_sha256": fingerprints.rules_fingerprint(),
    }
    write_json_atomic(chapter_dir / "checks" / f"{version_id}.json", binding_doc)

    warning_count = sum(1 for item in checks_doc["warnings"] if item["result"] == "warning")
    failure_count = len(checks_doc["failures"])

    def mutate(updated: dict[str, Any]) -> None:
        current = next(item for item in updated["chapters"] if item["id"] == chapter_id)
        current["checks"] = {
            "hard_gate": checks_doc["hard_gate"],
            "event_comparison": checks_doc["event_comparison"],
            "last_run": checks_doc["generated_at"],
            "warning_count": warning_count,
            "failure_count": failure_count,
        }
        current["fingerprints"]["checks"] = sha256_file(checks_path)
        fingerprint_values = {
            key: current["fingerprints"].get(key, "0" * 64)
            for key in ("source", "events", "project_decisions", "chapter_decisions", "rules", "output", "checks")
        }
        current["fingerprints"]["effective"] = sha256_json(fingerprint_values)
        current["updated_at"] = now_iso()

    updated = store.update(mutate, event="checks_recorded")
    append_audit(
        store.audit_path,
        "checks_detail",
        chapter_id=chapter_id,
        hard_gate=checks_doc["hard_gate"],
        event_comparison=checks_doc["event_comparison"],
        warning_count=warning_count,
        failure_count=failure_count,
        manifest_revision=updated["manifest_revision"],
    )
    return updated


def derive_blockers(checks_doc: dict[str, Any]) -> list[dict[str, Any]]:
    """从 checks 文档的失败/阻塞项生成 manifest blockers 列表。"""
    blockers: list[dict[str, Any]] = []
    for failure in checks_doc.get("failures", []):
        name = failure.get("name", "UNKNOWN")
        result = failure.get("result", "fail")
        code = f"{name.upper().replace('/', '_')}_{result.upper()}"
        blockers.append(
            {
                "code": code,
                "kind": "check",
                "message": failure.get("message", ""),
            }
        )
    for event in checks_doc.get("event_results", []):
        result = event.get("result")
        if result in {"fail", "blocked"}:
            event_id = event.get("event_id", "UNKNOWN")
            blockers.append(
                {
                    "code": f"EVENT_COMPARISON_{result.upper()}",
                    "kind": "check",
                    "message": f"事件 {event_id}: {event.get('evidence', '')}",
                }
            )
    return blockers


def next_state_after_checks(
    checks_doc: dict[str, Any], manifest: dict[str, Any]
) -> str:
    """依据 checks 文档给出建议下一状态（调用方按状态机迁移）。

    - hard_gate 或 event_comparison 为 fail/blocked → ``rework``；
    - 启用盲审且要求通过 → ``awaiting_review``；
    - 否则 → ``passed``。
    """
    if checks_doc["hard_gate"] in {"fail", "blocked"}:
        return "rework"
    if checks_doc["event_comparison"] in {"fail", "blocked"}:
        return "rework"
    enhancements = manifest.get("enhancements", {})
    blind = enhancements.get("blind_review", {})
    if blind.get("enabled") and blind.get("required_for_pass"):
        return "awaiting_review"
    platform = enhancements.get("platform_review", {})
    if platform.get("enabled") and platform.get("platforms"):
        return "awaiting_review"
    return "passed"
