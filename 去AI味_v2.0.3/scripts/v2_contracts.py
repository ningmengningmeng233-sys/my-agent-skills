#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2_contracts.py - v2.0.0 共用数据合同（alpha.1 P01 唯一落地处）。

总工单「共用类型合同」要求所有 v2 结构集中在本模块，严格校验 JSON 及未知字段；
引用现有 ``novel_contracts.SourceAnchor`` 与 ``novel_review.ReviewReport`` /
``ReviewValidation``，不另造同义类型。后续任务（P02 会话、P03 任务、P04 候选、
P08 判定、P10 补丁、P11 验证、P12 提交）只消费这里的类型与校验函数，接口同名
不得另写。

合同的三个不变量（与设计方案 1/6/7 节对齐）：

  1. **缺字段保持 None**：用户没有明确说的决策（处理方式/Jev/扫描档/输出形态）
     一律 ``None``，绝不填推荐值——推荐不是授权。
  2. **未知字段拒绝**：每一层 JSON 出现合同之外的字段就 ``ValueError``，不静默
     丢弃——静默丢弃会让授权语义藏在合同外字段里溜进门。
  3. **stage 是证据绑定字段**：``RELEASE_STAGES`` 取总工单全局约束 12 的值集
     （alpha.1…stable）；``metadata.version`` 仍写三段 ``2.0.0``，不向现有解析器
     塞预发布字符串。

外部运行依赖的登记表在 ``runtime_contract.EXTERNAL_DEPENDENCIES``（总工单全局
约束 10：随任务登记 runtime_contract.py 权威清单）；本模块经
``validate_external_imports`` 消费它，不复制第二份清单。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from novel_contracts import SourceAnchor
from novel_review import ReviewReport, ReviewValidation

# ---------------------------------------------------------------- 枚举常量

# 处理方式四选（用户要求第二节三入口 + 明确"只润色"自然语言入口）；None=未确认。
REQUEST_MODES: tuple[str, ...] = ("check", "clean", "clean_polish", "polish_only")
# Jev 只有明确启用/明确关闭两态；None=必须先问（用户要求第四节）。
REQUEST_JEV: tuple[str, ...] = ("enabled", "disabled")
REQUEST_SCAN: tuple[str, ...] = ("normal", "expanded")
# 输出形态沿用 v1 的 Rewrite(chat)/File/Embedded 合同；Audit 是处理方式(mode=check)，
# 不再是输出形态——输出形态与处理方式分别管理（设计 1 节）。
REQUEST_OUTPUT: tuple[str, ...] = ("chat", "file", "embedded")
# 有效改写力度：沿用 v1 的三档权限增量（novel_job.STRENGTHS），是规格字段而不是 scan 的副产物。
STRENGTHS: tuple[str, ...] = ("conservative", "balanced", "deep")
STRENGTH_LABELS: dict[str, str] = {"conservative": "保守", "balanced": "平衡",
                                   "deep": "深度"}
#: 场景词表就是入口 S3 那份（`SKILL.md` 第 6 节 S3 SCENE），外加「未知」。
#: 场景决定**合法默认力度**，与扫描范围（scan）、高风险授权各自独立。
SCENES: tuple[str, ...] = ("chat", "status", "docs", "public_writing", "novel",
                            "unknown")
SCENE_LABELS: dict[str, str] = {"chat": "聊天", "status": "数据汇报", "docs": "技术文档",
                                "public_writing": "普通内联短文", "novel": "小说/整本",
                                "unknown": "未定"}
#: 默认力度矩阵：与 `references/scenes-and-boundaries.md` 6.2 速查表同一口径——
#: 小说/整本去味与聊天、普通内联短文走平衡；技术文档、数据汇报走保守；
#: 场景未知且这次可能扩大权限（要写正文/落文件）时退回保守，不静默升档。
#: 深度只能由用户明示进入，永远不是默认。
SCENE_DEFAULT_STRENGTH: dict[str, str] = {"novel": "balanced", "chat": "balanced",
                                          "public_writing": "balanced",
                                          "docs": "conservative",
                                          "status": "conservative"}
DEFAULT_STRENGTH = "balanced"
#: 力度来源按事件记录，不按数值反推：同值也可能是用户明示，异值也可能是代理自选。
STRENGTH_ORIGINS: tuple[str, ...] = ("user_explicit", "legal_default",
                                     "agent_chosen", "legacy_unknown")
#: 场景来源同样按记录走：S3 判出的场景是「受控识别」，不是用户明示；
#: 用户自己说出来的场景才是 user_explicit；没说就是 unknown（默认按保守方向）。
SCENE_ORIGINS: tuple[str, ...] = ("user_explicit", "controlled_identified",
                                  "legal_default", "legacy_unknown")
SCENE_ORIGIN_LABELS: dict[str, str] = {"user_explicit": "用户明示",
                                       "controlled_identified": "受控识别",
                                       "legal_default": "未定（按保守方向）",
                                       "legacy_unknown": "旧载荷来源不明"}
STRENGTH_ORIGIN_LABELS: dict[str, str] = {"user_explicit": "用户明示",
                                          "legal_default": "合法默认",
                                          "agent_chosen": "代理自选",
                                          "legacy_unknown": "旧载荷来源不明"}


def default_strength(scene=None, output=None, write_body=False) -> str:
    """没明示力度时的合法默认：只认场景与「这次会不会扩大权限」，不认扫描档位。"""
    known = SCENE_DEFAULT_STRENGTH.get(str(scene or "unknown"))
    if known:
        return known
    return "conservative" if (str(output or "") == "file" or write_body) else DEFAULT_STRENGTH

# 交互状态全集（设计 6 节）：正常链 + PATCH 预览门 + 裁判暂停 + 任务终态。
INTERACTION_STATES: tuple[str, ...] = (
    "WAIT_MODE", "WAIT_JEV", "WAIT_JEV_KEY", "WAIT_TASK_CONFIRMATION",
    "READY", "RUNNING", "WAIT_PATCH_CONFIRMATION",
    "PAUSED_JUDGE_UNAVAILABLE", "PAUSED", "FAILED", "COMPLETED")

# release_stage 值集（总工单全局约束 12）；stage 进入 VerificationBundle /
# CommitRecord 的证据绑定，P17 全链核查。
RELEASE_STAGES: tuple[str, ...] = ("alpha.1", "alpha.2", "alpha.3",
                                   "beta.1", "rc.1", "stable")

# 判定结论三值：KEEP 是一等结果，证据不足与 KEEP 分开（设计 5 节）。
JUDGMENT_STATUSES: tuple[str, ...] = ("keep", "rewrite", "insufficient_evidence")

# patch 风险两档（用户要求第二十五节）；操作四类——KEEP 不产生 patch（NO-OP 是
# 一等结果），operation 只表达真实写动作。
PATCH_RISKS: tuple[str, ...] = ("low", "high")
PATCH_OPERATIONS: tuple[str, ...] = ("delete", "merge", "local_rewrite",
                                     "paragraph_rewrite")

# 提交记录状态：成功提交 / 任务回滚还原 / 提交失败。
COMMIT_STATUSES: tuple[str, ...] = ("committed", "rolled_back", "failed")
#: 另存通道没有「被覆盖的前态」，也就没有备份可指。登记里用这个声明值占位，
#: 让回滚能说清「这件不是本层能撤销的覆盖」，而不是假装有一条备份记录。
CREATED_BACKUP_REF = "created-by-job:nothing-overwritten"

# 机械验证层级：VerificationBundle 只聚合 L0/L1 机械层；L2/L3 语义审查在
# ReviewReport.completed_levels 里，语义发布权不来自本模块（设计 7 节）。
MECHANICAL_LEVELS: tuple[str, ...] = ("L0", "L1")

_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

# TaskSpec 合同 schema 版本：授权字段结构变化时递增（补正方案 3.1）。
TASK_SPEC_SCHEMA_VERSION = "lieflat-v2-task-spec-4"

# form_task_spec 接受的任务内容键集（= TaskSpec 字段去掉 request_digest）。
_TASK_SPEC_CONTENT_KEYS: tuple[str, ...] = (
    "task_id", "source_files", "choices", "judge_ref", "model_version",
    "rule_version", "prompt_version", "output_policy", "schema_version",
    "task_revision", "judge_epoch", "runtime_identity", "voice", "backup",
    "strength", "strength_origin", "scene", "scene_origin")


def derive_scene_fields(content: Mapping) -> tuple[str, str]:
    """``(scene, scene_origin)`` 的唯一推导处。

    - 载荷没给 scene → ``unknown`` + ``legal_default``（默认按保守方向，不猜场景）；
    - 给了 scene 没给来源：当前 schema 视为**受控识别**（S3 由执行代理判出），
      旧 schema 的载荷记 ``legacy_unknown``；
    - 用户事件给出场景时由 ``v2_session`` 写 ``user_explicit``，本函数只照抄。
    """
    content = dict(content)
    raw = str(content.get("scene") or "unknown")
    scene = raw if raw in SCENES else "unknown"
    if content.get("scene") in (None, ""):
        return "unknown", "legal_default"
    origin = content.get("scene_origin")
    if not origin:
        origin = ("legacy_unknown"
                  if content.get("schema_version") != TASK_SPEC_SCHEMA_VERSION
                  else "controlled_identified")
    return scene, str(origin)


def derive_strength_fields(content: Mapping) -> tuple[str, str, str]:
    """``(scene, strength, strength_origin)`` 的唯一推导处。

    - 载荷没给 strength → 按场景取合法默认，来源记 ``legal_default``；
    - 给了值没给来源：当前 schema 视为**代理自选**（Agent 自己填的，不能算用户明示），
      旧 schema 的载荷记 ``legacy_unknown``，不追溯解释；
    - 来源由事件写入（见 ``v2_session``），本函数只照抄，不再由数值反推。
    """
    content = dict(content)
    scene, _given_origin = derive_scene_fields(content)
    policy = content.get("output_policy") or {}
    if "strength" not in content or content.get("strength") is None:
        return scene, default_strength(scene, policy.get("output"),
                                       bool(policy.get("write_body"))), "legal_default"
    origin = content.get("strength_origin")
    if not origin:
        origin = ("legacy_unknown"
                  if content.get("schema_version") != TASK_SPEC_SCHEMA_VERSION
                  else "agent_chosen")
    return scene, str(content["strength"]), str(origin)


def _require_hex(value, label: str) -> str:
    if not isinstance(value, str) or not _HEX_RE.match(value):
        raise ValueError(f"{label} 必须是 64 位十六进制 SHA-256: {value!r}")
    return value.lower()


def _require_nonempty_str(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串: {value!r}")
    return value


def _require_enum(value, allowed: tuple, label: str):
    if value not in allowed:
        raise ValueError(f"{label} 非法: {value!r}，合法值 {allowed}")
    return value


def _require_nonneg_int(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} 必须是非负整数: {value!r}")
    return value


def _freeze_keys(mapping: Mapping, allowed: tuple[str, ...], label: str) -> None:
    """合同键集之外的字段一律拒绝；缺键由各构造方自己补（None 语义）。"""
    if not isinstance(mapping, Mapping):
        raise ValueError(f"{label} 必须是映射: {mapping!r}")
    extra = sorted(set(mapping) - set(allowed))
    if extra:
        raise ValueError(f"{label} 含未知字段: {','.join(extra)}")


# ---------------------------------------------------------------- RequestChoices

@dataclass(frozen=True)
class RequestChoices:
    """一次用户请求中**显式给出**的决策；缺字段保持 None，不猜默认。

    mode/jev/scan/output 四个决策字段互相独立（用户要求第六节：一次同意只代表
    当前阶段）；explicit_sources 是用户点名纳入范围的来源（用户指定范围优先，
    未知文件不自动入正文）。
    """

    mode: str | None = None
    jev: str | None = None
    scan: str | None = None
    output: str | None = None
    explicit_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, allowed in (("mode", REQUEST_MODES), ("jev", REQUEST_JEV),
                              ("scan", REQUEST_SCAN), ("output", REQUEST_OUTPUT)):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _require_enum(value, allowed, name))
        sources = tuple(self.explicit_sources)
        for item in sources:
            _require_nonempty_str(item, "explicit_sources")
        if len(set(sources)) != len(sources):
            raise ValueError("explicit_sources 含重复条目")
        object.__setattr__(self, "explicit_sources", sources)

    @classmethod
    def from_mapping(cls, value: Mapping) -> "RequestChoices":
        """严格键集构造：未知字段拒绝，缺字段保持 None。"""
        _freeze_keys(value, ("mode", "jev", "scan", "output", "explicit_sources"),
                     "choices")
        return cls(
            mode=value.get("mode"), jev=value.get("jev"),
            scan=value.get("scan"), output=value.get("output"),
            explicit_sources=tuple(value.get("explicit_sources") or ()))


# ---------------------------------------------------------------- 任务摘要（唯一实现）

def compute_task_digest(content: Mapping) -> str:
    """任务摘要的唯一实现（补正方案 3.2）：规范 JSON 序列化 + SHA-256。

    覆盖 task_id/revision、规范来源身份与哈希、mode/jev/scan/output、裁判与
    model/rule/prompt 版本、voice/backup/output_policy、judge_epoch 和
    runtime_identity；排除 request_digest 自身、凭据明文与易变时间戳。
    任何内容变更都会改变摘要——不能靠调用方忘记更新来延续授权。
    """
    fields = derive_strength_fields(content)
    files: dict[str, str] = {}
    for item in content.get("source_files") or ():
        path, sha = item.get("path"), item.get("sha256")
        if path in files and files[path] != sha:
            raise ValueError(f"source_files 同一路径携带不同哈希: {path}")
        files[path] = sha
    choices = content.get("choices") or {}
    payload = {
        "schema_version": content.get("schema_version"),
        "task_id": content.get("task_id"),
        "task_revision": content.get("task_revision", 0),
        "source_files": sorted([p, files[p]] for p in files),
        "choices": {
            "mode": choices.get("mode"), "jev": choices.get("jev"),
            "scan": choices.get("scan"), "output": choices.get("output"),
            "explicit_sources": sorted(choices.get("explicit_sources") or ()),
        },
        "judge_ref": dict(content.get("judge_ref") or {}),
        "model_version": content.get("model_version"),
        "rule_version": content.get("rule_version"),
        "prompt_version": content.get("prompt_version"),
        "voice": content.get("voice", "strict"),
        "backup": content.get("backup", True),
        "output_policy": dict(content.get("output_policy") or {}),
        "scene": fields[0],
        "scene_origin": derive_scene_fields(content)[1], "strength": fields[1],
        "strength_origin": fields[2],
        "judge_epoch": content.get("judge_epoch", 0),
        "runtime_identity": dict(content.get("runtime_identity") or {}),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def content_of_task_spec(spec: "TaskSpec") -> dict:
    """TaskSpec → 任务内容字典（不含 request_digest）；摘要计算与规格改写共用。"""
    return {
        "task_id": spec.task_id,
        "source_files": [{"path": ref.path, "sha256": ref.sha256}
                         for ref in spec.source_files],
        "choices": {"mode": spec.choices.mode, "jev": spec.choices.jev,
                    "scan": spec.choices.scan, "output": spec.choices.output,
                    "explicit_sources": list(spec.choices.explicit_sources)},
        "judge_ref": dict(spec.judge_ref),
        "model_version": spec.model_version,
        "rule_version": spec.rule_version,
        "prompt_version": spec.prompt_version,
        "output_policy": dict(spec.output_policy),
        "schema_version": spec.schema_version,
        "task_revision": spec.task_revision,
        "judge_epoch": spec.judge_epoch,
        "runtime_identity": dict(spec.runtime_identity),
        "voice": spec.voice, "backup": spec.backup,
        "strength": spec.strength,
        "strength_origin": spec.strength_origin,
        "scene": spec.scene,
        "scene_origin": spec.scene_origin,
    }


def task_digest(spec: "TaskSpec") -> str:
    """从规格现场重算任务摘要：与 spec.request_digest 不符 = 内容被改动。"""
    return compute_task_digest(content_of_task_spec(spec))


def form_task_spec(content: Mapping) -> "TaskSpec":
    """从任务内容（不含 request_digest）构造 TaskSpec：摘要由本模块唯一计算。

    事件实际选择改写规格（apply_request_event）与测试工厂都走这里，
    不允许调用方自带摘要冒充内容绑定。
    """
    _freeze_keys(content, _TASK_SPEC_CONTENT_KEYS, "TaskSpec content")
    filled = dict(content)
    # 先定场景与来源，再按场景推默认力度：反过来会把「载荷没给场景」二次推导成
    # 代理判出的 controlled_identified（unknown 一旦被归一化就看不出原样了）。
    filled["scene"], filled["scene_origin"] = derive_scene_fields(filled)
    _scene, filled["strength"], filled["strength_origin"] = \
        derive_strength_fields(filled)
    return validate_task_spec({**filled,
                               "request_digest": compute_task_digest(filled)})


# ---------------------------------------------------------------- TaskSpec

@dataclass(frozen=True)
class SourceFileRef:
    """任务范围内的一个来源文件：路径 + 订约时哈希（任务摘要绑定源 hash）。"""

    path: str
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _require_nonempty_str(self.path, "path"))
        object.__setattr__(self, "sha256", _require_hex(self.sha256, "sha256"))

    @classmethod
    def from_mapping(cls, value: Mapping) -> "SourceFileRef":
        _freeze_keys(value, ("path", "sha256"), "source_files 条目")
        if "path" not in value or "sha256" not in value:
            raise ValueError(f"source_files 条目缺 path/sha256: {sorted(value)}")
        return cls(path=value["path"], sha256=value["sha256"])


@dataclass(frozen=True)
class TaskSpec:
    """任务规格快照：写文件前任务摘要的全部绑定字段（设计 6 节）。

    voice/backup 钉死 strict/True——保真与可回滚不是可协商字段；显式传其他值
    在构造期拒绝。choices 是确认后的 ``RequestChoices``；judge_ref 固定裁判
    身份（provider+model），裁判变更走 SessionRecord.judge_epoch，不改历史
    TaskSpec。
    """

    task_id: str
    source_files: tuple[SourceFileRef, ...]
    choices: RequestChoices
    judge_ref: Mapping
    model_version: str
    rule_version: str
    prompt_version: str
    output_policy: Mapping
    request_digest: str
    schema_version: str
    task_revision: int = 0
    judge_epoch: int = 0
    runtime_identity: Mapping | None = None
    voice: Literal["strict"] = "strict"
    backup: Literal[True] = True
    #: 没明示时按场景取合法默认（见 default_strength）；构造后一定是三档之一。
    strength: str | None = None
    #: 力度来源：只由事件/载荷记录，不由数值反推（裁决 2）。
    strength_origin: str | None = None
    #: 实际场景：决定合法默认力度，与 scan、高风险授权分开（裁决 3）。
    scene: str | None = None
    #: 场景来源：用户明示 / 受控识别 / 未定，不伪造用户选择（G5 裁决 2）。
    scene_origin: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id",
                           _require_nonempty_str(self.task_id, "task_id"))
        if isinstance(self.source_files, (str, bytes)) or not isinstance(
                self.source_files, (tuple, list)):
            raise ValueError("source_files 必须是序列")
        files = tuple(
            item if isinstance(item, SourceFileRef) else SourceFileRef.from_mapping(item)
            for item in self.source_files)
        if not files:
            raise ValueError("source_files 不能为空：没有来源就没有任务")
        object.__setattr__(self, "source_files", files)
        if not isinstance(self.choices, RequestChoices):
            raise ValueError("choices 必须是 RequestChoices")
        _freeze_keys(self.judge_ref, ("provider", "model"), "judge_ref")
        for key in ("provider", "model"):
            if key not in self.judge_ref:
                raise ValueError(f"judge_ref 缺必需键 {key}")
            _require_nonempty_str(self.judge_ref[key], f"judge_ref.{key}")
        object.__setattr__(self, "model_version",
                           _require_nonempty_str(self.model_version, "model_version"))
        object.__setattr__(self, "rule_version",
                           _require_nonempty_str(self.rule_version, "rule_version"))
        object.__setattr__(self, "prompt_version",
                           _require_nonempty_str(self.prompt_version, "prompt_version"))
        _freeze_keys(self.output_policy, ("output", "write_body"), "output_policy")
        if "output" not in self.output_policy or "write_body" not in self.output_policy:
            raise ValueError("output_policy 缺 output/write_body")
        _require_enum(self.output_policy["output"], REQUEST_OUTPUT,
                      "output_policy.output")
        if not isinstance(self.output_policy["write_body"], bool):
            raise ValueError("output_policy.write_body 必须是布尔")
        object.__setattr__(self, "request_digest",
                           _require_hex(self.request_digest, "request_digest"))
        object.__setattr__(self, "schema_version",
                           _require_nonempty_str(self.schema_version,
                                                 "schema_version"))
        object.__setattr__(self, "task_revision",
                           _require_nonneg_int(self.task_revision, "task_revision"))
        object.__setattr__(self, "judge_epoch",
                           _require_nonneg_int(self.judge_epoch, "judge_epoch"))
        # 构造期兜底：直接构造 TaskSpec 也走同一份推导，不留第二套默认。
        scene, scene_origin = derive_scene_fields({
            "scene": self.scene, "scene_origin": self.scene_origin,
            "schema_version": self.schema_version})
        strength_scene, strength, origin = derive_strength_fields({
            "scene": scene, "strength": self.strength,
            "strength_origin": self.strength_origin,
            "output_policy": dict(self.output_policy),
            "schema_version": self.schema_version})
        object.__setattr__(self, "scene",
                           _require_enum(strength_scene or scene, SCENES, "scene"))
        object.__setattr__(self, "scene_origin",
                           _require_enum(scene_origin, SCENE_ORIGINS, "scene_origin"))
        object.__setattr__(self, "strength",
                           _require_enum(strength, STRENGTHS, "strength"))
        object.__setattr__(self, "strength_origin",
                           _require_enum(origin, STRENGTH_ORIGINS, "strength_origin"))
        object.__setattr__(self, "runtime_identity",
                           dict(self.runtime_identity or {}))
        # 运行件身份（补正方案 5.2）：必须由 runtime_identity() 产生并进入
        # task_digest——stage/origin/版本/包摘要缺一不可，空身份不得进入授权绑定。
        identity = self.runtime_identity
        if not identity:
            raise ValueError("runtime_identity 必须由 v2_task.runtime_identity() "
                             "产生：空身份不能进入任务授权绑定")
        missing = {"origin", "skill_version", "release_stage",
                   "package_digest"} - set(identity)
        if missing:
            raise ValueError(f"runtime_identity 缺字段: "
                             f"{','.join(sorted(missing))}")
        _require_enum(identity["release_stage"], RELEASE_STAGES,
                      "runtime_identity.release_stage")
        _require_nonempty_str(identity["origin"], "runtime_identity.origin")
        _require_nonempty_str(identity["skill_version"],
                              "runtime_identity.skill_version")
        _require_hex(identity["package_digest"],
                     "runtime_identity.package_digest")
        if self.voice != "strict":
            raise ValueError(f"voice 钉死 strict，不接受 {self.voice!r}")
        if self.backup is not True:
            raise ValueError("backup 钉死 True：所有正文修改必须可回滚")
        # 只检查不得绑定可写正文规格（补正方案 3.4）。
        if self.choices.mode == "check" and self.output_policy["write_body"]:
            raise ValueError("mode=check 与 output_policy.write_body=True 冲突："
                             "只检查不写正文")
        # 摘要唯一实现：声明摘要必须等于任务内容摘要（补正方案 3.2）。
        expected = compute_task_digest(content_of_task_spec(self))
        if self.request_digest != expected:
            raise ValueError(
                f"request_digest 与任务内容不符：声明 {self.request_digest[:12]}…"
                f" ≠ 计算 {expected[:12]}…（改内容必须换摘要）")


def validate_task_spec(value: dict) -> TaskSpec:
    """从 JSON 字典严格构造 TaskSpec：未知字段拒绝，必需字段缺失拒绝。"""
    _freeze_keys(value, ("task_id", "source_files", "choices", "judge_ref",
                         "model_version", "rule_version", "prompt_version",
                         "output_policy", "request_digest", "schema_version",
                         "task_revision", "judge_epoch", "runtime_identity",
                         "voice", "backup", "strength", "strength_origin",
                         "scene", "scene_origin"),
                 "TaskSpec")
    for key in ("task_id", "source_files", "choices", "judge_ref",
                "model_version", "rule_version", "prompt_version",
                "output_policy", "request_digest", "schema_version"):
        if key not in value:
            raise ValueError(f"TaskSpec 缺必需字段 {key}")
    choices = value["choices"]
    if not isinstance(choices, RequestChoices):
        choices = RequestChoices.from_mapping(choices)
    return TaskSpec(
        task_id=value["task_id"], source_files=tuple(value["source_files"]),
        choices=choices, judge_ref=dict(value["judge_ref"]),
        model_version=value["model_version"], rule_version=value["rule_version"],
        prompt_version=value["prompt_version"],
        output_policy=dict(value["output_policy"]),
        request_digest=value["request_digest"],
        schema_version=value["schema_version"],
        task_revision=value.get("task_revision", 0),
        judge_epoch=value.get("judge_epoch", 0),
        runtime_identity=value.get("runtime_identity") or {},
        voice=value.get("voice", "strict"), backup=value.get("backup", True),
        strength=value.get("strength"),
        strength_origin=value.get("strength_origin"), scene=value.get("scene"),
        scene_origin=value.get("scene_origin"))


# ---------------------------------------------------------------- CredentialReceipt

@dataclass(frozen=True)
class CredentialReceipt:
    """可信凭据验证结果（补正方案 3.3）：只能由受信任 Provider 通路产生。

    只保存 credential_ref 引用与验证元数据，**不存 Key 明文**；用户事件的
    ``credential_ok`` 不能伪造本类型——验证是验证器的事实，不是用户可声明的
    事实。provider/model 绑定当前裁判，换裁判即失效（require_ready 核对）。
    alpha.1 未接真实 Jev 通路（P09）：本类型供状态门与桩测试消费，
    不代表实际联通。
    """

    provider: str
    model: str
    credential_ref: str
    validation_id: str
    verified: bool
    runtime_identity: Mapping | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider",
                           _require_nonempty_str(self.provider, "provider"))
        object.__setattr__(self, "model",
                           _require_nonempty_str(self.model, "model"))
        object.__setattr__(self, "credential_ref",
                           _require_nonempty_str(self.credential_ref,
                                                 "credential_ref"))
        object.__setattr__(self, "validation_id",
                           _require_nonempty_str(self.validation_id,
                                                 "validation_id"))
        if not isinstance(self.verified, bool):
            raise ValueError(f"verified 必须是布尔: {self.verified!r}")
        object.__setattr__(self, "runtime_identity",
                           dict(self.runtime_identity or {}))

    @classmethod
    def from_mapping(cls, value: Mapping) -> "CredentialReceipt":
        _freeze_keys(value, ("provider", "model", "credential_ref",
                             "validation_id", "verified", "runtime_identity"),
                     "CredentialReceipt")
        for key in ("provider", "model", "credential_ref", "validation_id",
                    "verified"):
            if key not in value:
                raise ValueError(f"CredentialReceipt 缺必需字段 {key}")
        return cls(provider=value["provider"], model=value["model"],
                   credential_ref=value["credential_ref"],
                   validation_id=value["validation_id"],
                   verified=value["verified"],
                   runtime_identity=value.get("runtime_identity") or {})


# ---------------------------------------------------------------- SessionRecord

@dataclass(frozen=True)
class SessionRecord:
    """交互会话记录：附着现有 job，不建第二个任务账本（设计 6 节）。

    judge_epoch 是裁判阶段号：用户显式换裁判时 +1，未验证候选与旧裁判缓存
    随之失效（设计 7 节、P13）；0 起步，不允许负数。

    补正方案 3.1 扩展：``choices`` 保存当前**显式确认**的实际选择；
    ``task_revision``/``confirmed_revision`` 绑定确认针对的任务内容版本；
    ``credential_receipt`` 只引用可信验证器结果（Q02 接语义），不由用户
    事件伪造。旧开发会话缺这些字段时不得自动继承 READY（显式迁移）。
    """

    task_id: str
    interaction_state: str
    explicit_sources: tuple[str, ...]
    summary_digest: str | None
    authorization_event_refs: tuple[str, ...]
    judge_epoch: int
    choices: RequestChoices | None = None
    task_revision: int = 0
    confirmed_revision: int | None = None
    credential_receipt: "CredentialReceipt | None" = None
    #: 任务摘要**在哪个回合被真正展示给用户**（G2 补正）。展示与确认必须分属
    #: 两个回合：``presented_revision`` 是展示时对应的任务版本，
    #: ``presented_request_id`` 是那次展示所属的用户请求 id。二者皆 None 表示
    #: 从未展示过——此时任何 ``task_confirmed`` 事件都不算确认。
    presented_revision: int | None = None
    presented_request_id: str | None = None
    #: 交到用户手上的**那一份摘要文本**的 SHA-256：确认必须绑定它，
    #: 而不是只绑定「版本相同 + 换了个 request_id」。
    presented_summary_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id",
                           _require_nonempty_str(self.task_id, "task_id"))
        object.__setattr__(self, "interaction_state",
                           _require_enum(self.interaction_state,
                                         INTERACTION_STATES, "interaction_state"))
        sources = tuple(self.explicit_sources)
        for item in sources:
            _require_nonempty_str(item, "explicit_sources")
        object.__setattr__(self, "explicit_sources", sources)
        if self.summary_digest is not None:
            object.__setattr__(self, "summary_digest",
                               _require_hex(self.summary_digest,
                                            "summary_digest"))
        refs = tuple(self.authorization_event_refs)
        for item in refs:
            _require_nonempty_str(item, "authorization_event_refs")
        object.__setattr__(self, "authorization_event_refs", refs)
        object.__setattr__(self, "judge_epoch",
                           _require_nonneg_int(self.judge_epoch, "judge_epoch"))
        choices = self.choices
        if choices is not None and not isinstance(choices, RequestChoices):
            if not isinstance(choices, Mapping):
                raise ValueError(f"choices 必须是 RequestChoices 或映射: "
                                 f"{choices!r}")
            choices = RequestChoices.from_mapping(choices)
            object.__setattr__(self, "choices", choices)
        object.__setattr__(self, "task_revision",
                           _require_nonneg_int(self.task_revision,
                                               "task_revision"))
        if self.confirmed_revision is not None:
            object.__setattr__(self, "confirmed_revision",
                               _require_nonneg_int(self.confirmed_revision,
                                                   "confirmed_revision"))
        if self.credential_receipt is not None:
            receipt = self.credential_receipt
            if not isinstance(receipt, CredentialReceipt):
                if not isinstance(receipt, Mapping):
                    raise ValueError(
                        "credential_receipt 必须是 CredentialReceipt 或映射: "
                        f"{receipt!r}")
                receipt = CredentialReceipt.from_mapping(receipt)
            object.__setattr__(self, "credential_receipt", receipt)


# ---------------------------------------------------------------- CandidateRecord

@dataclass(frozen=True)
class CandidateRecord:
    """候选：由发现层产生、只描述疑点，不携带任何修改授权（设计 5 节）。

    source_anchor 复用 ``novel_contracts.SourceAnchor``（不另造同义类型）；
    retrieval_score 可空——结构规则候选没有相似度分数，不得编 0 充数。
    """

    candidate_id: str
    rule_id: str
    kind: str
    source_anchor: SourceAnchor
    context_anchors: tuple[SourceAnchor, ...]
    discovery_source: str
    source_hash: str
    context_hash: str
    retrieval_score: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id",
                           _require_nonempty_str(self.candidate_id, "candidate_id"))
        object.__setattr__(self, "rule_id",
                           _require_nonempty_str(self.rule_id, "rule_id"))
        object.__setattr__(self, "kind", _require_nonempty_str(self.kind, "kind"))
        if not isinstance(self.source_anchor, SourceAnchor):
            raise ValueError("source_anchor 必须是 novel_contracts.SourceAnchor")
        anchors = tuple(self.context_anchors)
        for item in anchors:
            if not isinstance(item, SourceAnchor):
                raise ValueError("context_anchors 每项必须是 SourceAnchor")
        object.__setattr__(self, "context_anchors", anchors)
        object.__setattr__(self, "discovery_source",
                           _require_nonempty_str(self.discovery_source,
                                                 "discovery_source"))
        object.__setattr__(self, "source_hash",
                           _require_hex(self.source_hash, "source_hash"))
        object.__setattr__(self, "context_hash",
                           _require_hex(self.context_hash, "context_hash"))
        if self.retrieval_score is not None:
            if isinstance(self.retrieval_score, bool) or not isinstance(
                    self.retrieval_score, (int, float)):
                raise ValueError(f"retrieval_score 必须是数值或 None: "
                                 f"{self.retrieval_score!r}")
            object.__setattr__(self, "retrieval_score", float(self.retrieval_score))


def validate_candidate(value: dict) -> CandidateRecord:
    """从 JSON 字典严格构造 CandidateRecord（anchor 为 SourceAnchor 实例）。"""
    _freeze_keys(value, ("candidate_id", "rule_id", "kind", "source_anchor",
                         "context_anchors", "discovery_source", "source_hash",
                         "context_hash", "retrieval_score"), "CandidateRecord")
    for key in ("candidate_id", "rule_id", "kind", "source_anchor",
                "discovery_source", "source_hash", "context_hash"):
        if key not in value:
            raise ValueError(f"CandidateRecord 缺必需字段 {key}")
    return CandidateRecord(
        candidate_id=value["candidate_id"], rule_id=value["rule_id"],
        kind=value["kind"], source_anchor=value["source_anchor"],
        context_anchors=tuple(value.get("context_anchors") or ()),
        discovery_source=value["discovery_source"],
        source_hash=value["source_hash"], context_hash=value["context_hash"],
        retrieval_score=value.get("retrieval_score"))


# ---------------------------------------------------------------- JudgmentRecord

@dataclass(frozen=True)
class JudgmentRecord:
    """裁判对单个候选的分项判定（设计 7 节：窄问题、原子回答）。

    atomic_answers 是分项命题的真值/概率表；概率与 confidence 分别保存，
    多问题概率不得相乘凑置信度（P08 的 combine 政策管组合，本模块只锁 schema）。
    """

    candidate_id: str
    provider: str
    actual_model: str
    judge_epoch: int
    question_version: str
    atomic_answers: Mapping
    status: str
    evidence_anchors: tuple[SourceAnchor, ...]
    reason_claims: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id",
                           _require_nonempty_str(self.candidate_id, "candidate_id"))
        object.__setattr__(self, "provider",
                           _require_nonempty_str(self.provider, "provider"))
        object.__setattr__(self, "actual_model",
                           _require_nonempty_str(self.actual_model, "actual_model"))
        object.__setattr__(self, "judge_epoch",
                           _require_nonneg_int(self.judge_epoch, "judge_epoch"))
        object.__setattr__(self, "question_version",
                           _require_nonempty_str(self.question_version,
                                                 "question_version"))
        if not isinstance(self.atomic_answers, Mapping):
            raise ValueError("atomic_answers 必须是映射")
        object.__setattr__(self, "status",
                           _require_enum(self.status, JUDGMENT_STATUSES, "status"))
        anchors = tuple(self.evidence_anchors)
        for item in anchors:
            if not isinstance(item, SourceAnchor):
                raise ValueError("evidence_anchors 每项必须是 SourceAnchor")
        object.__setattr__(self, "evidence_anchors", anchors)
        claims = tuple(self.reason_claims)
        for item in claims:
            _require_nonempty_str(item, "reason_claims")
        object.__setattr__(self, "reason_claims", claims)


# ---------------------------------------------------------------- PatchRecord

@dataclass(frozen=True)
class PatchRecord:
    """一个具体写动作：old/new 文本 + 锚点 + 哈希 + 风险 + 验证结果（设计 8 节）。

    accepted_by 可空：预览接受发生在验证之后、提交之前；验证失败时该字段
    保持 None 且 patch 不得提交（P12）。
    """

    patch_id: str
    candidate_id: str
    source_anchor: SourceAnchor
    old_text: str
    new_text: str
    context_hash: str
    source_sha256: str
    risk: str
    operation: str
    validation: Mapping
    accepted_by: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "patch_id",
                           _require_nonempty_str(self.patch_id, "patch_id"))
        object.__setattr__(self, "candidate_id",
                           _require_nonempty_str(self.candidate_id, "candidate_id"))
        if not isinstance(self.source_anchor, SourceAnchor):
            raise ValueError("source_anchor 必须是 novel_contracts.SourceAnchor")
        for name in ("old_text", "new_text"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} 必须是字符串")
        object.__setattr__(self, "context_hash",
                           _require_hex(self.context_hash, "context_hash"))
        object.__setattr__(self, "source_sha256",
                           _require_hex(self.source_sha256, "source_sha256"))
        object.__setattr__(self, "risk", _require_enum(self.risk, PATCH_RISKS,
                                                       "risk"))
        object.__setattr__(self, "operation",
                           _require_enum(self.operation, PATCH_OPERATIONS,
                                         "operation"))
        if not isinstance(self.validation, Mapping):
            raise ValueError("validation 必须是映射")
        if self.accepted_by is not None:
            object.__setattr__(self, "accepted_by",
                               _require_nonempty_str(self.accepted_by,
                                                     "accepted_by"))


# ---------------------------------------------------------------- VerificationBundle

@dataclass(frozen=True)
class VerificationBundle:
    """一次验证的全部证据（设计 8 节）：机械层 + 现有复核类型 + 哈希 + stage。

    review_report / review_validation 直接持有 ``novel_review`` 的现有类型；
    发布判定仍用 ``PublicationDecision``（总工单），本模块不复制它。
    """

    mechanical_levels: tuple[str, ...]
    review_report: ReviewReport
    review_validation: ReviewValidation
    ledger_sha256: str
    source_sha256: str
    candidate_sha256: str
    stage: str

    def __post_init__(self) -> None:
        levels = tuple(self.mechanical_levels)
        for level in levels:
            _require_enum(level, MECHANICAL_LEVELS, "mechanical_levels")
        if len(set(levels)) != len(levels):
            raise ValueError("mechanical_levels 重复")
        object.__setattr__(self, "mechanical_levels", levels)
        if not isinstance(self.review_report, ReviewReport):
            raise ValueError("review_report 必须是 novel_review.ReviewReport")
        if not isinstance(self.review_validation, ReviewValidation):
            raise ValueError("review_validation 必须是 "
                             "novel_review.ReviewValidation")
        object.__setattr__(self, "ledger_sha256",
                           _require_hex(self.ledger_sha256, "ledger_sha256"))
        object.__setattr__(self, "source_sha256",
                           _require_hex(self.source_sha256, "source_sha256"))
        object.__setattr__(self, "candidate_sha256",
                           _require_hex(self.candidate_sha256, "candidate_sha256"))
        object.__setattr__(self, "stage", _require_enum(self.stage,
                                                        RELEASE_STAGES, "stage"))


# ---------------------------------------------------------------- CommitRecord

@dataclass(frozen=True)
class CommitRecord:
    """单文件提交记录（设计 9 节）：同一 job 的 files 状态按文件登记。

    多文件任务不宣称一次原子交换——每文件一条 CommitRecord，进度逐文件可见，
    整任务回滚（P13）逐文件安全恢复。
    """

    file: str
    before_sha256: str
    after_sha256: str
    backup_ref: str
    stage: str
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "file", _require_nonempty_str(self.file, "file"))
        object.__setattr__(self, "before_sha256",
                           _require_hex(self.before_sha256, "before_sha256"))
        object.__setattr__(self, "after_sha256",
                           _require_hex(self.after_sha256, "after_sha256"))
        object.__setattr__(self, "backup_ref",
                           _require_nonempty_str(self.backup_ref, "backup_ref"))
        object.__setattr__(self, "stage", _require_enum(self.stage,
                                                        RELEASE_STAGES, "stage"))
        object.__setattr__(self, "status", _require_enum(self.status,
                                                         COMMIT_STATUSES,
                                                         "status"))


# ---------------------------------------------------------------- 外部依赖校验

def validate_external_imports(names: list[str]) -> list[str]:
    """对照 runtime_contract.EXTERNAL_DEPENDENCIES 权威登记表检查 import 名。

    返回违规条目列表，每条形如 ``undeclared_external_dependency:<依赖名>``；
    登记表内的名字不产生条目。登记表本身随任务维护在 runtime_contract.py
    （总工单全局约束 10），本函数只读不写，禁止用扩大登记表的方式掩盖缺件
    ——登记表变更必须与实际安装验证同任务发生。
    """
    from runtime_contract import EXTERNAL_DEPENDENCIES
    violations: list[str] = []
    for name in names:
        if name not in EXTERNAL_DEPENDENCIES:
            violations.append(f"undeclared_external_dependency:{name}")
    return violations
