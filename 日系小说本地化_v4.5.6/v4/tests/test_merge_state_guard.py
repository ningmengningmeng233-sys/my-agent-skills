"""工单 T08 第 3 项①：passed 状态不得直接合并，必须先经 approve。

实现里的守卫写在 `v4/core/merge.py`（只选 state ∈ {approved, delivered}，
遇到 passed 给出「草稿需先经由 novel approve 审批通过」），但既有测试只用
`state="approved"` 正向路径，从没钉过这条拒绝——所以这里补上，并配一条
同夹具的正对照（approved 能合），这样守卫一旦被删，本文件必红而不是全体沉默。

夹具直接复用 test_core_pipeline 的构造器，不再抄一份 manifest 写法。
"""

from __future__ import annotations

import pathlib

from v4.core import merge
from v4.core.jsonio import read_json, write_json_atomic
from v4.tests.test_core_pipeline import (
    SOURCE_TEXT,
    _build_workspace,
    _make_chapter,
    _passed_checks_summary,
)

GUARD_TEXT = "不可合并"
APPROVE_HINT = "novel approve"


def _install(ws: pathlib.Path, chapters: list) -> None:
    manifest = read_json(ws / "manifest.json")
    manifest["chapters"] = chapters
    write_json_atomic(ws / "manifest.json", manifest)


def test_passed_chapter_is_refused_by_merge_until_approved(tmp_path):
    ws = _build_workspace(tmp_path, [])
    ch = _make_chapter(ws, chapter_id="ch-001", ordinal=1, title="第一章 相遇",
                       state="passed", output_text=SOURCE_TEXT,
                       checks_summary=_passed_checks_summary())
    _install(ws, [ch])

    report = merge.merge_output(ws)

    assert report["ok"] is not True, report
    assert report.get("merged") in ([], None), report
    reasons = " ".join(str(item.get("reason", "")) for item in report["excluded"])
    assert GUARD_TEXT in reasons, reasons
    assert APPROVE_HINT in reasons, f"拒绝理由要指明出路：{reasons}"
    assert not (ws / "output" / "localized.md").exists(), "被拒的草稿不得写出正式稿"


def test_approved_chapter_is_the_positive_control(tmp_path):
    """同一夹具换成 approved 必须能合：证明上一条红的是状态守卫，不是夹具搭错。"""
    ws = _build_workspace(tmp_path, [])
    ch = _make_chapter(ws, chapter_id="ch-001", ordinal=1, title="第一章 相遇",
                       state="approved", output_text=SOURCE_TEXT,
                       checks_summary=_passed_checks_summary())
    _install(ws, [ch])

    report = merge.merge_output(ws)

    assert report["ok"] is True, report
    assert report["merged"] == ["ch-001"], report
    assert (ws / "output" / "localized.md").is_file()
    states = {c["id"]: c["state"] for c in read_json(ws / "manifest.json")["chapters"]}
    assert states == {"ch-001": "delivered"}, states
