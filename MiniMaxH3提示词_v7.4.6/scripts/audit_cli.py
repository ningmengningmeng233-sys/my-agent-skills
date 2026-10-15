#!/usr/bin/env python3
"""Unified Audit CLI and Engine for MiniMax H3 Craft."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timezone

from result import (
    EXIT_FAILURE,
    EXIT_INPUT_ERROR,
    EXIT_SUCCESS,
    AuditReport,
    RuleResult,
)
from rules_native_drama import audit_native_drama_rules
from rules_native_general import audit_native_general_rules
from rules_ref2va import audit_ref2va_rules
from versioning import get_version

def get_ruleset_version() -> str:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return get_version(repo_root)

RULESET_VERSION = get_ruleset_version()


def compute_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def detect_channel(text: str) -> str:
    """Detect whether prompt text conforms to ref2va-en, native-drama-series, or native-general."""
    has_ref2va = (
        "subject_definitions:" in text
        or "detailed_description:" in text
        or "<Subject " in text
    )
    has_drama = "【环境光影】" in text or "【空间基准】" in text or "【运镜设计】" in text

    if has_ref2va and has_drama:
        return "conflict"
    if has_ref2va:
        return "ref2va-en"
    if has_drama:
        return "native-drama-series"
    
    # Substantial Chinese text qualifies as native-general
    zh_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    if zh_chars >= 4:
        return "native-general"

    return "unknown"


def read_input_strictly(file_arg: str | None, encoding: str = "utf-8") -> tuple[str, int]:
    """Read input text strictly without silent fallback to prevent damaged encodings (P1-11)."""
    if not file_arg or file_arg == "-":
        raw = sys.stdin.buffer.read()
        try:
            return raw.decode(encoding), EXIT_SUCCESS
        except UnicodeDecodeError as exc:
            sys.stderr.write(f"ERROR: stdin is not valid {encoding}: {exc}\n")
            return "", EXIT_INPUT_ERROR

    if not os.path.exists(file_arg):
        sys.stderr.write(f"ERROR: file does not exist: {file_arg}\n")
        return "", EXIT_INPUT_ERROR

    try:
        with open(file_arg, "rb") as f:
            raw = f.read()
            return raw.decode(encoding), EXIT_SUCCESS
    except UnicodeDecodeError as exc:
        sys.stderr.write(f"ERROR: file {file_arg} is not valid {encoding}: {exc}\n")
        return "", EXIT_INPUT_ERROR


def run_audit(
    text: str,
    mode: str = "auto",
    audio_policy: str = "normal",
    warn_policy: str = "report",
    max_duration_ms: int | None = None,
) -> AuditReport:
    """Core auditor execution function returning structured AuditReport."""
    # Channel detection
    detected = detect_channel(text)

    if mode == "auto":
        channel = detected
    else:
        channel = mode

    results: list[RuleResult] = []

    if channel == "conflict":
        results.append(
            RuleResult(
                rule_id="R-SYS-CONFLICT",
                status="FAIL",
                message="提示词同时包含英文 Ref2VA 标签与中文原生章节，通道混用冲突",
            )
        )
        profile_name = "hybrid-conflict"
    elif channel == "unknown":
        results.append(
            RuleResult(
                rule_id="R-SYS-UNKNOWN",
                status="FAIL",
                message="无法识别合法提示词通道：既不包含 Ref2VA 英文六字段，也不包含原生中文必须章节",
            )
        )
        profile_name = "unknown"
    elif channel in ("ref2va-en", "ref2va"):
        if detected == "native-zh":
            results.append(
                RuleResult(
                    rule_id="R-SYS-MODE-MISMATCH",
                    status="FAIL",
                    message="显式指定了 ref2va-en 模式，但输入文本为原生中文结构",
                )
            )
        else:
            results.extend(
                audit_ref2va_rules(
                    text, audio_policy=audio_policy, max_duration_ms=max_duration_ms
                )
            )
        profile_name = "ref2va-en"
    elif channel in ("native-zh", "native-drama-series"):
        if detected == "ref2va-en":
            results.append(
                RuleResult(
                    rule_id="R-SYS-MODE-MISMATCH",
                    status="FAIL",
                    message="显式指定了 native-zh 模式，但输入文本为英文 Ref2VA 结构",
                )
            )
        else:
            results.extend(audit_native_drama_rules(text))
        profile_name = "native-drama-series"
    elif channel == "native-general":
        if detected == "ref2va-en":
            results.append(
                RuleResult(
                    rule_id="R-SYS-MODE-MISMATCH",
                    status="FAIL",
                    message="显式指定了 native-general 模式，但输入文本为英文 Ref2VA 结构",
                )
            )
        elif detected in ("native-zh", "native-drama-series"):
            results.append(
                RuleResult(
                    rule_id="R-SYS-MODE-MISMATCH",
                    status="FAIL",
                    message="显式指定了 native-general 模式，但输入文本为短剧固定六段式结构，请使用 native-drama-series 规则集",
                )
            )
        else:
            results.extend(audit_native_general_rules(text))
        profile_name = "native-general"
    else:
        results.append(
            RuleResult(
                rule_id="R-SYS-ERR",
                status="FAIL",
                message=f"不支持的校验模式: {channel}",
            )
        )
        profile_name = str(channel)

    passed_cnt = sum(1 for r in results if r.status == "PASS")
    warned_cnt = sum(1 for r in results if r.status == "WARN")
    failed_cnt = sum(1 for r in results if r.status == "FAIL")
    skipped_cnt = sum(1 for r in results if r.status == "SKIP")

    report = AuditReport(
        profile=profile_name,
        ruleset_version=RULESET_VERSION,
        passed=passed_cnt,
        warned=warned_cnt,
        failed=failed_cnt,
        skipped=skipped_cnt,
        results=results,
        prompt_sha256=compute_sha256(text),
        executed_at=datetime.now(timezone.utc).isoformat(),
    )
    return report


# Backward-compatible APIs for existing code
def audit_native_zh(text: str) -> tuple[int, int, int]:
    report = run_audit(text, mode="native-zh")
    return report.passed, report.warned, report.failed


def audit_ref2va_en(text: str) -> tuple[int, int, int]:
    report = run_audit(text, mode="ref2va-en")
    return report.passed, report.warned, report.failed


def main() -> int:
    parser = argparse.ArgumentParser(description="MiniMax H3 Craft Video Prompt Auditor")
    parser.add_argument("prompt_file", nargs="?", default="-", help="Prompt file path or '-' for stdin")
    parser.add_argument("--mode", choices=["auto", "native-zh", "native-drama-series", "native-general", "ref2va-en"], default="auto")
    parser.add_argument("--audio-policy", choices=["normal", "silent", "diegetic-only", "user-specified"], default="normal")
    parser.add_argument("--warn-policy", choices=["allow", "report", "fail"], default="report")
    parser.add_argument("--input-encoding", default="utf-8", help="Encoding of input text (default: utf-8)")
    parser.add_argument("--json", action="store_true", help="Output audit report in structured JSON format")

    args = parser.parse_args()

    text, read_code = read_input_strictly(args.prompt_file, encoding=args.input_encoding)
    if read_code != EXIT_SUCCESS:
        return read_code

    report = run_audit(
        text=text,
        mode=args.mode,
        audio_policy=args.audio_policy,
        warn_policy=args.warn_policy,
    )

    if args.json:
        print(report.to_json())
    else:
        print(f"=== Auditing Profile: {report.profile} ({len(text)} chars) ===")
        for r in report.results:
            print(f"{r.status} [{r.rule_id}]: {r.message}")
        print(f"RESULT: {report.passed} PASS, {report.warned} WARN, {report.failed} FAIL (Ruleset: v{report.ruleset_version})")

    # Determine exit code according to warn-policy and failure status
    if report.failed > 0:
        return EXIT_FAILURE
    if report.warned > 0 and args.warn_policy == "fail":
        return EXIT_FAILURE

    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
