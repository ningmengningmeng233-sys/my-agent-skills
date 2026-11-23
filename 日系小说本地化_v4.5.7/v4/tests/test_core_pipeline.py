"""v4 Core checks/decisions/rewrite/merge 自测。

覆盖：
- decisions：结构校验、禁止 state 字段、A/B/C 分层、开放项、哈希、事件决策闭环、映射冲突
- checks：正文纯度、分段 ID、事件级对照、必替换残留分级、篇幅 warning、写作指导六项旁车
- rewrite：改写前检查、状态迁移、确定性占位、纯正文提交
- merge：正式合并、缺章/哈希不一致拒绝、实验版隔离

可作为 pytest 测试运行，也可独立运行：
    python -m v4.tests.test_core_pipeline
（在技能根目录）
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from v4.core import checks, decisions, events, fingerprints, merge, rewrite  # noqa: E402
from v4.core.audit import append_audit, now_iso  # noqa: E402
from v4.core.hashing import sha256_bytes  # noqa: E402
from v4.core.jsonio import read_json, write_json_atomic  # noqa: E402
from v4.core.manifest import ManifestStore  # noqa: E402
from v4.core.paths import ARTIFACT_SCHEMA, RULES_PATH  # noqa: E402
from v4.core.schemas import SchemaError  # noqa: E402
from v4.core.state_machine import StateError  # noqa: E402
from v4.core.workspace import create_workspace  # noqa: E402

SOURCE_TEXT = """第一章 相遇

森田推门进入大厅。

森田向老板说明来意，老板答应借宿。

两人在灯下交谈，森田心中平静。
"""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _build_events(chapter_id: str = "ch-001") -> dict:
    source_sha = sha256_bytes(SOURCE_TEXT.encode("utf-8"))
    ev1 = events.new_event(
        "e-0001", 1, participants=["森田"], action="推门进入大厅",
        result="进入大厅", world_function="无", confidence=0.9,
    )
    ev2 = events.new_event(
        "e-0002", 2, participants=["森田", "老板"], prerequisites=["e-0001"],
        action="向老板说明来意", result="老板答应借宿", world_function="无",
        confidence=0.9,
    )
    return events.build_event_graph(chapter_id, source_sha, [ev1, ev2])


def _empty_chapter_decisions(chapter_id: str = "ch-001") -> dict:
    return {
        "schema_version": "v4-chapter-decisions-1",
        "chapter_id": chapter_id,
        "revision": 1,
        "entries": [],
    }


def _make_chapter(
    ws: Path,
    *,
    chapter_id: str,
    ordinal: int,
    title: str,
    state: str,
    source_text: str = SOURCE_TEXT,
    events_doc: dict | None = None,
    decisions_doc: dict | None = None,
    output_text: str | None = None,
    checks_summary: dict | None = None,
) -> dict:
    ch_dir = ws / "chapters" / chapter_id
    ch_dir.mkdir(parents=True, exist_ok=True)
    (ch_dir / "source.md").write_text(source_text, encoding="utf-8")
    (ch_dir / "events.json").write_text(
        json.dumps(events_doc or _build_events(chapter_id), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (ch_dir / "decisions.json").write_text(
        json.dumps(decisions_doc or _empty_chapter_decisions(chapter_id), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (ch_dir / "audit.jsonl").touch()
    if output_text is not None:
        (ch_dir / "output.md").write_text(output_text, encoding="utf-8")
    # v4.5.0-dev.3：approved/delivered 章节必须带不可变版本工件与批准指针
    if state in {"approved", "delivered"} and output_text is not None:
        from v4.core.version_store import save_version

        record = save_version(ch_dir, output_text, kind="draft")
        (ch_dir / "current.json").write_text(
            json.dumps(
                {
                    "latest_version": 1,
                    "latest_sha256": record["sha256"],
                    "latest_kind": "draft",
                    "approved_version": 1,
                    "approved_sha256": record["sha256"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    fingerprints_rec = fingerprints.chapter_fingerprints(
        source=ch_dir / "source.md",
        events=ch_dir / "events.json",
        project_decisions=ws / "project-decisions.json",
        chapter_decisions=ch_dir / "decisions.json",
        rules=RULES_PATH,
        output=ch_dir / "output.md",
        checks=ch_dir / "checks.json",
    )
    append_audit(ws / "audit.jsonl", "state_change_detail", chapter_id=chapter_id, to=state)
    return {
        "id": chapter_id,
        "ordinal": ordinal,
        "title": title,
        "state": state,
        "paths": {
            "source": f"chapters/{chapter_id}/source.md",
            "events": f"chapters/{chapter_id}/events.json",
            "decisions": f"chapters/{chapter_id}/decisions.json",
            "output": f"chapters/{chapter_id}/output.md",
            "checks": f"chapters/{chapter_id}/checks.json",
            "audit": f"chapters/{chapter_id}/audit.jsonl",
        },
        "fingerprints": fingerprints_rec,
        "checks": checks_summary or {
            "hard_gate": "not_run",
            "event_comparison": "not_run",
            "last_run": None,
        },
        "blockers": [],
        "decisions": decisions.decision_summary(decisions_doc or _empty_chapter_decisions(chapter_id)),
        "enhancements": {
            "reference_assets_used": False,
            "blind_review": {"enabled": False, "result": "not_enabled"},
        },
        "updated_at": now_iso(),
    }


def _build_workspace(tmp: Path, chapters: list[dict], *, name: str | None = None) -> Path:
    ws = tmp / (name or f"ws-{len(list(tmp.iterdir()))}")
    create_workspace(ws, title="测试小说", author="测试", input_language="zh-CN")
    manifest = read_json(ws / "manifest.json")
    manifest["chapters"] = chapters
    write_json_atomic(ws / "manifest.json", manifest)
    for chapter in chapters:
        append_audit(
            ws / "audit.jsonl",
            "state_change_detail",
            chapter_id=chapter["id"],
            to=chapter["state"],
        )
    return ws


def _validate_checks_doc(doc: dict) -> None:
    from jsonschema import Draft202012Validator

    full = json.loads(Path(ARTIFACT_SCHEMA).read_text(encoding="utf-8"))
    schema = full["$defs"]["checks"]
    schema["$defs"] = full["$defs"]
    Draft202012Validator(schema).validate(doc)


def _passed_checks_summary() -> dict:
    return {
        "hard_gate": "pass",
        "event_comparison": "pass",
        "last_run": now_iso(),
        "warning_count": 0,
        "failure_count": 0,
    }


# ── decisions ────────────────────────────────────────────────────────────


def run_decisions() -> None:
    # 空决策文件可校验
    empty = _empty_chapter_decisions()
    _check(decisions.validate_document(empty, project=False) == [], "空章节决策应通过校验")
    project_empty = {"schema_version": "v4-project-decisions-1", "revision": 1, "entries": []}
    _check(decisions.validate_document(project_empty, project=True) == [], "空项目决策应通过校验")

    # 禁止直接状态字段（C-035 / F-017）
    forged = dict(empty)
    forged["state"] = "passed"
    errors = decisions.validate_decision_document(forged, project=False)
    _check(any("state" in e for e in errors), "决策文档 state 字段应被拒绝")
    forged_entry = dict(empty)
    forged_entry["entries"] = [
        {
            "decision_id": "dec-0001", "category": "mapping", "target": "森田",
            "choice": "林远", "status": "accepted", "state": "passed",
            "source": {"kind": "user", "value": "u"}, "created_at": now_iso(),
        }
    ]
    errors = decisions.validate_decision_document(forged_entry, project=False)
    _check(any("state/passed" in e for e in errors), "决策项 state 字段应被拒绝")

    # 决策 ID 重复
    dup = dict(empty)
    entry = decisions.new_entry(
        category="mapping", target="森田", choice="林远",
        source_kind="user", source_value="u", decision_id="dec-0001",
    )
    dup["entries"] = [entry, dict(entry)]
    errors = decisions.validate_decision_document(dup, project=False)
    _check(any("重复" in e for e in errors), "重复 decision_id 应被拒绝")

    # A/B/C 分层
    _check(decisions.classify_tier(decisions.new_entry(
        category="mapping", target="森田", choice="林远",
        source_kind="user", source_value="u", reason="主角名核心译名，A档")) == "A",
        "显式 A档 识别失败")
    _check(decisions.classify_tier(decisions.new_entry(
        category="mapping", target="语气词", choice="删除",
        source_kind="system", source_value="rules", reason="低风险日常语气词")) == "B",
        "低风险日常词应归 B 档")
    _check(decisions.classify_tier(decisions.new_entry(
        category="mapping", target="神社", choice="保留功能",
        source_kind="user", source_value="u", reason="承担世界观功能，文化外壳不确定")) == "C",
        "文化功能不确定应归 C 档")
    _check(decisions.classify_tier(decisions.new_entry(
        category="plot_change", target="删除事件", choice="不删",
        source_kind="user", source_value="u")) == "A", "剧情级变更应归 A 档")

    # 开放项阻塞
    open_doc = dict(empty)
    open_doc["entries"] = [decisions.new_entry(
        category="mapping", target="森田", choice="林远",
        source_kind="user", source_value="u", reason="主角名，A档", status="open",
    )]
    summary = decisions.decision_summary(open_doc)
    _check(summary["required_open"] is True and summary["open_count"] == 1, "开放 A 档项应阻塞")
    _check(bool(decisions.required_open_entries(open_doc)), "required_open_entries 应返回开放 A/C 项")

    # 事件决策闭环
    graph = _build_events()
    status, issues = decisions.closure_status(project_empty, empty, graph)
    _check(status == "pass", f"无开放项应 pass: {status} {issues}")
    risky = _build_events()
    for event in risky["events"]:
        event["risk"] = events.high_risk(["causal_chain"], decision_status="approved", decision_id="dec-0001")
    status, issues = decisions.closure_status(project_empty, empty, risky)
    _check(status == "blocked", "approved 高风险事件缺少匹配决策应阻塞")

    # 同一原词多个项目级译名 → C 档冲突（C-033）
    conflicts = decisions.project_mapping_conflicts({
        "schema_version": "v4-project-decisions-1", "revision": 2,
        "entries": [
            decisions.new_entry(category="mapping", target="森田", choice="林远",
                                source_kind="user", source_value="u", status="accepted"),
            decisions.new_entry(category="mapping", target="森田", choice="田森",
                                source_kind="user", source_value="u", status="accepted"),
        ],
    })
    _check(len(conflicts) == 1 and conflicts[0]["target"] == "森田", "同词多译名冲突未检出")

    # add/set 与哈希
    doc = dict(empty)
    decisions.add_entry(doc, decisions.new_entry(
        category="mapping", target="森田", choice="林远",
        source_kind="user", source_value="u", reason="主角名，A档",
    ))
    _check(doc["revision"] == 2 and doc["entries"][0]["decision_id"] == "dec-0001", "add_entry 应递增 revision")
    decisions.set_entry_status(doc, "dec-0001", "accepted", source_kind="user", source_value="u")
    _check(doc["revision"] == 3 and doc["entries"][0]["status"] == "accepted", "set_entry_status 应生效")
    sha = decisions.hash_decision(Path(RULES_PATH))
    _check(len(sha) == 64 and all(c in "0123456789abcdef" for c in sha), "决策哈希应为 64 位十六进制")


# ── checks ──────────────────────────────────────────────────────────────


def run_checks_basic() -> None:
    # 正文纯度：干净文本通过
    clean = "第一章 相遇\n\n森田推门进入大厅。\n\n老板答应借宿。"
    _check(checks.purity_issues(clean) == [], "纯正文不应有纯度问题")
    _check(checks.purity_check(clean).result == "pass", "纯正文纯度应 pass")
    # F-001 / F-002：计划与爽点对照泄漏
    leaked = "## 写前计划\n\n- 铺垫冲突\n\n正文……"
    issues = checks.purity_issues(leaked)
    _check(issues, "写前计划应被拒绝")
    _check(checks.purity_check(leaked).result == "fail", "泄漏纯度应 fail")
    leaked2 = "## 本批爽点对照\n\n|爽点|位置|\n|---|---|\n|打脸|p-0001|"
    _check(checks.purity_issues(leaked2), "爽点对照表应被拒绝")
    frontmatter = "---\ntitle: x\n---\n正文"
    _check(checks.purity_issues(frontmatter), "frontmatter 应被拒绝")

    # 分段 ID
    pids = checks.paragraph_ids(clean)
    _check(len(pids) == 3 and all(pid.startswith("p-") and pid[2:].isdigit() for pid, _ in pids),
           "分段 ID 应为 p-NNNN")
    _check(pids[0][0] == "p-0001" and pids == checks.paragraph_ids(clean), "分段 ID 应稳定")
    _check("p-0001" not in clean, "分段 ID 不得写入正文")

    # 事件级对照：占位正文（与原文一致）应全部通过
    graph = _build_events()
    pids = checks.paragraph_ids(SOURCE_TEXT)
    results, aggregate = checks.event_comparison_check(graph, pids)
    _check(aggregate == "pass", f"事件对照应 pass: {aggregate}")
    _check(len(results) == 2 and all(r["paragraph_ids"] for r in results), "每个事件都应有段落 ID 证据")
    _check(all("p-" in r["evidence"] for r in results), "证据必须携带段落 ID（F-014）")

    # 事件缺失 → fail
    missing_text = "第一章 相遇\n\n完全无关的正文内容。"
    results, aggregate = checks.event_comparison_check(graph, checks.paragraph_ids(missing_text))
    _check(aggregate == "fail", "事件缺失应 fail")

    # 必替换残留分级
    tiers = {"must_replace": ["森田"], "functional_adaptation": ["神社"], "default_keep": []}
    result, warnings = checks.residue_check("森田推门进入大厅。", tiers, [])
    _check(result.result == "fail", "未决必替换残留应 fail（C-049）")
    accepted = [decisions.new_entry(
        category="mapping", target="森田", choice="保留原名",
        source_kind="user", source_value="u", status="accepted",
    )]
    result, warnings = checks.residue_check("森田推门进入大厅。", tiers, accepted)
    _check(result.result == "pass" and any(w.result == "warning" for w in warnings),
           "用户决策覆盖后不得误报失败（C-050）")
    result, _ = checks.residue_check("神社前的祭典开始了。", tiers, [])
    _check(result.result == "blocked", "功能适配元素未决应 blocked（C-045）")

    # 自身映射（source == target）在 must_replace 中永不可满足，不得判 fail
    # （真实 DeepSeek 日语源实测暴露：字典曾含 校服→校服，导致正确译文被判残留）
    identity_tiers = {"must_replace": [{"source": "校服", "target": "校服"}],
                      "functional_adaptation": [], "default_keep": []}
    result, _ = checks.residue_check("他穿着校服走进教室。", identity_tiers, [])
    _check(result.result == "pass", "自身映射不得判为必替换残留")
    # 真正的必替换词仍必须 fail（不因跳过自身映射而放松）
    real_tiers = {"must_replace": [{"source": "水手服", "target": "校服"}],
                  "functional_adaptation": [], "default_keep": []}
    result, _ = checks.residue_check("他穿着水手服走进教室。", real_tiers, [])
    _check(result.result == "fail", "真实必替换词残留仍应 fail")

    # 篇幅
    _check(checks.length_check(SOURCE_TEXT, SOURCE_TEXT).result == "pass", "篇幅比正常应 pass")
    short = checks.length_check(SOURCE_TEXT, "第一章 相遇\n\n太短。")
    _check(short.result == "warning" and "漏写" in short.message, "过短应 warning")

    # 写作指导六项一致性旁车
    items = checks.writing_guide_consistency(None)
    _check(len(items) == 6 and all(i.result == "not_run" for i in items),
           "写作指导未启用时应 6 项 not_run")
    provider = lambda dimensions, output_text, source_text: [
        (dim, "fail", "证据不足") for dim in dimensions
    ]
    items = checks.writing_guide_consistency(provider)
    _check(any(i.name == "writing_guide_consistency/aggregate" and i.result == "blocked" for i in items),
           "≥2 项一致性不稳应按写作指导规则 blocked")


def run_checks_document() -> None:
    ws = _build_workspace(Path(tempfile.mkdtemp()), [])
    chapter = _make_chapter(
        ws, chapter_id="ch-001", ordinal=1, title="第一章 相遇",
        state="awaiting_checks", output_text=SOURCE_TEXT,
    )
    manifest = read_json(ws / "manifest.json")
    manifest["chapters"] = [chapter]
    write_json_atomic(ws / "manifest.json", manifest)
    recorded = chapter["fingerprints"]
    input_hashes = {
        key: recorded[key]
        for key in ("source", "events", "project_decisions", "chapter_decisions", "rules")
    }
    doc = checks.run_checks(
        chapter_id="ch-001",
        source_text=SOURCE_TEXT,
        output_text=SOURCE_TEXT,
        events_graph=_build_events(),
        project_decisions=read_json(ws / "project-decisions.json"),
        chapter_decisions=_empty_chapter_decisions(),
        input_hashes=input_hashes,
    )
    _validate_checks_doc(doc)
    _check(doc["hard_gate"] == "pass" and doc["event_comparison"] == "pass", "正常章节应 pass")
    _check(doc["purity_result"] == "pass" and doc["decision_result"] == "pass", "purity/decision 结果应 pass")
    _check(len(doc["event_results"]) == 2, "事件对照记录数应为事件数")

    # 记录到 manifest
    updated = checks.record_checks(ws, "ch-001", doc)
    summary = updated["chapters"][0]["checks"]
    _check(summary["hard_gate"] == "pass" and summary["last_run"], "checks 摘要应写入 manifest")
    _check(updated["chapters"][0]["fingerprints"]["checks"] != "0" * 64,
           "checks 指纹应写入 manifest")

    # input_fingerprint 不一致 → 拒绝
    bad = dict(doc)
    bad["input_fingerprint"] = "0" * 64
    try:
        checks.record_checks(ws, "ch-001", bad)
        _check(False, "指纹不一致应抛 SchemaError")
    except SchemaError:
        pass

    # 纯泄漏正文 → fail
    leaked_doc = checks.run_checks(
        chapter_id="ch-001",
        source_text=SOURCE_TEXT,
        output_text="## 写前计划\n\n正文……",
        events_graph=_build_events(),
        project_decisions=read_json(ws / "project-decisions.json"),
        chapter_decisions=_empty_chapter_decisions(),
        input_hashes=input_hashes,
    )
    _check(leaked_doc["hard_gate"] == "fail" and leaked_doc["purity_result"] == "fail",
           "泄漏正文 hard_gate 应 fail")
    _check(checks.next_state_after_checks(leaked_doc, manifest) == "rework", "失败检查应建议 rework")


# ── rewrite ─────────────────────────────────────────────────────────────


def run_rewrite() -> None:
    ws = _build_workspace(Path(tempfile.mkdtemp()), [])
    chapter = _make_chapter(
        ws, chapter_id="ch-001", ordinal=1, title="第一章 相遇",
        state="ready_for_rewrite",
    )
    manifest = read_json(ws / "manifest.json")
    manifest["chapters"] = [chapter]
    write_json_atomic(ws / "manifest.json", manifest)

    # ready_for_rewrite 且输入齐备应可改写
    _check(rewrite.verify_rewrite_readiness(ws, "ch-001", manifest) == [],
           "ready_for_rewrite 且输入齐备应可改写")

    # begin_rewrite：ready_for_rewrite → rewriting
    ctx = rewrite.begin_rewrite(ws, "ch-001")
    _check(ctx.source_text == SOURCE_TEXT and ctx.title == "第一章 相遇", "改写上下文应完整")
    rewritten = ManifestStore(ws).load()
    _check(rewritten["chapters"][0]["state"] == "rewriting", "begin_rewrite 应迁移到 rewriting")

    # 确定性占位：纯正文、无内部元数据、确定性可复现
    text = rewrite.rewrite_text(ctx)
    _check(checks.purity_issues(text) == [], "占位正文必须纯正文")
    _check(text == rewrite.rewrite_text(ctx), "占位改写应确定性可复现")

    # commit_output：rewriting → awaiting_checks + output 指纹
    updated = rewrite.commit_output(ws, "ch-001", text)
    committed = updated["chapters"][0]
    _check(committed["state"] == "awaiting_checks", "commit_output 应迁移到 awaiting_checks")
    _check((ws / committed["paths"]["output"]).is_file(), "output.md 应已写入")

    # 未 begin 直接 commit（状态仍 ready_for_rewrite）应拒绝
    ws2 = _build_workspace(Path(tempfile.mkdtemp()), [])
    chapter2 = _make_chapter(
        ws2, chapter_id="ch-001", ordinal=1, title="第一章 相遇",
        state="ready_for_rewrite",
    )
    manifest2 = read_json(ws2 / "manifest.json")
    manifest2["chapters"] = [chapter2]
    write_json_atomic(ws2 / "manifest.json", manifest2)
    try:
        rewrite.commit_output(ws2, "ch-001", "第一章 相遇\n\n正文")
        _check(False, "ready_for_rewrite 直接 commit 应抛 StateError")
    except StateError:
        pass


def run_full_pipeline_to_passed(tmp: Path) -> Path:
    """完整链路：workspace → ready_for_rewrite → rewriting → awaiting_checks → checks → passed。"""
    ws = _build_workspace(tmp, [])
    chapter = _make_chapter(
        ws, chapter_id="ch-001", ordinal=1, title="第一章 相遇",
        state="ready_for_rewrite",
    )
    manifest = read_json(ws / "manifest.json")
    manifest["chapters"] = [chapter]
    write_json_atomic(ws / "manifest.json", manifest)

    ctx = rewrite.begin_rewrite(ws, "ch-001")
    text = rewrite.rewrite_text(ctx)
    rewrite.commit_output(ws, "ch-001", text)
    manifest = ManifestStore(ws).load()
    recorded = manifest["chapters"][0]["fingerprints"]
    input_hashes = {
        key: recorded[key]
        for key in ("source", "events", "project_decisions", "chapter_decisions", "rules")
    }
    doc = checks.run_checks(
        chapter_id="ch-001",
        source_text=ctx.source_text,
        output_text=text,
        events_graph=ctx.events,
        project_decisions=ctx.project_decisions,
        chapter_decisions=ctx.chapter_decisions,
        input_hashes=input_hashes,
    )
    checks.record_checks(ws, "ch-001", doc)
    store = ManifestStore(ws)
    store.transition_chapter("ch-001", "passed", reason="checks 通过")
    return ws


def run_rewrite_pipeline(tmp: Path) -> None:
    ws = run_full_pipeline_to_passed(tmp)
    manifest = ManifestStore(ws).load()
    _check(manifest["chapters"][0]["state"] == "passed", "完整链路后章节应为 passed")
    _check(manifest["chapters"][0]["checks"]["hard_gate"] == "pass", "passed 章节 hard_gate 应为 pass")


# ── merge ───────────────────────────────────────────────────────────────


def run_merge(tmp: Path) -> None:
    # 两章全通过并经批准 → 正式合并
    ws = _build_workspace(tmp, [])
    ch1 = _make_chapter(
        ws, chapter_id="ch-001", ordinal=1, title="第一章 相遇",
        state="approved", output_text=SOURCE_TEXT, checks_summary=_passed_checks_summary(),
    )
    ch2 = _make_chapter(
        ws, chapter_id="ch-002", ordinal=2, title="第二章 出发",
        state="approved", output_text="第二章 出发\n\n两人清晨出发前往北境。",
        checks_summary=_passed_checks_summary(),
    )
    manifest = read_json(ws / "manifest.json")
    manifest["chapters"] = [ch1, ch2]
    write_json_atomic(ws / "manifest.json", manifest)

    report = merge.merge_output(ws)
    _check(report["ok"] is True and report["merged"] == ["ch-001", "ch-002"], "全通过应合并成功")
    localized = (ws / "output" / "localized.md").read_text(encoding="utf-8")
    _check("第一章 相遇" in localized and "第二章 出发" in localized, "合并正文应含两章标题")
    _check(localized.index("第二章") > localized.index("第一章"), "合并应按 ordinal 排序")

    updated = ManifestStore(ws).load()
    states = {c["id"]: c["state"] for c in updated["chapters"]}
    _check(states == {"ch-001": "delivered", "ch-002": "delivered"}, "合并后 approved 应转 delivered")
    _check(updated["output"]["sha256"], "合并后应记录正式输出哈希")

    # 存在 rework 章节 → 默认拒绝，不静默跳过（C-081）
    ws2 = _build_workspace(tmp, [])
    a = _make_chapter(ws2, chapter_id="ch-001", ordinal=1, title="第一章",
                      state="approved", output_text="第一章\n\n正文A", checks_summary=_passed_checks_summary())
    b = _make_chapter(ws2, chapter_id="ch-002", ordinal=2, title="第二章", state="rework")
    manifest = read_json(ws2 / "manifest.json")
    manifest["chapters"] = [a, b]
    write_json_atomic(ws2 / "manifest.json", manifest)
    report = merge.merge_output(ws2)
    _check(report["ok"] is False and any("不可合并" in i for i in report["issues"]),
           "存在 rework 应拒绝合并")
    _check(not (ws2 / "output" / "localized.md").exists() or
           (ws2 / "output" / "localized.md").read_text(encoding="utf-8") != "第一章\n\n正文A\n",
           "拒绝合并时不得写入/覆盖正式输出")

    # 序号缺口（C-084）
    ws3 = _build_workspace(tmp, [])
    x = _make_chapter(ws3, chapter_id="ch-001", ordinal=1, title="第一章",
                      state="approved", output_text="第一章\n\n正文A", checks_summary=_passed_checks_summary())
    y = _make_chapter(ws3, chapter_id="ch-003", ordinal=3, title="第三章",
                      state="approved", output_text="第三章\n\n正文C", checks_summary=_passed_checks_summary())
    z = _make_chapter(ws3, chapter_id="ch-002", ordinal=2, title="第二章", state="rework")
    manifest = read_json(ws3 / "manifest.json")
    manifest["chapters"] = [x, y, z]
    write_json_atomic(ws3 / "manifest.json", manifest)
    report = merge.merge_output(ws3, allow_incomplete=True)
    _check(report["ok"] is False and any("缺口" in i for i in report["issues"]),
           "候选序号缺口应拒绝合并（C-084）")

    # 哈希不一致（修改 output）→ 整次失败，旧正式输出保留（C-085 / F-018）
    ws4 = _build_workspace(tmp, [])
    ch = _make_chapter(
        ws4, chapter_id="ch-001", ordinal=1, title="第一章",
        state="approved", output_text="第一章\n\n正文A", checks_summary=_passed_checks_summary(),
    )
    manifest = read_json(ws4 / "manifest.json")
    manifest["chapters"] = [ch]
    write_json_atomic(ws4 / "manifest.json", manifest)
    # 先成功合并一次
    report = merge.merge_output(ws4)
    _check(report["ok"] is True, "首次合并应成功")
    old = (ws4 / "output" / "localized.md").read_text(encoding="utf-8")
    # 篡改 output.md（伪造新决策结果）
    (ws4 / "chapters" / "ch-001" / "output.md").write_text("第一章\n\n被篡改的正文", encoding="utf-8")
    report = merge.merge_output(ws4)
    _check(report["ok"] is False and any("指纹" in i for i in report["issues"]),
           "output 哈希变化应使合并失败")
    _check((ws4 / "output" / "localized.md").read_text(encoding="utf-8") == old,
           "失败合并不得覆盖旧正式输出")

    # 带风险版隔离（R18 / C-087）
    ws5 = _build_workspace(tmp, [])
    ch = _make_chapter(ws5, chapter_id="ch-001", ordinal=1, title="第一章",
                       state="ready_for_rewrite")
    manifest = read_json(ws5 / "manifest.json")
    manifest["chapters"] = [ch]
    write_json_atomic(ws5 / "manifest.json", manifest)
    exp = merge.write_experimental(
        ws5, "ch-001", "第一章\n\n实验版正文", "用户坚持保留敏感设定，存在平台风险，未审核"
    )
    _check(exp.as_posix().endswith("output/experimental/ch-001.md"), "实验版必须走独立路径")
    m = ManifestStore(ws5).load()
    _check(m["chapters"][0]["experimental_output"] == "output/experimental/ch-001.md",
           "manifest 应登记 experimental_output")
    _check("风险" in m["chapters"][0]["experimental_risk"], "manifest 应登记 experimental_risk")
    _check(m["chapters"][0]["state"] == "ready_for_rewrite", "实验版不得改变章节状态")
    # 实验版永不参与正式合并
    manifest = ManifestStore(ws5).load()
    manifest["chapters"][0]["state"] = "approved"
    manifest["chapters"][0]["checks"] = _passed_checks_summary()
    manifest["chapters"][0]["blockers"] = []
    write_json_atomic(ws5 / "manifest.json", manifest)
    append_audit(ws5 / "audit.jsonl", "state_change_detail", chapter_id="ch-001", to="approved")
    report = merge.merge_output(ws5)
    _check(report["ok"] is False and any("风险" in i for i in report["issues"]),
           "带风险版章节不得合并")
    _check(not (ws5 / "output" / "localized.md").exists(), "带风险版不得生成正式输出")


def test_decisions() -> None:
    run_decisions()


def test_checks_basic() -> None:
    run_checks_basic()


def test_checks_document() -> None:
    run_checks_document()


def test_rewrite() -> None:
    run_rewrite()


def test_rewrite_pipeline(tmp_path: Path) -> None:
    run_rewrite_pipeline(tmp_path)


def test_merge(tmp_path: Path) -> None:
    run_merge(tmp_path)


def test_stale_fingerprints_downgrade_passed(tmp_path: Path) -> None:
    """passed/delivered 章节在 source 等输入指纹变化后应降级到 ready_for_rewrite。"""
    ws = _build_workspace(tmp_path, [])
    ch = _make_chapter(
        ws, chapter_id="ch-001", ordinal=1, title="第一章 相遇",
        state="passed", output_text=SOURCE_TEXT, checks_summary=_passed_checks_summary(),
    )
    manifest = read_json(ws / "manifest.json")
    manifest["chapters"] = [ch]
    write_json_atomic(ws / "manifest.json", manifest)

    store = ManifestStore(ws)
    before = store.load()
    old_source_fp = before["chapters"][0]["fingerprints"]["source"]

    # 模拟用户改稿：source 指纹变化
    (ws / "chapters" / "ch-001" / "source.md").write_text(
        SOURCE_TEXT + "\n\n新增段落。", encoding="utf-8"
    )

    downgraded, refreshed = store.refresh_stale_chapters()
    assert len(downgraded) == 1
    cid, target, changed = downgraded[0]
    assert cid == "ch-001"
    assert target == "ready_for_rewrite"
    assert "source" in changed

    after = ManifestStore(ws).load()
    chapter = after["chapters"][0]
    assert chapter["state"] == "ready_for_rewrite"
    assert any(b.get("code") == "STALE_INPUT_FINGERPRINT" for b in chapter["blockers"])
    assert chapter["fingerprints"]["source"] != old_source_fp


def test_stale_fingerprints_downgrade_awaiting_checks(tmp_path: Path) -> None:
    """awaiting_checks / awaiting_review 章节在输入变化后应降级到 rework。"""
    ws = _build_workspace(tmp_path, [])
    ch = _make_chapter(
        ws, chapter_id="ch-001", ordinal=1, title="第一章 相遇",
        state="awaiting_checks", output_text=SOURCE_TEXT,
    )
    manifest = read_json(ws / "manifest.json")
    manifest["chapters"] = [ch]
    write_json_atomic(ws / "manifest.json", manifest)

    store = ManifestStore(ws)
    (ws / "chapters" / "ch-001" / "source.md").write_text(
        SOURCE_TEXT + "\n\n又改了。", encoding="utf-8"
    )

    downgraded, refreshed = store.refresh_stale_chapters()
    assert len(downgraded) == 1
    assert downgraded[0][1] == "rework"
    assert "source" in downgraded[0][2]

    after = ManifestStore(ws).load()
    assert after["chapters"][0]["state"] == "rework"


def main() -> int:
    run_decisions()
    run_checks_basic()
    run_checks_document()
    run_rewrite()
    with tempfile.TemporaryDirectory() as tmpdir:
        run_rewrite_pipeline(Path(tmpdir))
        run_merge(Path(tmpdir))
    print("v4 Core checks/decisions/rewrite/merge 全部自测通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
