#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""novel_output.py - 安全交付合同：no-clobber 另存与受控覆盖（v1.2 Task 12）。

设计方案 82–88 行「安全交付合同」与交接 §9 的可执行入口。**所有落盘字节都交给
``scripts/file_commit.py``**（全仓唯一写者），本模块只做命名、门禁、任务状态与恢复：

  1. 默认另存 ``<stem>.去AI味-<中文力度>.<ext>``；冲突时追加 ``-2``、``-3`` 递增序号，
     **绝不覆盖任何既有输出**；字节先写同卷临时文件并 fsync，再用硬链接独占挂名
     （设计方案 86 行），挂名失败才推进下个序号——没有 check-then-act 窗口，
     也不会在交付名上留下半个文件。
  2. 候选按原编码与换行策略生成的字节与原稿**完全相同** → 返回 ``NO-OP``，一个文件都不创建。
  3. 平台兑现不了「目标存在即失败」的原子创建时**安全停止**（``no_clobber_unsupported``），
     不降级成普通覆盖。
  4. 发布要求 L0–L3 全部通过（``publication_decision``）；FAIL/REVIEW/ERROR 一律不发布。
     证据文件必须带着本次的源哈希、候选哈希、场景与力度，对不上就是陈旧证据。
  5. 另存与覆盖**共用同一份保护合同**（先修问题第 8 项）：同一套保护项核实、同一条
     整段删除授权要求，覆盖通道不许多一道也不许少一道闸。Task 8 移交的放行交叉核对
     落在这里（``_release_problems``）：被授权放行的保护项必须真能对上一个原稿段落，
     且那段确实不在成稿里。
  6. 覆盖固定顺序（交接 §9）：**任务锁 → 初始哈希 → 排他唯一备份 → 同目录临时文件 fsync
     → 替换前复查目标哈希 → 保留权限原子替换 → 复读验证 → 必要时从备份回滚**。
     第 1 步在本模块，第 2–7 步在 ``file_commit.overwrite_atomically``。
  7. 混合换行的源文件默认只另存；**直接覆盖必须交出用户原话**，而且是引号包起来的
     原话（``require_user_utterance``），不接受任意非空串——Task 5 移交的账。
  8. ``ends_with_newline`` 在这里有真消费方：结尾换行被增删等于整篇尾部被动过，拦。
  9. **两条通道**的交付落盘后都先把输出名与目标哈希推进到任务状态（published），**再**清理
     临时候选；入册失败时候选原地保留（恢复材料不能先被删掉）。恢复时按目标哈希认出这次
     交付并幂等收尾，绝不产出第二份同名副本或第二份备份。
     任务锁与状态闸在**一切实测之前**（设计方案 88 行的第 1 步就是锁）：推进状态必须是锁
     持有者，锁被清过先 ``resume_job``，``refuse`` 就不发布；已 published 的任务直接按
     记录的目标哈希收尾，此时候选已被清理，所以这道闸不能排在候选读取之后。
 10. 源哈希重导、重解析点/符号链接拒绝、磁盘空间、同卷临时文件、Windows 保留设备名与
     路径长度，全都在动手之前查完。

账本摘要复用 :func:`novel_review.ledger_digest`（唯一实现，``fiction-zh.md`` §8 记账），
本模块不另抄一份哈希算法。
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import file_commit                                     # noqa: E402
import novel_contracts                                 # noqa: E402
import novel_job                                       # noqa: E402
import novel_review                                    # noqa: E402
import novel_validation                                # noqa: E402
import text_source                                     # noqa: E402
from novel_contracts import ContractError              # noqa: E402
from novel_review import ledger_digest                 # noqa: E402  账本摘要唯一实现

#: 交付状态词表：与 ``PublicationDecision`` / ``ValidationResult`` 的枚举一致。
PUBLISH_STATUSES: tuple[str, ...] = ("PASS", "NO-OP", "REVIEW", "FAIL", "ERROR")
CHANNEL_SAVE_AS = text_source.CHANNEL_SAVE_AS
CHANNEL_OVERWRITE = text_source.CHANNEL_OVERWRITE
CHANNELS: tuple[str, ...] = (CHANNEL_SAVE_AS, CHANNEL_OVERWRITE)
STRENGTH_LABELS = {"conservative": "保守", "balanced": "平衡", "deep": "深度"}
SCENES: tuple[str, ...] = ("ordinary", "novel")
DELIVERY_INFIX = "去AI味"
MAX_OUTPUT_ORDINAL = 9999            # 与 novel_job 的交付名形状 (?:[.-][0-9]{1,4}) 对齐
#: 覆盖顺序的可读回显（第 1 步是任务锁，由本模块负责）。
OVERWRITE_STEPS = file_commit.OVERWRITE_STEPS

VALIDATION_REPORT_SCHEMA = "lieflat-validation-report-v1"
PROTECTION_MANIFEST_SCHEMA = "lieflat-protection-manifest-v1"
REVIEW_REPORT_SCHEMA = "lieflat-review-report-v1"

# ---- 判定码：进 reasons 与 CLI 输出，不进正文 --------------------------------
CODE_BAD_REQUEST = "bad_request"
CODE_SOURCE_UNREADABLE = "source_unreadable"
CODE_SOURCE_HASH = "source_hash_mismatch"
CODE_CANDIDATE_UNREADABLE = "candidate_unreadable"
CODE_EMPTY_CANDIDATE = "empty_candidate"
CODE_TRAILING_NEWLINE_CHANGED = "trailing_newline_changed"
CODE_MIXED_NEWLINE = "mixed_newline_overwrite_unauthorized"
CODE_EVIDENCE_SCHEMA = "evidence_schema_invalid"
CODE_EVIDENCE_BINDING = "evidence_binding_mismatch"
CODE_EVIDENCE_UNREADABLE = "evidence_unreadable"
CODE_LEDGER_DIGEST = "ledger_digest_mismatch"
CODE_REVIEW_LEVELS_INCOMPLETE = "review_levels_incomplete"
CODE_SEGMENT_COVERAGE = "segment_coverage_incomplete"
#: 剧情敏感 certain 条目缺「绑定本次交付」的第二次语义复核事实。判定码沿用
#: ``novel_contracts``/``novel_validation`` 已有的那一个，本模块不新造判据也不改类别。
CODE_SECOND_REVIEW = "plot_sensitive_needs_second_review"
CODE_RELEASE_UNVERIFIED = "released_protection_unverified"
CODE_DISK_SPACE = "disk_space_low"
CODE_DIFFERENT_VOLUME = "temp_file_not_on_source_volume"
CODE_NO_CLOBBER_UNSUPPORTED = "no_clobber_unsupported"
CODE_NAME_REJECTED = "output_name_rejected"
CODE_PATH_TOO_LONG = "output_path_too_long"
CODE_WRITE_FAILED = "output_write_failed"
CODE_TARGET_DRIFT = "target_changed_before_replace"
CODE_LOCK_NOT_HELD = "job_lock_not_held"
CODE_JOB_STATE = "job_state_not_finalised"
CODE_DELIVERY_MISSING = "recorded_delivery_missing"
#: 状态没走到 reviewed 就想动手：写前闸门，正文与目录都不该被碰一下。
CODE_JOB_NOT_PUBLISHABLE = "job_not_publishable"
#: 交付事实记不进事件层：文件可能已经写成，但恢复无从认领，绝不许当成提交成功。
CODE_DELIVERY_LEDGER = "delivery_ledger_not_recorded"
#: 只有走到这一步的任务才有可发布的复核结论（published 走幂等认领，另算一路）。
PUBLISHABLE_STATE = "reviewed"
CODE_CANDIDATE_CLEANUP = "candidate_cleanup_failed"
CODE_INTERNAL = "publish_internal_error"

INFO_MIXED_NEWLINE = "mixed_newline_unified"
INFO_PROTECTION_RELEASED = "protection_released_by_authorization"
#: 平台局限的如实回执（不是拒绝，也不影响 PASS）：Windows 无法 fsync 目录项。
INFO_DIRECTORY_FSYNC = "directory_fsync_unavailable"

STATUS_ORDER = ("ERROR", "FAIL", "REVIEW", "NO-OP", "PASS")
_UTTERANCE_PAIRS = (("“", "”"), ("「", "」"), ("『", "』"), ('"', '"'))
MAX_UTTERANCE_CHARS = 200


class PublishError(RuntimeError):
    """发布层的拒绝：``code`` 给机器读，``message`` 给人读，都不含正文。"""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details)

    def to_dict(self) -> dict:
        return {"code": self.code, "detail": self.message}


@dataclass(frozen=True)
class CommitRequest:
    source: Path
    scene: Literal["ordinary", "novel"]
    strength: Literal["conservative", "balanced", "deep"]
    expected_source_sha256: str
    candidate: Path
    validation_report: Path
    protection_manifest: Path
    review_report: Path


@dataclass(frozen=True)
class PublishResult:
    status: Literal["PASS", "NO-OP", "REVIEW", "FAIL", "ERROR"]
    path: Path | None
    sha256: str | None
    backup_path: Path | None


# ---------------------------------------------------------------------------
# 交付名
# ---------------------------------------------------------------------------
def strength_label(strength: str) -> str:
    try:
        return STRENGTH_LABELS[strength]
    except KeyError:
        raise PublishError(CODE_BAD_REQUEST,
                           f"力度 {strength!r} 不在规范集合里，只能是 "
                           f"{' / '.join(STRENGTH_LABELS)}") from None


def default_output_name(source, strength: str) -> str:
    """``<stem>.去AI味-<中文力度>.<ext>``（设计方案 82 行的原样形状）。"""
    path = Path(source)
    return f"{path.stem}.{DELIVERY_INFIX}-{strength_label(strength)}{path.suffix}"


def conflicted_output_name(source, strength: str, ordinal: int) -> str:
    """冲突递增：``<stem>.去AI味-<中文力度>-2.<ext>``，序号从 2 起。"""
    if not isinstance(ordinal, int) or ordinal < 2 or ordinal > MAX_OUTPUT_ORDINAL:
        raise PublishError(CODE_BAD_REQUEST,
                           f"冲突序号必须是 2–{MAX_OUTPUT_ORDINAL} 的整数，收到 {ordinal!r}")
    path = Path(source)
    return (f"{path.stem}.{DELIVERY_INFIX}-{strength_label(strength)}"
            f"-{ordinal}{path.suffix}")


def output_name_gate(path) -> None:
    """输出路径自己的闸门：Windows 保留设备名与 MAX_PATH 长度。

    保留名判定复用 Task 5 的 :mod:`text_source`（同一份规则，不抄第二份），长度上限
    也一样；名字不合规就地拒绝，绝不「换个名字接着写」。
    """
    target = Path(path)
    try:
        text_source._name_gate(target)          # 单一实现就在 text_source
    except text_source.SourceInputError as exc:
        raise PublishError(CODE_NAME_REJECTED, str(exc), path=str(target)) from exc
    if not target.name or Path(target.name).name != target.name:
        raise PublishError(CODE_NAME_REJECTED,
                           f"输出必须是一个裸文件名（不含目录分隔）: {target}",
                           path=str(target))
    if len(str(target)) > text_source.WINDOWS_MAX_PATH:
        raise PublishError(CODE_PATH_TOO_LONG,
                           f"输出路径长度 {len(str(target))} 超过 Windows MAX_PATH "
                           f"{text_source.WINDOWS_MAX_PATH}，请把成稿放到更浅的目录: {target}",
                           path=str(target), length=len(str(target)))


def require_user_utterance(text) -> str:
    """混合换行覆盖授权必须是一段**被引号包起来的用户原话**，不是任意非空串。

    Task 5 移交：那边只挡住了「空串与非空串无区别」。这里要求的是一条能被归档的
    原话引用：成对引号、内部有内容、≤200 字符、不含控制字符。
    """
    value = str(text or "").strip()
    if not value:
        raise PublishError(CODE_MIXED_NEWLINE,
                           "混合换行的源文件直接覆盖需要用户授权的原话，不能为空")
    if len(value) > MAX_UTTERANCE_CHARS:
        raise PublishError(CODE_MIXED_NEWLINE,
                           f"授权原话长度 {len(value)} 超过上限 {MAX_UTTERANCE_CHARS} 字符："
                           "要的是用户那一句，不是一整段说明")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise PublishError(CODE_MIXED_NEWLINE, "授权原话里不得含控制字符或换行")
    for opening, closing in _UTTERANCE_PAIRS:
        if value.startswith(opening) and value.endswith(closing):
            inner = value[1:-1].strip()
            if not inner:
                raise PublishError(CODE_MIXED_NEWLINE,
                                   f"授权原话只有一对引号（{opening}{closing}），"
                                   "里面没有任何用户说过的话")
            return inner
    raise PublishError(CODE_MIXED_NEWLINE,
                       "授权必须是用户原话的引用（用引号包起来的那句话），"
                       "不接受 ok / 同意 / 随便一个非空字符串")


# ---------------------------------------------------------------------------
# 证据读取
# ---------------------------------------------------------------------------
def _load_json(path, schema: str) -> dict:
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PublishError(CODE_EVIDENCE_UNREADABLE,
                           f"证据文件读不出来，拒绝据此发布: {target}（{exc}）",
                           path=str(target)) from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise PublishError(CODE_EVIDENCE_SCHEMA, f"证据不是合法 JSON: {target}（{exc}）",
                           path=str(target)) from exc
    if not isinstance(doc, dict) or doc.get("schema") != schema:
        raise PublishError(CODE_EVIDENCE_SCHEMA,
                           f"证据 schema 应为 {schema}，实际 {doc.get('schema') if isinstance(doc, dict) else type(doc).__name__}: {target}",
                           path=str(target), expected=schema)
    return doc


def _check_binding(doc: dict, label: str, snapshot, candidate_sha: str, request) -> None:
    for field, expected in (("source_sha256", snapshot.sha256),
                           ("candidate_sha256", candidate_sha),
                           ("scene", request.scene), ("strength", request.strength)):
        got = str(doc.get(field) or "").lower()
        want = str(expected or "").lower()
        if got != want:
            raise PublishError(CODE_EVIDENCE_BINDING,
                               f"{label} 的 {field} 与本次交付不符"
                               f"（记录 {got[:12] or '—'}… / 本次 {want[:12]}…）："
                               "这是别次的证据，拒绝据此发布",
                               field=field, label=label)


def _segments_from(records, label: str):
    built = []
    for index, record in enumerate(records or ()):
        if not isinstance(record, Mapping):
            raise PublishError(CODE_EVIDENCE_SCHEMA,
                               f"{label} 第 {index + 1} 项不是对象")
        try:
            built.append(novel_review.Segment(**dict(record)))
        except TypeError as exc:
            raise PublishError(CODE_EVIDENCE_SCHEMA,
                               f"{label} 第 {index + 1} 项字段与分段合同不符: {exc}") from exc
    return tuple(built)


def _recorded_result(doc: dict, label: str, level: str):
    record = doc.get(level.lower())
    if not isinstance(record, dict):
        raise PublishError(CODE_EVIDENCE_SCHEMA, f"{label} 缺 {level} 记录")
    status = str(record.get("status") or "")
    codes = tuple(str(code) for code in (record.get("codes") or ()))
    if status not in ("PASS", "NO-OP", "REVIEW", "FAIL", "ERROR"):
        raise PublishError(CODE_EVIDENCE_SCHEMA,
                           f"{label} 的 {level} 状态非法: {status!r}")
    if status in ("REVIEW", "FAIL") and not codes:
        raise PublishError(CODE_EVIDENCE_SCHEMA,
                           f"{label} 的 {level} 判 {status} 却没交出任何判定码，"
                           "无法复核，按不可信处理")
    try:
        return novel_validation.ValidationResult(status=status, level=level, codes=codes)
    except ValueError as exc:
        raise PublishError(CODE_EVIDENCE_SCHEMA, str(exc)) from exc


def _recorded_review(doc: dict):
    record = doc.get("review")
    if not isinstance(record, dict):
        raise PublishError(CODE_EVIDENCE_SCHEMA, "复核报告缺 review 记录")
    status = str(record.get("status") or "")
    codes = tuple(str(code) for code in (record.get("codes") or ()))
    if status == "PASS":
        built = novel_review.ReviewValidation()
    elif status == "FAIL":
        built = novel_review.ReviewValidation(
            rejected=tuple(novel_contracts.ContractIssue(index, code, code, "复核报告记录的拒绝项")
                           for index, code in enumerate(codes)))
    elif status == "REVIEW":
        built = novel_review.ReviewValidation(
            review=tuple(novel_contracts.ContractIssue(index, code, code, "复核报告记录的复核项")
                         for index, code in enumerate(codes)))
    else:
        raise PublishError(CODE_EVIDENCE_SCHEMA,
                           f"复核报告的 review 状态非法: {status!r}（只能是 PASS/REVIEW/FAIL）")
    if built.status != status:
        raise PublishError(CODE_EVIDENCE_SCHEMA,
                           f"复核报告判 {status} 却给不出可复核的判定码，按不可信处理")
    return built


#: 一条复核事实要齐的出处字段：少任何一项都当「没复核过」，不默认放行。
#: ``note`` 在列＝写了 confirmed 却没交出任何依据文字的不算复核过（与投影层同口径）。
SECOND_REVIEW_FIELDS = ("stage", "rule_version", "prompt_version", "judge_provider",
                        "judge_model", "independence", "artifact", "note")


def _second_review_gaps(ledger, manifest, *, source_sha: str, candidate_sha: str,
                        ledger_sha: str) -> tuple:
    """发布复验真读保护清单里的第二次语义复核事实。

    G7 之前 manifest 那一栏没有任何消费者：写了等于没写。现在它是一道真门 ——
    只认「判定 confirmed ＋ 字节绑定就是这份原稿与这份候选 ＋ 账本摘要对得上 ＋
    出处字段齐备」的事实；换候选、旧账本、判定是 failed/unknown 一律算缺证据。
    类别表与降级判定仍归 ``novel_contracts``，这里不重复实现也不放宽。
    """
    accepted: set = set()
    for item in (manifest.get("second_semantic_review") or ()):
        if not isinstance(item, dict):
            continue
        claim_id = str(item.get("claim_id") or "").strip()
        bindings = item.get("bytes_bindings") or {}
        try:
            epoch_ok = int(item.get("judge_epoch")) >= 0
        except (TypeError, ValueError):
            epoch_ok = False
        independent = str(item.get("independence") or "")
        if (claim_id and str(item.get("verdict") or "") == "confirmed" and epoch_ok
                and str(bindings.get("source_sha256") or "").lower() == source_sha.lower()
                and str(bindings.get("candidate_sha256") or "").lower() == candidate_sha.lower()
                and str(item.get("ledger_sha256") or "").lower() == ledger_sha.lower()
                and independent in ("same_host", "external")
                and (independent == "same_host" or str(item.get("routing") or "").strip())
                and all(str(item.get(key) or "").strip() for key in SECOND_REVIEW_FIELDS)):
            accepted.add(claim_id)
    gaps = []
    for entry in ledger:
        if not isinstance(entry, dict):
            continue
        if (str(entry.get("category") or "")
                in novel_contracts.PLOT_SENSITIVE_CATEGORIES
                and str(entry.get("certainty") or "") == "certain"):
            claim_id = str(entry.get("claim_id") or "").strip()
            if claim_id not in accepted:
                gaps.append((claim_id, str(entry.get("category") or "")))
    return tuple(gaps)


# ---------------------------------------------------------------------------
# 保护合同（另存与覆盖共用）
# ---------------------------------------------------------------------------
def _release_problems(released, source_text: str, candidate_text: str) -> list:
    """Task 8 移交的放行交叉核对：授权段必须真的装着这条保护项，且那段真的没了。

    契约模块只认「保护项哈希 == 授权哈希」这一层，等于说**任何**一段被授权删除的字节
    序列都能放行保护项——跨两段拼出来的区间、段里的一行、或者根本没被删的段落都可能被
    当成合法释放。这一步在原稿段落表上重算，落不回去的一律拦住。
    """
    if not released:
        return []
    source_paragraphs = {novel_contracts.sha256_text(paragraph): paragraph
                         for paragraph in novel_contracts.split_paragraphs(source_text)}
    candidate_hashes = {novel_contracts.sha256_text(paragraph)
                        for paragraph in novel_contracts.split_paragraphs(candidate_text)}
    problems = []
    for entry in released:
        paragraph = source_paragraphs.get(entry.text_sha256)
        if paragraph is None:
            problems.append((CODE_RELEASE_UNVERIFIED,
                             f"保护项 {entry.entry_id} 被删除授权放行，但授权哈希对不上"
                             "原稿里的任何一个段落（整段删除只能授权整段）"))
            continue
        if entry.original_text not in paragraph:
            problems.append((CODE_RELEASE_UNVERIFIED,
                             f"保护项 {entry.entry_id} 的原文不在被授权的那一段里，放行不成立"))
        if entry.text_sha256 in candidate_hashes:
            problems.append((CODE_RELEASE_UNVERIFIED,
                             f"保护项 {entry.entry_id} 被授权的那一段其实还在成稿里，"
                             "放行与事实不符"))
    return problems


def _protection_contract(manifest: dict, segments, source_text: str,
                         candidate_text: str, reasons: list) -> str:
    """跑共用保护合同，返回 PASS / REVIEW / FAIL / ERROR；判定码全部进 ``reasons``。

    保护核实、删除授权、放行交叉核对**各自算完再合并**：任何一处提前 return 都会让
    后面的核对没跑，那道闸就等于不存在（Task 8 移交的账正是这样被吞掉的）。
    """
    statuses: list = []
    try:
        protection_result = novel_contracts.validate_protections(
            manifest.get("protections") or (), segments, output_text=candidate_text,
            authorizations=manifest.get("authorizations") or (),
            indistinguishable=manifest.get("indistinguishable") or ())
    except ContractError as exc:
        reasons.append({"code": CODE_EVIDENCE_SCHEMA, "detail": f"保护清单不可用: {exc}"})
        return "ERROR"
    for issue in tuple(protection_result.issues) + tuple(protection_result.rejected):
        reasons.append({"code": issue.code, "detail": issue.detail})
    statuses.append(protection_result.status)

    original_paragraphs = novel_contracts.split_paragraphs(source_text)
    output_paragraphs = novel_contracts.split_paragraphs(candidate_text)
    authorizations = tuple(manifest.get("authorizations") or ())
    # 与机械层同一口径：只有内容单元「数量」减少才要删除授权（改写不算删除）。
    if authorizations or len(output_paragraphs) < len(original_paragraphs):
        try:
            deletion = novel_contracts.check_deletions(original_paragraphs, output_paragraphs,
                                                       authorizations)
        except ContractError as exc:
            reasons.append({"code": CODE_EVIDENCE_SCHEMA,
                            "detail": f"删除授权不可用: {exc}"})
            return "ERROR"
        for issue in tuple(deletion.rejected) + tuple(deletion.issues):
            if issue.code == "deletion_without_authorization":
                continue      # 少了几段由下面的数量闸表达，不重复登记
            reasons.append({"code": issue.code, "detail": issue.detail})
        # 状态同样按这个口径算：改写让原段哈希「消失」不是删除，数量闸才说明少了段。
        pending_review = [item for item in deletion.review
                          if item.code != "deletion_without_authorization"]
        statuses.append("FAIL" if deletion.rejected else
                        ("REVIEW" if pending_review else "PASS"))

    delta = len(original_paragraphs) - len(output_paragraphs)
    if delta > 0 and len(authorizations) < delta:
        reasons.append({"code": "paragraph_missing_without_authorization",
                        "detail": f"成稿比原文少 {delta} 个内容单元，删除授权只有 "
                                  f"{len(authorizations)} 条，数量对不上"})
        statuses.append("REVIEW")

    problems = _release_problems(protection_result.released, source_text, candidate_text)
    for code, detail in problems:
        reasons.append({"code": code, "detail": detail})
    if problems:
        statuses.append("REVIEW")
    return _worst(statuses) if statuses else "PASS"


# ---------------------------------------------------------------------------
# 门禁汇总
# ---------------------------------------------------------------------------
def _worst(statuses) -> str:
    for status in STATUS_ORDER:
        if status in statuses:
            return status
    return "PASS"


def _volume_of(path) -> int:
    return os.lstat(str(path)).st_dev


def _plain(text: str) -> str:
    """把换行风格归一，只用于「这段文字还在不在」的比对，不改要落盘的字节。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _prewrite_checks(out_dir, source, payload: bytes, extra: int, reasons: list,
                     channel: str) -> bool:
    """磁盘空间与同卷临时文件：动手之前查完，不在半途留下写坏的东西。

    查的是**真正会落盘的那个目录**：另存在 ``out_dir``，覆盖在原稿自己所在的目录
    （``--output-dir`` 不改变覆盖通道的落盘点，拿它做检查既误拒又漏检）。
    """
    write_dir = Path(source).parent if channel == CHANNEL_OVERWRITE else Path(out_dir)
    if _volume_of(write_dir) != _volume_of(source):
        reasons.append({"code": CODE_DIFFERENT_VOLUME,
                        "detail": f"落盘目录 {write_dir} 与原稿 {Path(source).parent} 不在同一卷，"
                                  "临时文件与原子替换都要求同卷"})
        return False
    need = len(payload) * 2 + extra
    try:
        free = shutil.disk_usage(str(write_dir)).free
    except OSError as exc:
        reasons.append({"code": CODE_DISK_SPACE,
                        "detail": f"读不到输出目录的剩余空间，无法确认能写下 {len(payload)} 字节: {exc}"})
        return False
    if free < need:
        reasons.append({"code": CODE_DISK_SPACE,
                        "detail": f"剩余空间 {free} 字节不足以容纳成稿与同目录临时副本 "
                                  f"（需要约 {need} 字节），不发布"})
        return False
    # 「目标存在即失败」的原子能力两条通道都要查：另存靠它挂交付名，**覆盖靠它排他创建
    # 备份**（设计方案 88 行：备份名必须排他创建，避免两个进程选中同一个同名备份）。
    # 这条能力兑现不了时两条路都只能安全停止，不存在「那就直接写」的降级分支。
    probe = file_commit.SafeFileCommitEngine.probe_no_clobber(write_dir)
    if not probe.get("atomic_exclusive_create"):
        reasons.append({"code": CODE_NO_CLOBBER_UNSUPPORTED,
                        "detail": f"该目录无法提供「目标存在即失败」的原子独占创建"
                                  f"（覆盖通道靠它排他创建备份），"
                                  f"安全停止而不是降级覆盖：{probe.get('reason') or '探针未说明'}"})
        return False
    if channel == CHANNEL_SAVE_AS and not probe.get("atomic_link"):
        reasons.append({"code": CODE_NO_CLOBBER_UNSUPPORTED,
                        "detail": f"该目录无法提供「目标存在即失败」的原子挂名（硬链接），"
                                  f"安全停止而不是降级覆盖："
                                  f"{probe.get('reason') or '探针未说明'}"})
        return False
    if not probe.get("directory_fsync"):
        reasons.append({"code": INFO_DIRECTORY_FSYNC,
                        "detail": "该平台无法 fsync 目录项（Windows 实况）：文件字节已 "
                                  "fsync，崩溃时可能留下未同步的目录项；独占语义由 "
                                  "O_EXCL/CREATE_NEW 与 CreateHardLinkW 承担"})
    return True


def _sha256(raw: bytes) -> str:
    return file_commit._sha256(raw)


# ---------------------------------------------------------------------------
# 任务状态与锁（Task 6 移交的账）
# ---------------------------------------------------------------------------
def _read_job_lock(job_dir: Path):
    path = job_dir / novel_job.LOCK_NAME
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "corrupt"
    return record if isinstance(record, dict) else "corrupt"


def _cleanup_candidates(job_dir: Path, reasons: list) -> None:
    """成功后删临时候选、留清单/哈希/报告——只在状态已收尾之后调用。"""
    try:
        novel_job.cleanup_job(job_dir, keep_evidence=True)
    except novel_job.JobError as exc:
        reasons.append({"code": CODE_CANDIDATE_CLEANUP,
                        "detail": f"交付已完成，但临时候选清理失败：{exc}"})


def _finalize_job(job_dir: Path, name: str, digest: str, channel: str, reasons: list) -> None:
    """先把输出名与目标哈希推进 published，**再**清理候选（设计方案 86 行的顺序）。

    两条通道共用这一条收尾，没有「覆盖通道记不上就算了」的分支：覆盖的交付物就是原稿
    本身，Task 6 的入册闸门按「发布方声明了覆盖通道 + 名字等于清单里那份原稿」如实放行
    （``novel_job._publish_record``）。记不上就是真记不上，此刻**绝不清理候选**——候选是
    唯一能重建交付的原材料，宁可留着也不许删；清理失败只记账，不反向删已交付内容。
    """
    try:
        novel_job.advance_state(job_dir, "published", output_name=name, output_sha256=digest,
                                channel=channel)
    except novel_job.JobError as exc:
        reasons.append({"code": CODE_JOB_STATE,
                        "detail": f"成稿已落盘但任务状态没能收尾，临时候选原地保留："
                                  f"{exc}"})
        return
    _cleanup_candidates(job_dir, reasons)


def _recorded_source_name(job_dir: Path, delivered: Path, before_sha256: str) -> str:
    """覆盖通道入册该记哪个裸名。

    新任务的清单名就是盘上那份的真实拼写（F61 修好后两边本就相等）。F61 之前建的旧 job
    清单里记的名字被平台规范键折过大小写，那份交付要入册得三件同时成立：由清单
    ``dir_rel`` + ``name`` 复原出来的路径与本次交付的是**同一个真实文件**（按本平台路径
    规范比较）、且清单订约的源摘要等于本次覆盖前的字节摘要。任一不成立就把真实裸名交回
    原有闸门照旧拒——不拿近似名猜，也不为此放宽那条判据。
    """
    from pathlib import PurePosixPath

    real = Path(delivered).name
    try:
        record = novel_job.load_manifest(job_dir).get("source") or {}
    except (novel_job.JobError, OSError, ValueError):
        return real
    name = str(record.get("name") or "")
    dir_rel = str(record.get("dir_rel") or "")
    if not name or name == real or not dir_rel:
        return real
    derived = Path(job_dir).resolve() / PurePosixPath(dir_rel) / name
    same_file = (os.path.normcase(str(derived.resolve()))
                 == os.path.normcase(str(Path(delivered).resolve())))
    contracted = str(record.get("sha256") or "").lower()
    if same_file and contracted and contracted == str(before_sha256 or "").lower():
        return name
    return real


def _recorded_delivery(job_dir: Path, state: dict, out_dir: Path, reasons: list):
    """已发布 + 记录重推导通过 → 按目标哈希认出交付，幂等收尾（不产第二份）。"""
    output = state.get("output") or {}
    name, digest = str(output.get("name") or ""), str(output.get("sha256") or "")
    target = out_dir / name if name else None
    landed = None
    if target is not None:
        try:
            landed = target.is_file() and _sha256(target.read_bytes()) == digest
        except OSError as exc:              # 读不回等于认不回：宁可拒发布，也不据猜称重发
            reasons.append({"code": CODE_DELIVERY_MISSING,
                            "detail": f"记录里的交付 {target} 读不出字节，无法按目标哈希核实："
                                      f"{type(exc).__name__}: {exc}"})
            return PublishResult("ERROR", None, None, None)
    if landed:
        _cleanup_candidates(job_dir, reasons)
        return PublishResult("PASS", target, digest, None)
    reasons.append({"code": CODE_DELIVERY_MISSING,
                    "detail": f"任务状态记着已发布（{name or '无名'} / {digest[:12] or '—'}…），"
                              "但按目标哈希找不回那个文件；不会据此重新发布一份副本"})
    return PublishResult("ERROR", None, None, None)


def _job_gate(job_dir: Path, out_dir: Path, resume_identity, reasons: list):
    """返回 None 表示可以继续发布；返回 PublishResult 表示已就地收尾或被拒。"""
    try:
        state = novel_job.load_state(job_dir)
    except novel_job.JobError as exc:
        reasons.append({"code": CODE_JOB_STATE, "detail": str(exc)})
        return PublishResult("ERROR", None, None, None)
    if state.get("state") == "published":
        return _recorded_delivery(job_dir, state, out_dir, reasons)

    lock = _read_job_lock(job_dir)
    if lock == "corrupt":
        reasons.append({"code": CODE_LOCK_NOT_HELD,
                        "detail": f"任务锁记录读不出（{novel_job.LOCK_NAME} 已损坏），"
                                  "无法证明持有者，不发布"})
        return PublishResult("ERROR", None, None, None)
    if lock is None or lock.get("pid") != os.getpid():
        # advance_state 只认持有者：锁被清过（或归别人）必须先 resume，绝不抢发布
        if resume_identity is None:
            reasons.append({"code": CODE_LOCK_NOT_HELD,
                            "detail": "本进程不持有任务锁（锁不存在或归别的进程），"
                                      "且没有给出 resume_identity：拒绝发布"})
            return PublishResult("ERROR", None, None, None)
        try:
            verdict = novel_job.resume_job(job_dir, **dict(resume_identity))
        except novel_job.JobError as exc:
            reasons.append({"code": CODE_LOCK_NOT_HELD, "detail": str(exc)})
            return PublishResult("ERROR", None, None, None)
        if verdict == "complete":
            return _recorded_delivery(job_dir, novel_job.load_state(job_dir), out_dir, reasons)
        if verdict == "refuse":
            reasons.append({"code": CODE_LOCK_NOT_HELD,
                            "detail": "resume_job 拒绝接管这个任务（源/身份/锁证明不成立），"
                                      "不发布"})
            return PublishResult("ERROR", None, None, None)
    # 写前闸门：状态没走到 reviewed 就没有「已通过复核的成稿」，一个字节都不许动。
    # 「先把正文写好、再靠收尾异常报告半成品」不是零触碰——独立复核那份反例正是这样
    # 停在 created 上把原稿改掉了，重启后恢复面还看不见。
    current = novel_job.load_state(job_dir).get("state")
    if current != PUBLISHABLE_STATE:
        reasons.append({"code": CODE_JOB_NOT_PUBLISHABLE,
                        "detail": f"任务 {Path(job_dir).name} 当前状态是 {current!r}，"
                                  f"没走到 {PUBLISHABLE_STATE!r} 就没有可发布的复核结论："
                                  "拒绝动手，正文与目录一字节不改"})
        return PublishResult("ERROR", None, None, None)
    return None


def _record_delivery(job_dir: Path, *, path: Path, digest: str, channel: str,
                     out_dir: Path, source: Path, backup, delivered_text: str,
                     reasons: list):
    """把「实际写成哪个文件、备份在哪、字节与文本摘要」记进现有事件层。

    路径只由写者给出：另存的名字可能被冲突序号推进，交付目录也可能是显式给定的另一处。
    恢复据此认领，既不在外层猜默认名，也不扫目录挑长得像的文件。只有还欠着交付的那条
    意图才记账（同一意图的幂等重试不会留下第二行）。记不上就返回 False：文件可能已写成，
    但这条事实无从认领，调用方必须停止，不许当成提交成功。
    """
    intent = novel_job.pending_intent(job_dir)
    if intent is None:
        return True                     # 没有待交付的意图（例如 v1 CLI 自己发布）
    source_path = Path(source).resolve()
    resolved_out = Path(out_dir).resolve()
    record = {
        "intent_id": str(intent.get("intent_id") or ""),
        "file": str(source_path),
        "channel": channel,
        "delivered": str(Path(path).resolve()),
        "backup_ref": str(Path(backup).resolve()) if backup else "",
        "after_sha256": digest,
        "delivered_text_sha256": novel_contracts.sha256_text(delivered_text),
        "output_dir": "" if resolved_out == source_path.parent else str(resolved_out),
    }
    try:
        novel_job.record_commit_delivery(job_dir, record)
    except novel_job.JobError as exc:
        reasons.append({"code": CODE_DELIVERY_LEDGER,
                        "detail": f"成稿已落盘但交付事实记不上（{exc.code}），"
                                  "恢复无从认领：停止，不把它当成已提交"})
        return False
    return True


# ---------------------------------------------------------------------------
# 两条通道
# ---------------------------------------------------------------------------
def _publish_save_as(out_dir: Path, source: Path, strength: str, payload: bytes,
                     mode: int, reasons: list):
    """同卷临时文件 fsync 后独占挂名；撞名只推进下个序号，绝不覆盖、绝不留半成品。"""
    for ordinal in range(1, MAX_OUTPUT_ORDINAL + 1):
        name = (default_output_name(source, strength) if ordinal == 1
                else conflicted_output_name(source, strength, ordinal))
        path = out_dir / name
        output_name_gate(path)
        try:
            created = file_commit.SafeFileCommitEngine.commit_new_file(path, payload, mode)
        except file_commit.NoClobberUnavailable as exc:
            # 平台兑现不了原子挂名：停在第一个序号上，不降级成「那就直接覆盖吧」。
            reasons.append({"code": CODE_NO_CLOBBER_UNSUPPORTED,
                            "detail": f"安全停止，不是降级覆盖：{exc}"})
            return None
        except OSError as exc:
            reasons.append({"code": CODE_WRITE_FAILED,
                            "detail": f"临时文件写入或挂名失败：{type(exc).__name__}: {exc}"})
            return None
        if not created:
            continue                    # 已被别人占：只重试下个序号
        if path.read_bytes() != payload:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            reasons.append({"code": CODE_WRITE_FAILED,
                            "detail": "成稿复读与候选字节不符，已删除这份不完整输出"})
            return None
        return path
    reasons.append({"code": CODE_WRITE_FAILED,
                    "detail": f"冲突序号递增到 {MAX_OUTPUT_ORDINAL} 仍没有可用名字，停止"})
    return None


def _publish_overwrite(target: Path, payload: bytes, expected_sha: str, reasons: list):
    outcome = file_commit.SafeFileCommitEngine.overwrite_atomically(
        target, payload, expected_sha256=expected_sha)
    status = outcome.get("status")
    if status == "COMMITTED":
        return Path(target), str(outcome.get("backup_path")), outcome.get("target_sha256")
    if status == "NOOP":
        return "NOOP", None, None
    if status == "ABORTED_RACE":
        reasons.append({"code": CODE_TARGET_DRIFT,
                        "detail": f"覆盖被中止：{outcome.get('reason')}"})
    else:
        reasons.append({"code": CODE_WRITE_FAILED,
                        "detail": f"覆盖通道返回 {status}：{outcome.get('reason')}；"
                                  f"已执行步骤 {outcome.get('steps')}；"
                                  f"备份 {outcome.get('backup_path') or '未创建'}"})
    return None, None, None


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------
def publish(request: CommitRequest, *, channel: str = CHANNEL_SAVE_AS,
            output_dir=None, mixed_newline_authorization: str | None = None,
            job_dir=None, resume_identity: Mapping | None = None,
            reasons: list | None = None) -> PublishResult:
    """按安全交付合同发布一份成稿；``reasons`` 用来收集全部判定码与说明。

    ``channel="overwrite"`` 直接覆盖原稿（走七步固定顺序并留唯一备份）；默认的
    ``"save_as"`` 另存新文件。两条通道用同一份保护合同与同一套 L0–L3 门禁。
    """
    sink = reasons if reasons is not None else []
    try:
        return _publish(request, channel=channel, output_dir=output_dir,
                        mixed_newline_authorization=mixed_newline_authorization,
                        job_dir=job_dir, resume_identity=resume_identity, reasons=sink)
    except PublishError as exc:
        sink.append(exc.to_dict())
        return PublishResult("ERROR", None, None, None)
    except (TypeError, ValueError) as exc:      # 入参形状不对：不猜，直接拒
        sink.append({"code": CODE_BAD_REQUEST,
                     "detail": f"{type(exc).__name__}: {exc}"})
        return PublishResult("ERROR", None, None, None)


def _publish(request, *, channel: str, output_dir, mixed_newline_authorization,
             job_dir, resume_identity, reasons: list) -> PublishResult:
    if channel not in CHANNELS:
        raise PublishError(CODE_BAD_REQUEST,
                           f"发布通道只能是 {' / '.join(CHANNELS)}，收到 {channel!r}")
    if request.scene not in SCENES:
        raise PublishError(CODE_BAD_REQUEST, f"scene 只能是 {'/'.join(SCENES)}")
    if request.strength not in STRENGTH_LABELS:
        raise PublishError(CODE_BAD_REQUEST,
                           f"strength 只能是 {' / '.join(STRENGTH_LABELS)}")
    expected = str(request.expected_source_sha256 or "").lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise PublishError(CODE_BAD_REQUEST,
                           "expected_source_sha256 必须是 64 位十六进制源哈希，"
                           "不接受空串或随手填的占位值")

    out_dir = Path(output_dir) if output_dir is not None else Path(request.source).parent

    # 0) 任务锁与状态（= 设计方案 88 行覆盖顺序的第 1 步「源锁」）：先于一切实测。
    # 放在最前面有两层必要：①设计方案 88 行要求锁在初始哈希之前，锁不是持有者就
    # 一个字都不该往下写；②已收尾的任务（state=published）按**记录的目标哈希**认领交付，
    # 此时临时候选早已被清理，若等到落盘那一步才查状态，幂等恢复会因为读不到候选而报
    # 「候选不可读」——Task 6 交接的「按目标哈希幂等收尾」就永远走不到。
    if job_dir is not None:
        outcome = _job_gate(Path(job_dir), out_dir, resume_identity, reasons)
        if isinstance(outcome, PublishResult):
            return outcome

    # 1) 源：重解析点/编码/保留名等闸门全交给 text_source（同一份规则）
    try:
        snapshot, source_text = text_source.read_source(request.source)
    except text_source.SourceInputError as exc:
        reasons.append({"code": CODE_SOURCE_UNREADABLE,
                        "detail": f"[{exc.code}] {exc.message}", "gate": exc.code})
        return PublishResult("ERROR", None, None, None)
    if snapshot.sha256 != expected:
        reasons.append({"code": CODE_SOURCE_HASH,
                        "detail": f"源文件当前哈希 {snapshot.sha256[:12]}… 与请求声明的 "
                                  f"{expected[:12]}… 不符，拒绝据陈旧快照发布"})
        return PublishResult("ERROR", None, None, None)
    raw_source = snapshot.resolved_path.read_bytes()

    # 2) 候选字节与 NO-OP（设计方案 84 行）
    try:
        candidate_raw = Path(request.candidate).read_bytes()
    except OSError as exc:
        reasons.append({"code": CODE_CANDIDATE_UNREADABLE,
                        "detail": f"候选读不出来: {request.candidate}（{exc}）"})
        return PublishResult("ERROR", None, None, None)
    try:
        candidate_text, _encoding, _bom = text_source.decode_source_bytes(candidate_raw)
    except text_source.SourceInputError as exc:
        reasons.append({"code": CODE_CANDIDATE_UNREADABLE,
                        "detail": f"[{exc.code}] {exc.message}"})
        return PublishResult("ERROR", None, None, None)
    if not candidate_text.strip().lstrip("\ufeff").strip() \
            and novel_contracts.sha256_text(candidate_text) != snapshot.text_sha256:
        reasons.append({"code": CODE_EMPTY_CANDIDATE,
                        "detail": "候选只剩空白或 BOM（U+FEFF 不是 str.strip() 认的空白），"
                                  "而源文件有内容：拒绝据此产出一份空成稿"})
        return PublishResult("ERROR", None, None, None)

    mixed = text_source.newline_kind(snapshot) == "mixed"
    utterance = None
    if mixed and channel == CHANNEL_OVERWRITE:
        utterance = require_user_utterance(mixed_newline_authorization)
    elif mixed_newline_authorization not in (None, ""):
        utterance = require_user_utterance(mixed_newline_authorization)
    try:
        payload = text_source.encode_text(candidate_text, snapshot, channel=channel,
                                          mixed_newline_authorization=utterance)
    except text_source.SourceInputError as exc:
        reasons.append({"code": CODE_MIXED_NEWLINE if exc.code
                        == text_source.CODE_MIXED_NEWLINE_OVERWRITE else str(exc.code),
                        "detail": exc.message})
        return PublishResult("ERROR", None, None, None)
    if mixed and channel == CHANNEL_SAVE_AS:
        reasons.append({"code": INFO_MIXED_NEWLINE,
                        "detail": "混合换行已按占比最高的换行统一（默认另存，不改原稿）"})

    ends_with_newline = bool(candidate_text) and candidate_text[-1] in ("\n", "\r")
    if ends_with_newline != snapshot.ends_with_newline:
        reasons.append({"code": CODE_TRAILING_NEWLINE_CHANGED,
                        "detail": f"原稿末尾{'有' if snapshot.ends_with_newline else '没有'}"
                                  "换行，候选却"
                                  f"{'没有' if snapshot.ends_with_newline else '有'}——"
                                  "整篇尾部的换行不是可静默改动的东西"})
        return PublishResult("ERROR", None, None, None)

    if payload == raw_source:
        return PublishResult("NO-OP", None, None, None)
    # 保护与删除闸核对的是**真正落到盘上的那份文字**，不是候选文件里的临时形态。
    delivered_text, _codec, _bom = text_source.decode_source_bytes(payload)

    # 3) 证据：三份都必须绑定本次的源哈希/候选哈希/场景/力度
    candidate_sha = _sha256(candidate_raw)
    validation = _load_json(request.validation_report, VALIDATION_REPORT_SCHEMA)
    manifest = _load_json(request.protection_manifest, PROTECTION_MANIFEST_SCHEMA)
    review = _load_json(request.review_report, REVIEW_REPORT_SCHEMA)
    for label, doc in (("机械验证报告", validation), ("保护清单", manifest),
                       ("复核报告", review)):
        _check_binding(doc, label, snapshot, candidate_sha, request)
    if str(manifest.get("source_sha256") or "").lower() != snapshot.sha256:
        raise PublishError(CODE_EVIDENCE_BINDING, "保护清单绑定的不是这份原稿")

    segments = _segments_from(manifest.get("segments"), "分段记录")
    output_segments = _segments_from(manifest.get("output_segments"), "成稿分段记录")
    for segment in output_segments:
        # 换行风格差异不算不符：候选最终按原稿的换行策略重写，分段记录是管线看到的文本。
        if segment.text and _plain(segment.text) not in _plain(candidate_text):
            reasons.append({"code": CODE_EVIDENCE_BINDING,
                            "detail": f"清单里的成稿分段 {segment.segment_id} 的文字不在候选里，"
                                      "这份保护清单对应的不是这次交付"})
            return PublishResult("ERROR", None, None, None)
    l0 = _recorded_result(validation, "机械验证报告", "L0")
    l1 = _recorded_result(validation, "机械验证报告", "L1")
    review_validation = _recorded_review(review)
    evidence_gates: list = []
    for level in novel_review.REQUIRED_REPORT_LEVELS:
        if level not in tuple(review.get("completed_levels") or ()):
            reasons.append({"code": CODE_REVIEW_LEVELS_INCOMPLETE,
                            "detail": f"复核报告没有完成 {level}：没有该级证据不得发布"})
            evidence_gates.append("REVIEW")
    claimed = {str(item) for item in (review.get("segment_ids") or ())}
    if {segment.segment_id for segment in segments} - claimed:
        reasons.append({"code": CODE_SEGMENT_COVERAGE,
                        "detail": "复核报告未覆盖全部分段，缺的部分没读过"})
        evidence_gates.append("REVIEW")
    ledger = tuple(manifest.get("ledger") or ())
    if ledger_digest(ledger) != str(review.get("ledger_sha256") or "").lower():
        reasons.append({"code": CODE_LEDGER_DIGEST,
                        "detail": "复核报告的账本哈希与当前账本对不上（按 ledger_digest "
                                  "现场重算），报告对应的不是这一版账本"})
        evidence_gates.append("ERROR")
    for claim_id, category in _second_review_gaps(
            ledger, manifest, source_sha=snapshot.sha256, candidate_sha=candidate_sha,
            ledger_sha=ledger_digest(ledger)):
        reasons.append({"code": CODE_SECOND_REVIEW,
                        "detail": f"账本条目 {claim_id}（{category}）是剧情敏感的确定事实，"
                                  "但这份证据里没有绑定本次交付的第二次语义复核事实："
                                  "缺证据不发布，也不许为此改类别"})
        evidence_gates.append("REVIEW")

    # 4) 判定：L0–L3 + 共用保护合同
    decision = novel_review.publication_decision(l0, l1, review_validation)
    for code in decision.codes:
        if code not in {item.get("code") for item in reasons}:
            reasons.append({"code": code, "detail": "发布判定（publication_decision）给出的码"})
    protection_status = _protection_contract(manifest, segments,
                                             source_text, delivered_text, reasons)
    status = _worst([decision.status, protection_status, *evidence_gates])
    if status != "PASS":
        return PublishResult(status, None, None, None)

    # 5) 落盘
    mode = stat.S_IMODE(snapshot.file_mode)
    target = snapshot.resolved_path
    payload_sha = _sha256(payload)
    if not _prewrite_checks(out_dir, target, payload, len(raw_source) if channel == CHANNEL_OVERWRITE else 0,
                            reasons, channel):
        return PublishResult("ERROR", None, None, None)

    if channel == CHANNEL_OVERWRITE:
        path, backup, digest = _publish_overwrite(target, payload, snapshot.sha256, reasons)
        if path == "NOOP":
            return PublishResult("NO-OP", None, None, None)
        if path is None:
            return PublishResult("ERROR", None, None, None)
        if job_dir is not None:
            if not _record_delivery(Path(job_dir), path=path, digest=digest,
                                    channel=channel, out_dir=out_dir, source=target,
                                    backup=backup, delivered_text=delivered_text,
                                    reasons=reasons):
                return PublishResult("ERROR", path, digest,
                                     Path(backup) if backup else None)
            # 先入册（路径 + 目标哈希）再清理候选：入册失败时候选必须原地留着。
            _finalize_job(Path(job_dir),
                          _recorded_source_name(Path(job_dir), path, snapshot.sha256),
                          digest, channel, reasons)
        return PublishResult("PASS", path, digest, Path(backup) if backup else None)

    existing = _find_delivered(out_dir, Path(request.source), request.strength,
                               payload_sha) if job_dir is not None else None
    if existing is not None:
        # 「文件已发布、状态未收尾」之间崩过：按目标哈希认领这次交付，幂等收尾
        _finalize_job(Path(job_dir), existing.name, payload_sha, channel, reasons)
        return PublishResult("PASS", existing, payload_sha, None)
    path = _publish_save_as(out_dir, Path(request.source), request.strength, payload,
                            mode, reasons)
    if path is None:
        return PublishResult("ERROR", None, None, None)
    digest = _sha256(path.read_bytes())
    if job_dir is not None:
        if not _record_delivery(Path(job_dir), path=path, digest=digest, channel=channel,
                                out_dir=out_dir, source=Path(request.source), backup=None,
                                delivered_text=delivered_text, reasons=reasons):
            return PublishResult("ERROR", path, digest, None)
        _finalize_job(Path(job_dir), path.name, digest, channel, reasons)
    return PublishResult("PASS", path, digest, None)


def _find_delivered(out_dir: Path, source: Path, strength: str, digest: str):
    """按目标哈希找出已经落在盘上的同名交付（恢复窗口，不重复产一份）。"""
    for ordinal in range(1, MAX_OUTPUT_ORDINAL + 1):
        name = (default_output_name(source, strength) if ordinal == 1
                else conflicted_output_name(source, strength, ordinal))
        path = out_dir / name
        try:
            if path.is_file() and _sha256(path.read_bytes()) == digest:
                return path
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
REQUIRED_FLAGS = ("--candidate", "--scene", "--strength", "--expected-source-sha256",
                  "--validation-report", "--protection-manifest", "--review-report")
#: 不带值的开关。**只认这一份名单**：把 --overwrite 写成 --verwrite 不会退成默认另存，
#: 而是被下面的未知参数分支当场拒掉——覆盖通道的开关不许静默失效。
BOOLEAN_FLAGS: tuple[str, ...] = ("--overwrite",)
#: 带值参数的白名单：未知旗标一律拒绝，不让拼错的合同参数被当成「没传」。
VALUE_FLAGS: tuple[str, ...] = REQUIRED_FLAGS + (
    "--output-dir", "--job-dir", "--mixed-newline-authorization",
    "--out-marker", "--start-gate")
EXIT_STATUS = {"PASS": 0, "NO-OP": 0, "FAIL": 1, "REVIEW": 2, "ERROR": 3}
USAGE = ("用法: python scripts/novel_output.py <source> --candidate FILE --scene "
         "ordinary|novel --strength conservative|balanced|deep "
         "--expected-source-sha256 SHA256 --validation-report FILE "
         "--protection-manifest FILE --review-report FILE "
         "[--overwrite] [--mixed-newline-authorization \"「用户原话」\"] "
         "[--output-dir DIR] [--job-dir DIR] [--out-marker FILE] [--start-gate FILE]")


def _wait_for_gate(path, timeout: float = 20.0) -> bool:
    """并发实证用的起跑线：等到同一个门文件出现再一起动手。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(path).is_file():
            return True
        time.sleep(0.005)
    return False


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--help" in args or "-h" in args:
        print(USAGE)
        print("\n覆盖通道必须显式带 --overwrite；混合换行的源文件覆盖还要带 "
              "--mixed-newline-authorization（用户原话的引用）。")
        print("\n能力边界（如实声明，不做静默降级）：--job-dir 的任务锁若已被清理，"
              "CLI 没有传 resume 身份的旗标，一律按 job_lock_not_held 拒绝发布；"
              "接管这类任务请经 Python API publish(..., resume_identity=…)（见 novel_job.resume_job）。")
        return 0
    if not args or args[0].startswith("--"):
        print(USAGE, file=sys.stderr)
        print("缺少原稿路径", file=sys.stderr)
        return 3
    source = Path(args[0])
    values: dict = {}
    index = 1
    while index < len(args):
        flag = args[index]
        if flag in BOOLEAN_FLAGS:
            values[flag] = True
            index += 1
            continue
        if not flag.startswith("--") or flag not in VALUE_FLAGS or index + 1 >= len(args):
            print(USAGE, file=sys.stderr)
            print(f"无法解析或未知的参数: {flag}（覆盖开关是 --overwrite，其余参数都必须带值）",
                  file=sys.stderr)
            return 3
        values[flag] = args[index + 1]
        index += 2
    missing = [flag for flag in REQUIRED_FLAGS if flag not in values]
    if missing:
        print(USAGE, file=sys.stderr)
        print(f"覆盖请求必须携带全部合同参数，缺少: {', '.join(missing)}", file=sys.stderr)
        return 3
    request = CommitRequest(
        source=source, scene=values["--scene"], strength=values["--strength"],
        expected_source_sha256=values["--expected-source-sha256"],
        candidate=Path(values["--candidate"]),
        validation_report=Path(values["--validation-report"]),
        protection_manifest=Path(values["--protection-manifest"]),
        review_report=Path(values["--review-report"]))
    if values.get("--start-gate") and not _wait_for_gate(values["--start-gate"]):
        print("起跑门文件超时未出现", file=sys.stderr)
        return 3
    reasons: list = []
    result = publish(request,
                     channel=CHANNEL_OVERWRITE if "--overwrite" in values else CHANNEL_SAVE_AS,
                     output_dir=values.get("--output-dir"),
                     mixed_newline_authorization=values.get("--mixed-newline-authorization"),
                     job_dir=values.get("--job-dir"), reasons=reasons)
    payload = {"status": result.status,
               "path": str(result.path) if result.path else None,
               "sha256": result.sha256,
               "backup_path": str(result.backup_path) if result.backup_path else None,
               "reasons": reasons}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    marker = values.get("--out-marker")
    if marker:
        # 结果回执也走唯一写者：独占创建，已有同名文件就绝不覆盖（并发实证各写各的标记）。
        # 写不进回执不改判发布结论——JSON 已经打印，这里只如实报出回执失败并留非零线索。
        try:
            file_commit.SafeFileCommitEngine.create_exclusive(Path(marker),
                                                              (text + "\n").encode("utf-8"))
        except OSError as exc:
            print(f"回执未能写入 {marker}：{type(exc).__name__}: {exc}", file=sys.stderr)
    return EXIT_STATUS[result.status]


if __name__ == "__main__":
    sys.exit(main())
