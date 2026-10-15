#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""结构化视觉质量检查（规格 8，工单03-Task5）。

- 问题码固定枚举：覆盖老罗失败反例（风格漂移/比例异常/断颈残留/背面残头/禁止拼版/
  正背不一致/主体裁切）；**阻断级问题一律不得进入 ACCEPTED**。
- `parse_qc_response` 只接受结构化映射；缺证据、引用错误任务、输入哈希不一致、
  未知问题码、人工缺操作者标签，一律返回 `QC_UNTRUSTWORTHY` 而非"勉强通过"。
- 人工 / Codex / Gemini 三类检查器共用同一 `QcResult` 与同一写回路径。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 回退
    import tomli as tomllib

from .visual_schema import AttemptRecord, QcDecision, VisualTask, normalize_rel_path

SKILL_DIR = Path(__file__).resolve().parent.parent.parent
RUBRIC_DIR = SKILL_DIR / "templates" / "视觉质检"

BLOCKING = "blocking"
MAJOR = "major"
MINOR = "minor"
SEVERITIES = (BLOCKING, MAJOR, MINOR)

# 艺术问题码（检查器可以报的）
ISSUE_CODES = frozenset({
    # 通用
    "STYLE_MISMATCH", "SUBJECT_CROPPED", "FORBIDDEN_MULTI_PANEL",
    "EXTRA_SUBJECT", "REFERENCE_OVERREACH", "TEXT_OR_WATERMARK",
    "MATERIAL_MISMATCH", "LIGHTING_MISMATCH", "LOW_DETAIL",
    # 人物
    "IDENTITY_DRIFT", "BODY_PROPORTION_ERROR", "NECK_STUMP_VISIBLE",
    "HEAD_REMAINS_IN_COSTUME_VIEW", "COSTUME_INCOMPLETE", "COSTUME_FRONT_BACK_MISMATCH",
    "FRONT_BACK_INCONSISTENT", "HAIR_STRUCTURE_ERROR",
    # 场景
    "SPACE_ANCHOR_MISSING", "SPACE_RELATION_ERROR", "SPACE_FACT_INVENTED",
    # 道具
    "PROP_SILHOUETTE_UNREADABLE", "PROP_STATE_MISMATCH", "PROP_PROPORTION_ERROR",
    # 阶段 6 工单 6.2 的结构化硬门（Omni 审核清单）
    "HAND_FINGER_DEFECT", "BODY_PROPORTION_DEFECT", "CROP_MISMATCH",
    "POSITION_MISMATCH", "VIEWPOINT_MISMATCH", "EYELINE_MISMATCH", "AXIS_CROSSING",
    "OCCLUSION_MISMATCH", "PROP_COUNT_MISMATCH", "SCENE_STRUCTURE_MISMATCH",
    "REFERENCE_CHAIN_INVALID",
})
# 流程问题码（检查结果本身不可信，不是画作缺陷）
PROCESS_CODES = frozenset({"QC_UNTRUSTWORTHY", "UNKNOWN_ISSUE_CODE",
                           "EVIDENCE_MISSING", "REPAIR_MISSING"})

# 硬门码（规格九 / 施工单 6.2）：码表顺序与施工单逐一对应，存在任一即不得放行
HARD_GATE_CODES = (
    "HAND_FINGER_DEFECT", "BODY_PROPORTION_DEFECT", "IDENTITY_DRIFT", "STYLE_MISMATCH",
    "CROP_MISMATCH", "POSITION_MISMATCH", "VIEWPOINT_MISMATCH", "EYELINE_MISMATCH",
    "AXIS_CROSSING", "OCCLUSION_MISMATCH", "PROP_COUNT_MISMATCH", "PROP_STATE_MISMATCH",
    "SCENE_STRUCTURE_MISMATCH", "EXTRA_SUBJECT", "TEXT_OR_WATERMARK",
    "REFERENCE_CHAIN_INVALID",
)

# 判定集合（规格九）：通过／通过但有备注／需要返工／无法判断
MANDATED_DECISIONS = ("ACCEPTED", "PASS_WITH_NOTES", "NEEDS_REVISION",
                      "NEEDS_HUMAN_DECISION")

# 同义旧码 → 规范硬门码：老检查器报旧名也必须触发同一道硬门，不得成为绕过口
CODE_ALIASES = {
    "BODY_PROPORTION_ERROR": "BODY_PROPORTION_DEFECT",
    "SUBJECT_CROPPED": "CROP_MISMATCH",
    "SPACE_RELATION_ERROR": "SCENE_STRUCTURE_MISMATCH",
    "SPACE_ANCHOR_MISSING": "SCENE_STRUCTURE_MISMATCH",
    "SPACE_FACT_INVENTED": "SCENE_STRUCTURE_MISMATCH",
}

_HARD_GATE_SET = frozenset(HARD_GATE_CODES)

KIND_SUBJECT = {
    "正脸": "人物", "后脑": "人物", "服装独立正面": "人物", "服装独立背面": "人物",
    "人物全身正面": "人物", "人物全身背面": "人物", "场景母版": "场景", "道具": "道具",
}


@dataclass(frozen=True)
class QcIssue:
    """一条结构化检查问题：码 + 严重度 + 证据 + 定向修复指令。"""

    code: str
    severity: str = BLOCKING
    evidence: str = ""
    repair_instruction: str = ""

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"非法严重度 {self.severity!r}（只允许 {list(SEVERITIES)}）")

    @property
    def is_blocking(self) -> bool:
        return self.severity == BLOCKING


@dataclass(frozen=True)
class QcResult:
    """统一检查结果（人工/Codex/Gemini 共用）。"""

    decision: str = QcDecision.NEEDS_REVISION.value
    issues: tuple[QcIssue, ...] = ()
    checker: str = ""
    checked_at: str = ""
    input_hashes: tuple[str, ...] = ()
    task_id: str = ""
    note: str = ""
    untrustworthy_reason: str = ""

    @property
    def blocking_issues(self) -> tuple[QcIssue, ...]:
        return tuple(i for i in self.issues if i.is_blocking)

    @property
    def is_accepted(self) -> bool:
        return self.decision == QcDecision.ACCEPTED.value

    def codes(self) -> tuple[str, ...]:
        return tuple(i.code for i in self.issues)


def subject_for_kind(kind: str) -> str:
    """任务种类 → 质检对象类别（人物/场景/道具）。"""
    return KIND_SUBJECT.get(str(kind), "人物")


def load_rubric(subject: str) -> dict:
    """读取检查清单（人物/场景/道具）；缺文件即显式失败，不用空清单冒充。"""
    path = RUBRIC_DIR / f"{subject}.toml"
    if not path.is_file():
        raise ValueError(f"缺少质检 rubric：{path}")
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    if not data.get("checks"):
        raise ValueError(f"质检 rubric 未定义检查项：{path}")
    return data


def rubric_for_kind(kind: str) -> dict:
    """按任务种类直接取对应 rubric（人物/场景/道具）。"""
    return load_rubric(subject_for_kind(kind))


def build_qc_request(task: VisualTask, attempt: AttemptRecord, rubric: Mapping,
                     generation_prompt: str = "") -> dict:
    """组装质检请求（与生成提示词必须不同；只带本任务图片与检查清单）。"""
    if not str(attempt.image or "").strip():
        raise ValueError("质检请求缺少待检查图片")
    if not rubric or not rubric.get("checks"):
        raise ValueError("质检请求缺少 rubric 检查项")
    image = normalize_rel_path(attempt.image)
    checks = "；".join(str(c) for c in rubric["checks"])
    blocking = "、".join(str(c) for c in rubric.get("blocking_codes", ()))
    prompt = (
        "你是图源质检员。请对给定**单张**图片做结构化质检，"
        "只依据下面的检查清单与资产事实，不评价审美偏好。\n"
        f"资产任务：{task.task_id}｜种类：{task.kind}｜画幅：{task.aspect_ratio}\n"
        f"事实依据：{task.fact_source}\n"
        f"检查清单：{checks}\n"
        f"阻断级问题码：{blocking}\n"
        "输出结构化结果（问题码 / 严重度 / 证据 / 定向修复指令）："
        "每个问题必须有可见证据，无证据不要报；"
        "存在任何阻断级问题即不得判为通过。禁止输出多格或拼版画面；若画面为多格，"
        "报 FORBIDDEN_MULTI_PANEL。"
    )
    if generation_prompt and generation_prompt.strip() == prompt.strip():
        raise ValueError("质检提示词不得与生成提示词相同（生成与检查必须使用不同提示词）")
    return {
        "task_id": task.task_id,
        "kind": task.kind,
        "aspect_ratio": task.aspect_ratio,
        "image": image,
        "input_hashes": [attempt.image_sha256] if attempt.image_sha256 else [],
        "rubric_subject": str(rubric.get("subject", "")),
        "prompt": prompt,
        "negative": "不要输出解释性长文；只输出结构化字段。",
    }


def _coerce_issue(raw: Any) -> tuple[QcIssue | None, str]:
    if not isinstance(raw, Mapping):
        return None, "问题项必须是对象"
    code = str(raw.get("code") or "").strip()
    severity = str(raw.get("severity") or BLOCKING).strip()
    evidence = str(raw.get("evidence") or "").strip()
    repair = str(raw.get("repair_instruction") or raw.get("repair") or "").strip()
    canonical = CODE_ALIASES.get(code, code)
    if code not in ISSUE_CODES and canonical not in ISSUE_CODES:
        return None, f"未知问题码 {code!r}"
    if not evidence:
        return None, f"问题 {code} 缺少证据（evidence）"
    if not repair:
        return None, f"问题 {code} 缺少定向修复指令"
    if canonical in _HARD_GATE_SET:
        # 硬门码的严重度由码本身决定：检查器把它标成 minor 也不能降低门槛。
        # 保留检查器报出的码名（同义旧码与规范码等价，映射见 CODE_ALIASES）。
        severity = BLOCKING
    try:
        return QcIssue(code=code, severity=severity, evidence=evidence,
                       repair_instruction=repair), ""
    except ValueError as e:
        return None, str(e)


def parse_qc_response(data: Mapping, expected: Mapping | None = None) -> QcResult:
    """解析结构化检查响应；任何不可信情形返回 QC_UNTRUSTWORTHY。"""
    if not isinstance(data, Mapping) or isinstance(data, (str, bytes)):
        raise ValueError("质检响应必须是结构化映射（JSON 对象/TOML 映射），不接受自由文本或字符串")

    expected = dict(expected or {})
    manual = bool(data.get("manual"))
    task_id = str(data.get("task_id") or "").strip()
    checker = str(data.get("checker") or "").strip()
    checked_at = str(data.get("checked_at") or "").strip()
    note = str(data.get("note") or "").strip()
    hashes = tuple(str(h) for h in (data.get("input_hashes") or ()) if str(h).strip())
    raw_issues = data.get("issues") or []

    untrustworthy = ""
    if not isinstance(raw_issues, list):
        untrustworthy = "issues 必须是列表"
    elif expected.get("task_id") and task_id != str(expected["task_id"]):
        untrustworthy = f"检查结果引用了错误任务（{task_id} ≠ {expected['task_id']}）"
    elif expected.get("input_hashes") and hashes and \
            set(hashes) != set(str(h) for h in expected["input_hashes"]):
        untrustworthy = "检查结果的输入哈希与被检产物不一致"
    elif expected.get("input_hashes") and not hashes:
        untrustworthy = "检查结果未提供输入哈希，无法确认检的是哪张图"
    elif manual and not checker:
        untrustworthy = "人工判定必须记录操作者标签（checker）"
    else:
        # 六项绑定闭包（阶段 2 工单 2.2）：提示词/参考集/画风/构图卡任一变化即失效
        for key in ("prompt_sha256", "reference_set_sha256", "style_sha256", "composition_sha256"):
            want = str(expected.get(key) or "").strip().lower()
            if not want:
                continue
            got = str(data.get(key) or "").strip().lower()
            if got != want:
                untrustworthy = f"审核绑定已失效：{key} 与当前输入不一致"
                break

    issues: list[QcIssue] = []
    if not untrustworthy:
        for raw in raw_issues:
            issue, err = _coerce_issue(raw)
            if err:
                untrustworthy = err
                break
            issues.append(issue)

    if untrustworthy:
        return QcResult(decision=QcDecision.UNTRUSTWORTHY.value, issues=tuple(issues),
                        checker=checker, checked_at=checked_at, input_hashes=hashes,
                        task_id=task_id, note=note, untrustworthy_reason=untrustworthy)

    decision = str(data.get("decision") or "").strip()
    allowed = set(MANDATED_DECISIONS) | {QcDecision.UNTRUSTWORTHY.value}
    if decision not in allowed:
        return QcResult(decision=QcDecision.UNTRUSTWORTHY.value, issues=tuple(issues),
                        checker=checker, checked_at=checked_at, input_hashes=hashes,
                        task_id=task_id, note=note,
                        untrustworthy_reason=f"非法判定 {decision!r}")
    if decision != QcDecision.UNTRUSTWORTHY.value and any(i.is_blocking for i in issues):
        # 阻断级问题（含全部硬门码）一律不得进入「通过」或「通过但有备注」
        decision = QcDecision.NEEDS_REVISION.value
    return QcResult(decision=decision, issues=tuple(issues), checker=checker,
                    checked_at=checked_at, input_hashes=hashes, task_id=task_id, note=note)


def _toml_str(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def qc_result_to_toml(result: QcResult) -> str:
    """确定性写回（中文直写，不转义为 \\uXXXX）。"""
    lines = [
        "# 视觉质检结果（结构化写回；人工/Codex/Gemini 共用同一结构）",
        f"decision = {_toml_str(result.decision)}",
        f"checker = {_toml_str(result.checker)}",
        f"checked_at = {_toml_str(result.checked_at)}",
        f"task_id = {_toml_str(result.task_id)}",
        "input_hashes = [" + ", ".join(_toml_str(h) for h in result.input_hashes) + "]",
    ]
    if result.untrustworthy_reason:
        lines.append(f"untrustworthy_reason = {_toml_str(result.untrustworthy_reason)}")
    if result.note:
        lines.append(f"note = {_toml_str(result.note)}")
    lines.append("")
    for issue in result.issues:
        lines.append("[[issues]]")
        lines.append(f"code = {_toml_str(issue.code)}")
        lines.append(f"severity = {_toml_str(issue.severity)}")
        lines.append(f"evidence = {_toml_str(issue.evidence)}")
        lines.append(f"repair_instruction = {_toml_str(issue.repair_instruction)}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def write_qc_result(lib: Path, attempt: AttemptRecord, result: QcResult) -> Path:
    """把检查结果原子写回该轮次目录的 qc.toml（历史轮次各自保留）。"""
    rel = normalize_rel_path(attempt.qc) or (
        str(Path(normalize_rel_path(attempt.image)).parent / "qc.toml").replace("\\", "/"))
    target = Path(lib) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(qc_result_to_toml(result), encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return target
