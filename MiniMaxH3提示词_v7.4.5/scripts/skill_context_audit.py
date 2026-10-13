#!/usr/bin/env python3
"""Skill Context Budget Auditor for MiniMax H3 Craft (main-entry guard).

Guards `SKILL.md` — the single authoritative main entry — against context-window
drift, using four deterministic rules:

  1. Entry size banding: LIGHTWEIGHT / TARGET / LARGE / OVERSIZED / MISSING.
  2. Declared on-demand routing integrity: every `references/...`, `scripts/...`,
     `config/...`, `templates/...`, `文档/...` token must resolve on disk.
  3. Whole-directory load anti-pattern: directives that load every reference up
     front. Detection matches the whole line with per-slot-bounded bridges, and a
     match is treated as a prohibition when a negation appears either inside the
     match span or in the same-clause head in front of it; see
     `detect_unconditional_load_all` and `is_prohibited` for the trade-offs.
  4. Evidence-infrastructure reachability (`e2e/`, `_审查测试/`) — advisory only,
     because missing harness infrastructure never blocks skill delivery.

Only these four conditions produce `status == "FAIL"`:
  MISSING_SKILL_ENTRY / SKILL_ENTRY_OVERSIZED / MISSING_ROUTE_TARGETS /
  UNCONDITIONAL_LOAD_ALL_REFERENCES

Corpus is read as strict UTF-8 (no BOM/newline translation) so `skill_bytes` is
the true on-disk size. An unreadable main entry is reported Fail-Closed as
`MISSING_SKILL_ENTRY` instead of raising, so the auditor is safe as a CI gate.

Code-block metrics follow CommonMark backtick-fence semantics: an opening fence
of N backticks is closed only by a fence of at least N backticks, so shorter
fence-looking lines inside a longer fence stay part of the block body.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SKILL_ENTRY = "SKILL.md"
LIGHTWEIGHT_MAX_BYTES = 10_000
NEAR_LIMIT_BYTES = 20_000
HARD_CAP_BYTES = 25_000
LARGE_MAX_BYTES = 50_000

ROUTE_PREFIXES = ("references", "scripts", "config", "templates", "文档")
EVIDENCE_PREFIXES = ("e2e", "_审查测试")
ALL_PREFIXES = ROUTE_PREFIXES + EVIDENCE_PREFIXES

FAIL_MISSING_ENTRY = "MISSING_SKILL_ENTRY"
FAIL_ENTRY_OVERSIZED = "SKILL_ENTRY_OVERSIZED"
FAIL_MISSING_ROUTES = "MISSING_ROUTE_TARGETS"
FAIL_UNCONDITIONAL_LOAD_ALL = "UNCONDITIONAL_LOAD_ALL_REFERENCES"

_TOKEN_CLASS = r"A-Za-z0-9_\-./\*"
_CJK_CLASS = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"

# `prefix/relative/path` tokens. The lookbehind rejects only real path characters
# (not CJK prose and not `*`), so a route glued to Chinese prose such as
# "详见references/ghost.md" and Markdown-emphasised routes such as
# "**references/ghost.md**" are still discovered instead of silently passing.
_PATH_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_\-./])"
    r"(?P<prefix>"
    + "|".join(re.escape(p) for p in sorted(ALL_PREFIXES, key=len, reverse=True))
    + r")/"
    r"(?P<tail>[" + _TOKEN_CLASS + _CJK_CLASS + r"]*)"
)

# Trailing punctuation trimmed from an extracted token. `*` is included so that
# Markdown emphasis fences (`**references/a.md**`, `*references/a.md*`) peel off
# to a clean path; a star following `/` keeps its glob meaning (see
# `strip_trailing_punctuation`).
_TRAILING_PUNCT = frozenset(".,;:!?)]}>\"'*”’。，、；：！？）】》」』…·")

_CJK_TAIL_RUN_RE = re.compile(r"[" + _CJK_CLASS + r"]+$")

_LOAD_VERB = r"(?:读取|阅读|加载|载入|遍历|浏览|查看|打开|读|看|扫)"
_LOAD_CORE = r"(?:全部|所有|整个|全量)"
_LOAD_REF = r"(?:references|参考)"

# Slot-tightened bridges. Each slot is only as permissive as the wording it must
# actually bridge, which is what keeps "pointer" sentences such as
# "全部可用模式请查看 references/native/model-capabilities.md" from reading as a
# load-everything directive: a core word may only sit within 3 characters of a
# verb, and a reference must be followed directly by an optional 目录/下/里/的/：
# before a core word can follow.
_BRIDGE_VERB_TO_REF = r"[^\n]{0,20}"
_BRIDGE_VERB_TO_CORE = r"[^\n]{0,3}"
_REF_TO_CORE = r"\s*(?:目录|文件夹|子目录|下|里|中|内)?\s*(?:的)?\s*[：:]?\s*"
_CORE_TO_REF = r"\s*(?:的)?\s*"

# Three word orders, matched against the whole line:
#   verb … reference … core  ("读取 references 下全部文件")
#   verb … core … reference  ("一次性读取全部references")
#   core … verb … reference  ("全部读取 references 下文件")
_LOAD_ALL_PATTERNS = (
    re.compile(_LOAD_VERB + _BRIDGE_VERB_TO_REF + _LOAD_REF + _REF_TO_CORE + _LOAD_CORE),
    re.compile(_LOAD_VERB + _BRIDGE_VERB_TO_CORE + _LOAD_CORE + _CORE_TO_REF + _LOAD_REF),
    re.compile(_LOAD_CORE + _BRIDGE_VERB_TO_CORE + _LOAD_VERB + _BRIDGE_VERB_TO_REF + _LOAD_REF),
)

# Clause punctuation: splits a line into clauses so the backward negation scan
# stops at the clause boundary instead of leaking into an earlier clause.
_CLAUSE_PUNCT_RE = re.compile(r"[，。；：、！？,;:!?…]+")

# Negation guard. A bare `不` only counts as negation when it is immediately
# followed by a prohibition/loading word, so 不确定 / 不管 / 不论 / 不仅 are not
# mistaken for prohibitions. Bare `别` is deliberately absent because 特别 /
# 识别 / 区别 would be false hits.
_NEGATION_GUARD = re.compile(
    r"勿|切勿|切忌|不得|不要|不应|不许|不准|禁止|严禁|避免|无需"
    r"|不(?=(?:要|得|应|能|可|许|准|会|允|必|宜|再)|" + _LOAD_VERB + r")"
)

_FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<fence>`{3,})(?P<info>[^`]*)$")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,})[ \t]*$")


def classify_band(skill_bytes: int) -> str:
    """Map an entry byte size onto its context-budget band."""
    if skill_bytes <= LIGHTWEIGHT_MAX_BYTES:
        return "LIGHTWEIGHT"
    if skill_bytes <= HARD_CAP_BYTES:
        return "TARGET"
    if skill_bytes <= LARGE_MAX_BYTES:
        return "LARGE"
    return "OVERSIZED"


def count_code_blocks(lines: list[str]) -> tuple[int, int]:
    """Count fenced code blocks and the bytes of their body lines.

    CommonMark semantics: an N-backtick opening fence is closed only by a fence
    of at least N backticks; shorter fence-looking lines are block body content.
    Fence delimiter lines themselves are never counted in the byte total.
    """
    block_count = 0
    content_bytes = 0
    open_fence_len = 0

    for line in lines:
        if open_fence_len:
            closing = _FENCE_CLOSE_RE.match(line)
            if closing and len(closing.group("fence")) >= open_fence_len:
                open_fence_len = 0
                continue
            content_bytes += len(line.encode("utf-8"))
            continue

        opening = _FENCE_OPEN_RE.match(line)
        if opening:
            open_fence_len = len(opening.group("fence"))
            block_count += 1

    return block_count, content_bytes


def strip_trailing_punctuation(token: str) -> str:
    """Drop trailing prose punctuation glued onto a path token.

    A trailing `*` run directly preceded by `/` is preserved (`config/*`,
    `references/*`): that is a glob over a directory and must keep being checked
    with `Path.glob` instead of collapsing to the parent directory. Stars that
    follow a real path character are Markdown emphasis fences
    (`**references/a.md**`, `*references/a.md*`) and are peeled off.
    """
    while token and token[-1] in _TRAILING_PUNCT:
        if token[-1] == "*" and token.endswith("/*"):
            break
        token = token[:-1]
    return token


def token_exists(root: Path, token: str) -> bool:
    """Probe a repo-relative token; `*` tokens are treated as globs."""
    if "*" in token:
        try:
            return any(True for _ in root.glob(token))
        except (ValueError, OSError):
            return False
    try:
        return (root / token).exists()
    except OSError:
        return False


def strip_trailing_cjk_run(token: str) -> str:
    """Peel the whole trailing CJK run off a token (stage 2 of `resolve_token`)."""
    return _CJK_TAIL_RUN_RE.sub("", token)


def resolve_token(root: Path, token: str) -> bool:
    """Three-stage probe of a declared route token.

    1. the token exactly as written;
    2. the token with its entire trailing CJK prose run peeled off, skipped when
       that would leave nothing or a bare `prefix/`;
    3. one-character-at-a-time rollback from the stage-2 base (or from the raw
       token when stage 2 was skipped), probing at every step and giving up only
       on an empty string or a bare `prefix/`.

    Stages 2 and 3 both fight the same failure mode: Chinese prose glues onto a
    declared path, e.g. `references/real.md这类文档`, or the path loses its tail to
    prose, e.g. `文档/评审记录目录` for the real directory `文档/评审记录`. Neither
    stage is capped, because any cap turns "path + N prose characters" into a
    cliff where N passes and N+1 fails for no defensible reason. Stage 1 always
    runs first, so a correctly written route is never weakened by stages 2-3.
    """
    if token_exists(root, token):
        return True

    base = strip_trailing_cjk_run(token)
    if base and not base.endswith("/"):
        if token_exists(root, base):
            return True
    else:
        base = token

    candidate = base
    while True:
        candidate = candidate[:-1]
        if not candidate or candidate.endswith("/"):
            return False
        if token_exists(root, candidate):
            return True


def collect_targets(root: Path, text: str) -> tuple[dict[str, bool], dict[str, bool]]:
    """Return (route_targets, evidence_targets) as token -> exists maps."""
    route_targets: dict[str, bool] = {}
    evidence_targets: dict[str, bool] = {}

    for match in _PATH_TOKEN_RE.finditer(text):
        token = strip_trailing_punctuation(match.group(0))
        bucket = (
            route_targets
            if match.group("prefix") in ROUTE_PREFIXES
            else evidence_targets
        )
        if token in bucket:
            continue
        bucket[token] = resolve_token(root, token)

    return route_targets, evidence_targets


def is_prohibited(line: str, match: re.Match) -> bool:
    """Decide whether a matched directive is negated, i.e. a prohibition.

    Two suppression channels; either one marks the match as a prohibition:

    1. a negation inside the match span itself
       ("按需加载：不要一次性读取全部references");
    2. a negation in the clause head in front of the match. The clause head runs
       from the start of the match's own clause to the match start with **no
       length limit**; the only truncation is the nearest clause punctuation.
       Punctuation is therefore the sole anti-leak mechanism, never a window
       size: "不要漏掉，读取全部references" stays a violation because the comma
       ends the clause, while a long same-clause qualification such as
       "不要在用户没有明确要求的情况下随意读取全部references" is still a
       prohibition however far the negation sits from the verb.

    The clause head is evaluated together with the match span so that a `不`
    sitting immediately before the verb can complete its `不(?=verb)` lookahead
    ("一次不加载整个references目录" is a prohibition).
    """
    if _NEGATION_GUARD.search(match.group(0)):
        return True

    clause_head = _CLAUSE_PUNCT_RE.split(line[:match.start()])[-1]
    return bool(_NEGATION_GUARD.search(clause_head + match.group(0)))


def detect_unconditional_load_all(text: str) -> tuple[bool, list[dict]]:
    """Detect "load the whole references directory" directives.

    Matching runs against the **whole line**, not against punctuation-delimited
    clauses, so a colon-separated label cannot hide a directive
    ("一次性加载：references 全部卡片" is a violation). Each of the three word-order
    patterns bridges its slots individually: a verb reaches a reference across up
    to 20 characters, but a core word (`全部/所有/整个/全量`) may only sit within
    3 characters of its verb and only directly after a reference (optional
    目录/下/里/中/内 + 的 + ：). That per-slot tightening is what rejects "pointer"
    sentences such as "全部可用模式请查看 references/native/model-capabilities.md".

    Negation is handled by two suppression channels (see `is_prohibited`): inside
    the match span, and in the same-clause head in front of it. A negation in an
    earlier clause does not suppress a later clause's directive.

    Two deliberate limitations are accepted here:

    * Per-slot bridging is bounded, so a rare wording that separates a verb from
      its object by punctuation or by more than the slot allowance is not
      detected. Treating those gaps as scope boundaries is preferred over the
      false positives that unlimited bridging produced on pointer sentences.
    * Only the three word orders above are covered. A fourth order —
      reference … core … verb, e.g. "references 下全部文件都要读" — is not
      detected, because adding it would immediately flag this project's own
      pointer phrasing such as "references 下全部卡片按需加载" as a violation,
      which is a far worse trade than missing that rare construction.

    Returns (violation, matches) where each match is `{"line": int, "text": str}`
    holding the full offending line (one entry per line, sorted by line number).
    """
    matches: list[dict] = []

    for line_number, line in enumerate(text.splitlines(), start=1):
        violation = False
        for pattern in _LOAD_ALL_PATTERNS:
            for match in pattern.finditer(line):
                if is_prohibited(line, match):
                    continue
                violation = True
                break
            if violation:
                break

        if violation:
            matches.append({"line": line_number, "text": line})

    matches.sort(key=lambda item: item["line"])
    return bool(matches), matches


def _empty_report(root: Path) -> dict:
    """Fail-Closed skeleton shared by every early-return path."""
    return {
        "root": str(root),
        "skill_entry": SKILL_ENTRY,
        "skill_exists": False,
        "skill_bytes": 0,
        "skill_lines": 0,
        "code_block_count": 0,
        "code_block_bytes": 0,
        "target_band_status": "MISSING",
        "near_limit_warning": False,
        "route_targets": {},
        "missing_route_targets": [],
        "evidence_targets": {},
        "missing_evidence_paths": [],
        "unconditional_load_all_references": False,
        "load_all_matches": [],
        "hard_failures": [],
        "status": "PASS",
    }


def _finalize(report: dict, hard_failures: list[str]) -> dict:
    report["hard_failures"] = sorted(set(hard_failures))
    report["status"] = "FAIL" if report["hard_failures"] else "PASS"
    return report


def audit_skill_context(root: Path) -> dict:
    """Audit a skill root's main entry; never raises on a missing entry."""
    root = Path(root)
    report = _empty_report(root)
    entry_path = root / SKILL_ENTRY

    try:
        raw = entry_path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return _finalize(report, [FAIL_MISSING_ENTRY])

    report["skill_exists"] = True
    report["skill_bytes"] = len(raw)
    report["skill_lines"] = len(text.splitlines())
    report["target_band_status"] = classify_band(len(raw))
    report["near_limit_warning"] = len(raw) > NEAR_LIMIT_BYTES

    block_count, block_bytes = count_code_blocks(text.splitlines())
    report["code_block_count"] = block_count
    report["code_block_bytes"] = block_bytes

    route_targets, evidence_targets = collect_targets(root, text)
    report["route_targets"] = route_targets
    report["evidence_targets"] = evidence_targets
    report["missing_route_targets"] = sorted(
        token for token, exists in route_targets.items() if not exists
    )
    report["missing_evidence_paths"] = sorted(
        token for token, exists in evidence_targets.items() if not exists
    )

    load_all, load_all_matches = detect_unconditional_load_all(text)
    report["unconditional_load_all_references"] = load_all
    report["load_all_matches"] = load_all_matches

    hard_failures: list[str] = []
    if len(raw) > HARD_CAP_BYTES:
        hard_failures.append(FAIL_ENTRY_OVERSIZED)
    if report["missing_route_targets"]:
        hard_failures.append(FAIL_MISSING_ROUTES)
    if load_all:
        hard_failures.append(FAIL_UNCONDITIONAL_LOAD_ALL)

    return _finalize(report, hard_failures)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="skill_context_audit",
        description=(
            "Audit a skill root's SKILL.md main entry: size band, on-demand "
            "routing integrity, and whole-directory load violations."
        ),
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=None,
        help=(
            "Skill root directory to audit. Defaults to the parent directory of "
            "this script (the skill root it ships inside)."
        ),
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    report = audit_skill_context(root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
