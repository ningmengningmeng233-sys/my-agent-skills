#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目美术总则与确定性规则优先级（v2.17.0 工单 5，设计方案 §7）。

- **唯一权威**：一个项目只有一份 `.process/项目美术总则.json`，锁定媒介、写实程度、人物比例、
  材质、色彩、光线、时代、场景/服装审美与禁止偏移方向，并给出内容指纹供依赖失效判定。
- **规则优先级**固定：`用户明确要求 > Omni标准 > 原文事实 > 项目美术总则 > 唯一主风格包 >
  类型模板 > 必要补全`。下游层永远不得覆盖上游层的确定值。
- **冲突检测**：请求正文里同一坐标的相斥表达（写实/卡通、破旧/整洁、低模/高精度、纯白/环境背景等）
  必须在调用生图**之前**阻断，并给出冲突双方与规则来源。
- **纯白背景固定**：人物/服装基础资产强制纯白 #FFFFFF 背景，风格包的灰底/摄影棚底不得覆盖。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field, fields as dataclass_fields
from typing import Iterable, Mapping, Sequence

RULE_PRECEDENCE: tuple[str, ...] = (
    "用户明确要求",
    "Omni标准",
    "原文事实",
    "项目美术总则",
    "唯一主风格包",
    "类型模板",
    "必要补全",
)

# 冲突坐标：同一请求里两侧词同时出现即为自相矛盾。左右两侧只保留语义明确的对立项，
# 避免把「无卡通」「非写实噪点」这类否定式短语中的单词误判为正向诉求——调用方应把
# 禁止项/负面块排除在检测范围之外（见 `check_prompt_conflicts`）。
CONFLICT_AXES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("写实/卡通",
     ("写实", "照片级", "物理渲染", "超真实"),
     ("卡通", "二次元", "Q版", "赛璐璐", "动画风")),
    ("高饱和/低饱和",
     ("高饱和", "浓艳", "色彩饱和"),
     ("低饱和", "灰调", "莫兰迪")),
    ("华丽/朴素",
     ("华丽", "奢华", "繁复"),
     ("朴素", "简朴", "素净")),
    ("干净/破旧",
     ("整洁", "崭新", "完好"),
     ("破旧", "褴褛", "破烂", "残破")),
    ("低模/高精度",
     ("低模", "低多边形", "简模"),
     ("高精度", "超精细", "细节丰富")),
    ("纯白背景/环境背景",
     ("纯白背景", "纯白底", "#FFFFFF"),
     ("环境背景", "灰背景", "灰色背景", "素灰背景", "摄影棚背景")),
    ("空镜/剧情画面",
     ("无人物", "空镜", "空场"),
     ("剧情画面", "对峙", "动作场面")),
)

# 人物/服装基础资产种类——只有这些必须锁死纯白背景
_PURE_WHITE_KINDS = ("正脸", "后脑", "服装独立", "人物全身")
_NEVER_PURE_WHITE_HINTS = ("场景", "道具", "剧情", "剧照", "动作参考", "材质特写")
_WHITE_LINE = "背景：纯白 #FFFFFF（人物/服装基础资产固定，非实景、非棚拍环境）"


def precedence_ranks() -> dict[str, int]:
    """规则层名 → 优先级秩（数字越小越优先）。"""
    return {name: i for i, name in enumerate(RULE_PRECEDENCE)}


def resolve_with_sources(slots: Sequence[tuple[str, Mapping[str, str]]]
                         ) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """按 `RULE_PRECEDENCE` 合并多层槽位，返回（确定值, 每槽来源层, 裁决记录）。

    覆盖只发生在**同一槽位**上：上游层一旦给出非空确定值，下游层的同名槽位一律不采用；
    未被任何上游占用的槽位仍由较低层填充（上游沉默 ≠ 上游否决）。
    """
    ranks = precedence_ranks()
    resolved: dict[str, str] = {}
    owners: dict[str, str] = {}
    rulings: list[str] = []
    for source, values in sorted(slots, key=lambda s: ranks.get(s[0], len(RULE_PRECEDENCE))):
        for key, value in (values or {}).items():
            text = str(value or "").strip()
            if not text:
                continue
            if key in resolved:
                rulings.append(f"{source}「{key}」被上游 {owners[key]} 确定值覆盖，未采用")
                continue
            resolved[key] = text
            owners[key] = source
    return resolved, owners, rulings


def resolve_by_precedence(slots: Sequence[tuple[str, Mapping[str, str]]]) -> tuple[dict[str, str], list[str]]:
    """`resolve_with_sources` 的简版：只要确定值与裁决记录。"""
    resolved, _owners, rulings = resolve_with_sources(slots)
    return resolved, rulings


@dataclass(frozen=True)
class Conflict:
    axis: str
    a_term: str
    b_term: str
    a_source: str = "请求正文"
    b_source: str = "请求正文"

    def describe(self) -> str:
        return (f"{self.axis} 冲突：{self.a_source}「{self.a_term}」 "
                f"与 {self.b_source}「{self.b_term}」 不可共存")


class ArtConflictError(ValueError):
    """请求中出现相斥的美术表达——生图前阻断。"""

    def __init__(self, conflicts: Sequence[Conflict]):
        self.conflicts = tuple(conflicts)
        detail = "；".join(c.describe() for c in self.conflicts)
        super().__init__(detail or "美术规则冲突")


@dataclass(frozen=True)
class ArtConstitution:
    """项目美术总则：九要素 + 禁止项 + 参考图哈希。"""

    medium: str = ""
    realism: str = ""
    proportions: str = ""
    materials: str = ""
    palette: str = ""
    lighting: str = ""
    era: str = ""
    scene_aesthetic: str = ""
    costume_aesthetic: str = ""
    forbidden: tuple[str, ...] = field(default_factory=tuple)
    reference_hash: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["forbidden"] = list(self.forbidden)
        return d

    def content_fingerprint(self) -> str:
        payload = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


CONSTITUTION_FILENAME = "项目美术总则.json"
_PROCESS_DIRNAME = ".process"


class ArtConstitutionInvalid(ValueError):
    """项目美术总则存在但不可用——必须显式失败，不得退化成「这个项目没有总则」。"""


def save_constitution(lib, constitution: ArtConstitution) -> str:
    """落盘 `.process/项目美术总则.json`，随内容写入指纹供依赖失效判定。"""
    proc = os.path.join(str(lib), _PROCESS_DIRNAME)
    os.makedirs(proc, exist_ok=True)
    path = os.path.join(proc, CONSTITUTION_FILENAME)
    payload = constitution.to_dict()
    payload["fingerprint"] = constitution.content_fingerprint()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def load_constitution(lib) -> ArtConstitution:
    """载入项目美术总则。文件不存在返回空总则（未设定 ≠ 损坏）；损坏或指纹不符必须报错。"""
    path = os.path.join(str(lib), _PROCESS_DIRNAME, CONSTITUTION_FILENAME)
    if not os.path.isfile(path):
        return ArtConstitution()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ArtConstitutionInvalid(f"{CONSTITUTION_FILENAME} 无法解析：{exc}") from exc
    if not isinstance(data, dict):
        raise ArtConstitutionInvalid(f"{CONSTITUTION_FILENAME} 顶层必须是对象")
    recorded = str(data.pop("fingerprint", "") or "")
    allowed = {f.name for f in dataclass_fields(ArtConstitution)}
    kwargs = {k: v for k, v in data.items() if k in allowed}
    if "forbidden" in kwargs:
        kwargs["forbidden"] = tuple(kwargs.get("forbidden") or ())
    constitution = ArtConstitution(**kwargs)
    if recorded and recorded != constitution.content_fingerprint():
        raise ArtConstitutionInvalid(
            f"{CONSTITUTION_FILENAME} 内容指纹不符（记录 {recorded}，实算 {constitution.content_fingerprint()}）")
    return constitution


def _first_hit(text: str, words: Iterable[str]) -> str:
    for w in words:
        if w and w in text:
            return w
    return ""


def detect_conflicts_by_source(segments: Mapping[str, str]) -> list[Conflict]:
    """把请求拆成 {规则来源: 文本}，逐轴找两侧词；同一来源内部也允许冲突命中。"""
    out: list[Conflict] = []
    for axis, lefts, rights in CONFLICT_AXES:
        left_hits = [(src, _first_hit(txt, lefts)) for src, txt in segments.items()
                     if txt and _first_hit(txt, lefts)]
        right_hits = [(src, _first_hit(txt, rights)) for src, txt in segments.items()
                      if txt and _first_hit(txt, rights)]
        if not left_hits or not right_hits:
            continue
        a_src, a_term = left_hits[0]
        b_src, b_term = right_hits[0]
        out.append(Conflict(axis=axis, a_term=a_term, b_term=b_term,
                            a_source=a_src, b_source=b_src))
    return out


def detect_conflicts(text: str) -> list[Conflict]:
    return detect_conflicts_by_source({"请求正文": text or ""})


_BLOCK_SPLIT = re.compile(r"^【([^】]+)】\s*$", re.M)
_NEGATIVE_BLOCKS = ("禁止项", "负面提示词", "negative", "negative_prompt")


def split_prompt_blocks(prompt: str) -> dict[str, str]:
    """把编译后的提示词按 【块名】 切开（禁止项块单独归到 `_negative` 键，不参与冲突判定）。"""
    parts = _BLOCK_SPLIT.split(prompt or "")
    out: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        name = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        key = "_negative" if name in _NEGATIVE_BLOCKS else name
        out[key] = (out.get(key, "") + "\n" + body).strip()
    return out


def check_prompt_conflicts(prompt: str) -> None:
    """在真正调用生图之前阻断相斥表达。禁止项/负面块不参与冲突判定。"""
    blocks = split_prompt_blocks(prompt)
    affirmative = {k: v for k, v in blocks.items() if k != "_negative"}
    if not affirmative:
        affirmative = {"请求正文": prompt or ""}
    conflicts = detect_conflicts_by_source(affirmative)
    if conflicts:
        raise ArtConflictError(conflicts)


def requires_pure_white(kind: str) -> bool:
    k = (kind or "").strip()
    if not k:
        return False
    if any(h in k for h in _NEVER_PURE_WHITE_HINTS):
        return False
    return any(h in k for h in _PURE_WHITE_KINDS)


_ENV_BG_PATTERNS = ("素灰背景", "灰背景", "灰色背景", "环境背景", "摄影棚背景",
                    "实景背景", "户外背景", "室内背景")


def enforce_pure_white_background(text: str, kind: str) -> str:
    """人物/服装基础资产固定纯白背景：剥离环境/摄影棚背景描述行，追加纯白硬约束。

    仅按**行**过滤：只有整行含背景关键词才删除，同段的其他指令保留。
    """
    if not requires_pure_white(kind):
        return text
    kept: list[str] = []
    for line in (text or "").splitlines():
        if any(pat in line for pat in _ENV_BG_PATTERNS):
            continue
        kept.append(line)
    body = "\n".join(kept).rstrip()
    return (body + "\n\n" if body else "") + _WHITE_LINE
