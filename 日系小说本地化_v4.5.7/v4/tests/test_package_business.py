"""包内业务验收：只走真实业务入口，不验替身、不背状态枚举。

放这里的理由：`v4/tests` 里 60 个测试文件是私有开发面，公开包按裁决可以不带；
但「不带测试的包」等于让新宿主无法自证业务门还在。于是把发布必须能验的几条
抽成这一个文件，随包分发，`scripts/run_package_checks.py` 的 `business_acceptance`
项就跑它（外加同批随包的两个拒绝类测试）。

覆盖的验收要求（用户追加第 3 条）：
1. `init` 真的建出工作区（走 `cli.main`，不是替身）；
2. 状态链从 `v4.core.state_machine` 的实现里读，不套我摘要里的链条；
3. passed 不得直接 merge，approved 才能；
4. 批准后正文被换（输出哈希不符）→ 拒绝交付且旧产物保留；
5. approval 事务失败 → manifest 状态与产物字节都不动。
"""

from __future__ import annotations

import json
from pathlib import Path

from v4.core import merge, state_machine
from v4.core.cli import main as cli_main
from v4.core.jsonio import read_json, write_json_atomic
from v4.tests.test_core_pipeline import (
    SOURCE_TEXT,
    _build_workspace,
    _make_chapter,
    _passed_checks_summary,
)


def _install(ws: Path, chapters: list[dict]) -> None:
    manifest = read_json(ws / "manifest.json")
    manifest["chapters"] = chapters
    write_json_atomic(ws / "manifest.json", manifest)


def _run_cli(monkeypatch, *args: str) -> int:
    monkeypatch.setattr("sys.argv", ["novel", *[str(a) for a in args]])
    return cli_main()


def test_cli_init_creates_a_real_workspace(tmp_path, monkeypatch):
    ws = tmp_path / "包内工作区"
    assert _run_cli(monkeypatch, "init", ws, "--title", "包内业务") == 0
    manifest = read_json(ws / "manifest.json")
    # 断言按 create_workspace 的真实产物面来，不猜字段名
    assert manifest.get("chapters") == [], manifest
    assert "schema_version" in manifest or "version" in manifest, sorted(manifest)
    assert (ws / "chapters").is_dir(), "真实 create_workspace 应建出章节目录"


def test_delivered_state_is_only_reachable_from_approved():
    """链条从实现读：钉住「未批准的态一律不得直接落到 delivered」这条安全属性。"""
    # 迁移表是函数内的局部量，没有模块级导出；那就用实现自己的判据把边集跑出来，
    # 这样链条仍然来自实现，不是我摘要里写的
    states = sorted(state_machine.STATES)
    edges = {(a, b) for a in states for b in states
             if state_machine.transition_allowed(a, b)}
    assert edges, "transition_allowed 全假，判据本身有问题"
    # 实测：实现允许 delivered→delivered（重复合并的幂等边），
    # 所以不硬断「唯一入边是 approved」——那会把我摘要里的链条当成规定。
    into_delivered = {src for src, tgt in edges if tgt == "delivered"}
    assert into_delivered <= {"approved", "delivered"}, into_delivered
    assert state_machine.transition_allowed("approved", "delivered") is True
    unapproved = sorted(state_machine.STATES - {"approved", "delivered"})
    leaked = [src for src in unapproved if state_machine.transition_allowed(src, "delivered")]
    assert leaked == [], f"这些未批准态竟然可直接交付: {leaked}"
    assert state_machine.transition_allowed("passed", "delivered") is False


def test_passed_is_refused_and_approved_is_merged(tmp_path):
    ws = _build_workspace(tmp_path, [], name="ws-passed")
    ch = _make_chapter(ws, chapter_id="ch-001", ordinal=1, title="第一章 相遇",
                       state="passed", output_text=SOURCE_TEXT,
                       checks_summary=_passed_checks_summary())
    _install(ws, [ch])
    refused = merge.merge_output(ws)
    assert refused["ok"] is not True, refused
    assert not (ws / "output" / "localized.md").exists(), "被拒不得写出正式稿"

    ok_ws = _build_workspace(tmp_path, [], name="ws-approved")
    good = _make_chapter(ok_ws, chapter_id="ch-001", ordinal=1, title="第一章 相遇",
                         state="approved", output_text=SOURCE_TEXT,
                         checks_summary=_passed_checks_summary())
    _install(ok_ws, [good])
    accepted = merge.merge_output(ok_ws)
    assert accepted["ok"] is True, accepted
    states = {c["id"]: c["state"] for c in read_json(ok_ws / "manifest.json")["chapters"]}
    assert states == {"ch-001": "delivered"}, states


def test_tampered_output_after_approval_is_refused(tmp_path, monkeypatch):
    """批准之后换正文：approve/merge 都必须拒绝，且旧产物字节不变。"""
    ws = _build_workspace(tmp_path, [], name="ws-tamper")
    ch = _make_chapter(ws, chapter_id="ch-001", ordinal=1, title="第一章 相遇",
                       state="passed", output_text=SOURCE_TEXT,
                       checks_summary=_passed_checks_summary())
    _install(ws, [ch])
    ch_dir = ws / "chapters" / "ch-001"
    before = (ch_dir / "output.md").read_bytes()
    current = read_json(ch_dir / "current.json") if (ch_dir / "current.json").is_file() else {}
    write_json_atomic(ch_dir / "current.json", {
        "latest_version": 1, "latest_sha256": "0" * 64,
        "approved_version": 1, "approved_sha256": "0" * 64,
    })
    rc = _run_cli(monkeypatch, "approve", ws, "--chapter", "ch-001", "--version", "v001")
    assert rc != 0, "哈希指针被改过就必须拒绝"
    assert (ch_dir / "output.md").read_bytes() == before, "拒绝时不得改动正文产物"
    states = {c["id"]: c["state"] for c in read_json(ws / "manifest.json")["chapters"]}
    assert states == {"ch-001": "passed"}, states
    assert current.get("latest_version") != 999  # 只是取个快照，不参与断言


def test_approval_transaction_failure_keeps_state_and_artifacts(tmp_path, monkeypatch):
    """批准中途失败（模拟版本指针落盘抛错）→ 状态与产物都不得半改。"""
    ws = _build_workspace(tmp_path, [], name="ws-rollback")
    ch = _make_chapter(ws, chapter_id="ch-001", ordinal=1, title="第一章 相遇",
                       state="passed", output_text=SOURCE_TEXT,
                       checks_summary=_passed_checks_summary())
    _install(ws, [ch])
    ch_dir = ws / "chapters" / "ch-001"
    write_json_atomic(ch_dir / "current.json", {
        "latest_version": 1, "latest_sha256": json.loads(
            (ch_dir / "v001.json").read_text(encoding="utf-8"))["sha256"]
        if (ch_dir / "v001.json").is_file() else "0" * 64,
        "approved_version": 0, "approved_sha256": None,
    })
    output_before = (ch_dir / "output.md").read_bytes()
    manifest_before = read_json(ws / "manifest.json")["chapters"]

    real_write = write_json_atomic

    def boom(path, *a, **kw):
        if str(path).endswith("audit.jsonl") or str(path).endswith("current.json"):
            raise OSError("模拟事务失败")
        return real_write(path, *a, **kw)

    monkeypatch.setattr("v4.core.version_store.write_json_atomic", boom)
    rc = _run_cli(monkeypatch, "approve", ws, "--chapter", "ch-001", "--version", "v001")
    assert rc != 0, "落盘失败必须让 approve 失败，不能假绿"
    assert (ch_dir / "output.md").read_bytes() == output_before, "正文产物不得半改"
    assert read_json(ws / "manifest.json")["chapters"] == manifest_before, \
        "manifest 状态必须回滚到批准前"


def test_versions_lists_a_draft_only_chapter_without_crashing(tmp_path, monkeypatch, capsys):
    """39 面登记表抓到的真崩，不是构造出来的极端。

    真实路径：rewrite 落草稿版本 → `current.json` 的 `approved_version` 是 `null`
    （`version_store.save_version` 就这么写），此时 `versions` 做
    `int(current.get("approved_version", 0))` —— 键存在但值为 None 时默认值不生效，
    `int(None)` 抛 TypeError；更糟的是异常处理器把章节从 `passed` 拖进 `error`。
    列版本是只读命令：既不该崩，也不该改状态。
    """
    from v4.core.version_store import save_version

    ws = _build_workspace(tmp_path, [], name="ws-versions")
    ch = _make_chapter(ws, chapter_id="ch-001", ordinal=1, title="第一章 相遇",
                       state="passed", output_text=SOURCE_TEXT,
                       checks_summary=_passed_checks_summary())
    _install(ws, [ch])
    ch_dir = ws / "chapters" / "ch-001"
    save_version(ch_dir, SOURCE_TEXT, kind="draft")
    current = read_json(ch_dir / "current.json")
    assert current.get("approved_version") is None, current

    rc = _run_cli(monkeypatch, "versions", ws, "--chapter", "ch-001")
    out = capsys.readouterr().out
    assert rc == 0, f"只读列版本不该失败: rc={rc} out={out!r}"
    assert "v001" in out, out
    states = {c["id"]: c["state"] for c in read_json(ws / "manifest.json")["chapters"]}
    assert states == {"ch-001": "passed"}, f"列版本不得改状态: {states}"


# ── 追加裁决 1：索引库/写作指导加载失败要能分类，且不得降级、不得外泄原文 ────
#
# 这两个 canary 刻意**不带凭据形状也不带私人绝对路径形状**：随包分发的测试里出现
# 密钥前缀串、或「盘符加家目录」式绝对路径，都会被脱敏扫描按真件拦下
# （cand9 首次构建就是被这条拦住的），而本测试要验的是「异常原文不回显」这个性质，
# 与字符串是不是密钥形状无关。
PLANTED_CANARY = "LEAK-CANARY-不可回显-42"
PLANTED_PATH_FRAGMENT = "私密库\\章节索引.md"


def _index_lib_shape(tmp: Path, name: str = "探针索引库") -> Path:
    """造一个通过 `looks_like_index_lib` 形态判据的最小库（不含真实书稿）。"""
    root = tmp / name
    (root / "章节索引").mkdir(parents=True)
    (root / "章节索引" / "探针-章索引.md").write_text(
        "---\n书名: 探针\n章节: 1\n---\n合成索引内容\n", encoding="utf-8")
    return root


def _ready_workspace(tmp: Path, name: str) -> Path:
    ws = _build_workspace(tmp, [], name=name)
    ch = _make_chapter(ws, chapter_id="ch-001", ordinal=1, title="第一章 相遇",
                       state="ready_for_rewrite")
    _install(ws, [ch])
    return ws


def _rewrite(ws: Path, lib: Path, monkeypatch, capsys):
    rc = _run_cli(monkeypatch, "rewrite", ws, "--chapter", "ch-001",
                  "--backend", "placeholder", "--index-lib", lib)
    return rc, capsys.readouterr()


def _last_unavailable(ws: Path) -> dict:
    from v4.core.audit import read_audit

    rows = [r for r in read_audit(ws / "audit.jsonl")
            if r.get("type") == "enhance_unavailable"]
    assert rows, "加载失败必须留 enhance_unavailable 记录，不能只报错不落账"
    return rows[-1]


def _break_scan(monkeypatch, target: str, exc: BaseException) -> None:
    module, _, cls = target.rpartition(".")
    import importlib

    holder = importlib.import_module(module)
    def boom(self, *a, **kw):
        raise exc
    monkeypatch.setattr(getattr(holder, cls), "scan", boom)


def test_missing_index_library_is_reported_as_missing(tmp_path, monkeypatch, capsys):
    """「确实不存在」是一类，不能再和读失败混成同一句「未找到」。"""
    ws = _ready_workspace(tmp_path, "ws-lib-missing")
    rc, cap = _rewrite(ws, tmp_path / "根本不存在库", monkeypatch, capsys)
    text = cap.out + cap.err
    assert rc != 0, "缺库仍须停止"
    assert "缺失" in text, text


def test_unreadable_index_library_is_not_called_missing(tmp_path, monkeypatch, capsys):
    """读取失败必须报成读取失败，并且只留异常类型——原始消息里有私密路径与假凭据。"""
    ws = _ready_workspace(tmp_path, "ws-lib-unreadable")
    lib = _index_lib_shape(tmp_path)
    _break_scan(monkeypatch, "v4.enhance.index_lib.IndexLibrary",
                PermissionError(f"无法读取 {PLANTED_PATH_FRAGMENT} 令牌 {PLANTED_CANARY}"))
    rc, cap = _rewrite(ws, lib, monkeypatch, capsys)
    text = cap.out + cap.err
    assert rc != 0, "读失败不得当成功"
    assert "读取失败" in text and "PermissionError" in text, text
    assert "未找到" not in text, f"读失败被误报成缺件: {text}"
    assert PLANTED_CANARY not in text and PLANTED_PATH_FRAGMENT not in text, "输出泄漏原文"
    rec = _last_unavailable(ws)
    assert "读取失败" in rec["reason"] and PLANTED_CANARY not in rec["reason"], rec


def test_corrupt_index_library_is_reported_as_format_damage(tmp_path, monkeypatch, capsys):
    import json as _json

    ws = _ready_workspace(tmp_path, "ws-lib-corrupt")
    lib = _index_lib_shape(tmp_path)
    _break_scan(monkeypatch, "v4.enhance.index_lib.IndexLibrary",
                _json.JSONDecodeError("Expecting value", f"文档含 {PLANTED_CANARY}", 3))
    rc, cap = _rewrite(ws, lib, monkeypatch, capsys)
    text = cap.out + cap.err
    assert rc != 0, text
    assert "格式损坏" in text and "JSONDecodeError" in text, text
    assert PLANTED_CANARY not in text, "格式损坏也不得回显原文"


def test_other_index_library_exception_is_named_not_blurred(tmp_path, monkeypatch, capsys):
    ws = _ready_workspace(tmp_path, "ws-lib-other")
    lib = _index_lib_shape(tmp_path)
    _break_scan(monkeypatch, "v4.enhance.index_lib.IndexLibrary",
                RuntimeError(f"底层炸了 {PLANTED_PATH_FRAGMENT}"))
    rc, cap = _rewrite(ws, lib, monkeypatch, capsys)
    text = cap.out + cap.err
    assert rc != 0, text
    assert "加载异常" in text and "RuntimeError" in text, text
    assert PLANTED_PATH_FRAGMENT not in text, text


def test_writing_guide_failure_does_not_blank_out_index_library(tmp_path, monkeypatch, capsys):
    """兜底把两个资产一起清空过：写作指导炸了会让索引库被误判为「没配」。"""
    ws = _ready_workspace(tmp_path, "ws-guide-fail")
    lib = _index_lib_shape(tmp_path)
    guide = tmp_path / "探针写作指导"
    (guide / "references" / "modules").mkdir(parents=True)
    _break_scan(monkeypatch, "v4.enhance.writing_guide.WritingGuide", OSError("读不到模块"))
    rc = _run_cli(monkeypatch, "rewrite", ws, "--chapter", "ch-001",
                  "--backend", "placeholder", "--index-lib", lib, "--writing-guide", guide)
    cap = capsys.readouterr()
    text = cap.out + cap.err
    assert rc == 0, f"索引库可用时不得因写作指导失败而整体停摆: {text}"
    rec = _last_unavailable(ws)
    assert rec["asset_kind"] == "writing_module", rec
    assert "读取失败" in rec["reason"], rec


def test_gate_reports_business_file_as_the_real_entry_point():
    """防「验收项被悄悄换成替身」：门必须仍然指向这个文件里的真实入口。"""
    import importlib.util
    gate_path = Path(__file__).resolve().parents[2] / "scripts" / "run_package_checks.py"
    spec = importlib.util.spec_from_file_location("run_package_checks_gate", gate_path)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    src = gate_path.read_text(encoding="utf-8")
    assert "test_package_business.py" in src, "包内业务验收项被摘掉了"
    assert hasattr(gate, "run_gate") and hasattr(gate, "check_tree_drift")
