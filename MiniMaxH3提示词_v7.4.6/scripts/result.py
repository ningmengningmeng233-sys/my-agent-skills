#!/usr/bin/env python3
"""Unified Result Data Structures and Exit Codes for MiniMax H3 Craft Auditor."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

StatusType = Literal["PASS", "WARN", "FAIL", "SKIP"]


@dataclass
class RuleResult:
    rule_id: str
    status: StatusType
    message: str
    evidence: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditReport:
    profile: str
    ruleset_version: str
    passed: int
    warned: int
    failed: int
    skipped: int
    results: list[RuleResult]
    prompt_sha256: str | None = None
    auditor_sha256: str | None = None
    executed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["results"] = [r.to_dict() for r in self.results]
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# Exit code standards
EXIT_SUCCESS = 0  # All PASS or WARN allowed by policy
EXIT_FAILURE = 1  # Contains FAIL
EXIT_INPUT_ERROR = 2  # Bad CLI arguments, encoding or syntax
EXIT_CORRUPT_ERROR = 3  # Corrupt ruleset or internal error
