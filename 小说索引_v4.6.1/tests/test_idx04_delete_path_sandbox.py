# -*- coding: utf-8 -*-
"""IDX-04: 删除/改名路径沙箱约束与参数规范化。

受控 RED→GREEN：书名/作者名含 `../` 越界时必须抛 ValueError / 返回非零退出码，
且绝不搜集、删除、改名索引库之外的任何路径；合法书名行为不变。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import delete_novel
import rename_novel


def _outside_markers(tmp_lib: str) -> tuple[Path, Path]:
    """在索引库之外布置“诱饵”：越界搜集一旦发生就会命中它们。"""
    parent = Path(tmp_lib).parent
    outside_dir = parent / "evil-x"
    outside_dir.mkdir(exist_ok=True)
    dir_marker = outside_dir / "evil-x-001.md"
    dir_marker.write_text("---\n小说名称: evil\n---\n正文\n", encoding="utf-8")
    nav_marker = parent / "evil-x.md"
    nav_marker.write_text("---\n小说名称: evil\n---\n导航\n", encoding="utf-8")
    return dir_marker, nav_marker


def test_traversal_novel_raises_and_touches_nothing(tmp_lib):
    dir_marker, nav_marker = _outside_markers(tmp_lib)
    with pytest.raises(ValueError):
        delete_novel._collect_paths(Path(tmp_lib), "../../evil", "x")
    assert dir_marker.exists() and nav_marker.exists()


def test_traversal_author_rejected(tmp_lib):
    with pytest.raises(ValueError):
        delete_novel._collect_paths(Path(tmp_lib), "好书", "../x")
    with pytest.raises(ValueError):
        delete_novel._collect_paths(Path(tmp_lib), "好\\书", "作者")


def test_delete_cli_refuses_traversal(tmp_lib, monkeypatch):
    dir_marker, nav_marker = _outside_markers(tmp_lib)
    monkeypatch.setattr(sys, "argv", ["delete_novel.py", "--novel", "../../evil",
                                      "--author", "x", "--lib", tmp_lib, "--yes"])
    assert delete_novel.main() == 1
    assert dir_marker.exists() and nav_marker.exists(), "越界诱饵必须完好无损"


def test_rename_cli_refuses_traversal(tmp_lib, monkeypatch):
    dir_marker, _ = _outside_markers(tmp_lib)
    before = dir_marker.read_bytes()
    monkeypatch.setattr(sys, "argv", ["rename_novel.py", "--old-novel", "../../evil",
                                      "--old-author", "x", "--new-novel", "新书",
                                      "--new-author", "作者", "--lib", tmp_lib, "--yes"])
    assert rename_novel.main() == 1
    assert dir_marker.read_bytes() == before, "库外文件一个字节都不许动"


def test_legit_novel_collects_inside_lib(tmp_lib):
    lib = Path(tmp_lib)
    raw = lib / "小说原文" / "好书-作者"
    raw.mkdir(parents=True)
    (raw / "好书-作者-第1章-001.md").write_text("正文", encoding="utf-8")
    (lib / "章节索引" / "好书-作者-第1章-001-idx.md").write_text("索引", encoding="utf-8")
    (lib / "小说索引" / "好书-作者.md").write_text("导航", encoding="utf-8")
    paths = delete_novel._collect_paths(lib, "好书", "作者")
    assert len(paths) == 3
    root = lib.resolve()
    assert all(p.resolve().is_relative_to(root) for p in paths)


def test_special_char_name_still_supported(tmp_lib):
    """书名含 `[`、`?` 等字面量字符仍是合法输入（只做字面量匹配，不拒绝）。"""
    lib = Path(tmp_lib)
    (lib / "小说索引" / "书[名]-作者.md").write_text("导航", encoding="utf-8")
    paths = delete_novel._collect_paths(lib, "书[名]", "作者")
    assert [p.name for p in paths] == ["书[名]-作者.md"]
