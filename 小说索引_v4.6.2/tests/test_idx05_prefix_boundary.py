# -*- coding: utf-8 -*-
"""IDX-05 回归：按书删除/改名必须做结构化边界匹配，杜绝连字符前缀碰撞误伤。

缺陷：_common._is_book_key_segment 用简陋的 startswith(key + "-") 做字面量
匹配。当书名/作者含连字符时（如 book-a 与 book-a-part2），删除/重命名
book-a 会把 book-a-part2 的段文件、索引、断点记录一起删掉/改走。

受控 RED：以下断言在修复前必须失败（part2 被误伤），修复后通过。
全离线，不调模型。
"""
from __future__ import annotations

import sys
from pathlib import Path

import delete_novel
import rename_novel
from _common import path_has_book_key, path_replace_book_key, prefixed_files
from index_db import IndexDB

# book-a：novel="book" author="a"；book-a-part2：novel="book-a" author="part2"
BOOK_A = ("book", "a", "book-a")
BOOK_B = ("book-a", "part2", "book-a-part2")


def _write_seg(lib: Path, key: str, rng: str, seq: str) -> Path:
    novel_dir = lib / "小说原文" / key
    novel_dir.mkdir(parents=True, exist_ok=True)
    seg = novel_dir / f"{key}-{rng}-{seq}.md"
    seg.write_text(
        "---\n小说名称: 《X》\n作者: Y\n章节范围: %s\n对应索引: %s-%s-%s-idx.md\n"
        "分段方式: 按章节切分\n---\n第1章 正文\n" % (rng, key, rng, seq),
        encoding="utf-8")
    return seg


def _write_idx(lib: Path, key: str, rng: str, seq: str) -> Path:
    idx = lib / "章节索引" / f"{key}-{rng}-{seq}-idx.md"
    idx.write_text(
        "---\n小说名称: 《X》\n作者: Y\n---\n源文件: 小说原文/%s/%s-%s-%s.md\n正文\n"
        % (key, key, rng, seq), encoding="utf-8")
    return idx


def _build_book(lib: Path, novel: str, author: str, key: str):
    seg = _write_seg(lib, key, "第1章", "001")
    idx = _write_idx(lib, key, "第1章", "001")
    nav = lib / "小说索引" / f"{key}.md"
    nav.write_text("---\n小说名称: 《X》\n---\n导航\n", encoding="utf-8")
    with IndexDB(str(lib)) as db:
        db.mark_done(str(seg), str(idx))
    return seg, idx, nav


def _snapshot(paths):
    return {p: Path(p).read_bytes() for p in paths}


def test_unit_boundary_rejects_prefix_collision():
    """单元级：book-a-part2 的段/索引/目录一律不归属 book-a。"""
    assert not path_has_book_key("小说原文/book-a-part2/book-a-part2-第1章-001.md", "book-a")
    assert not path_has_book_key("book-a-part2-第1章-001-idx.md", "book-a")
    assert not path_has_book_key("小说原文/book-a-part2", "book-a")
    assert path_replace_book_key("book-a-part2-第1章-001.md", "book-a", "book-x") is None
    # 本书自身仍命中：精确目录 + 结构化段/索引后缀
    assert path_has_book_key("小说原文/book-a/book-a-第1章-001.md", "book-a")
    assert path_has_book_key("book-a-第1章-001-idx.md", "book-a")
    assert path_replace_book_key("book-a-第1章-001.md", "book-a", "book-x") == "book-x-第1章-001.md"


def test_prefixed_files_excludes_collision(tmp_lib):
    lib = Path(tmp_lib)
    _build_book(lib, *BOOK_A)
    _build_book(lib, *BOOK_B)
    got = [p.name for p in prefixed_files(lib / "章节索引", "book-a", "-idx.md")]
    assert got == ["book-a-第1章-001-idx.md"], f"误伤了他书索引：{got}"


def test_delete_does_not_touch_prefix_collision_book(tmp_lib, monkeypatch):
    lib = Path(tmp_lib)
    _build_book(lib, *BOOK_A)
    seg_b, idx_b, nav_b = _build_book(lib, *BOOK_B)
    before = _snapshot([seg_b, idx_b, nav_b])

    monkeypatch.setattr(sys, "argv", ["delete_novel.py", "--novel", "book", "--author", "a",
                                      "--lib", str(lib), "--yes"])
    assert delete_novel.main() == 0

    for p, data in before.items():
        assert Path(p).is_file(), f"他书文件被误删：{p}"
        assert Path(p).read_bytes() == data, f"他书文件被改动：{p}"
    with IndexDB(str(lib)) as db:
        rows = db.conn.execute("SELECT segment_path FROM segment_tasks").fetchall()
    assert [r[0] for r in rows] == [str(seg_b)], f"他书断点被误删：{rows}"
    # 本书确实被删干净
    assert not (lib / "小说原文" / "book-a").exists()
    assert list((lib / "章节索引").glob("book-a-第1章-001-idx.md")) == []
    assert not (lib / "小说索引" / "book-a.md").exists()


def test_rename_does_not_touch_prefix_collision_book(tmp_lib, monkeypatch):
    lib = Path(tmp_lib)
    _build_book(lib, *BOOK_A)
    seg_b, idx_b, nav_b = _build_book(lib, *BOOK_B)
    before = _snapshot([seg_b, idx_b, nav_b])

    monkeypatch.setattr(sys, "argv", ["rename_novel.py", "--old-novel", "book", "--old-author", "a",
                                      "--new-novel", "book", "--new-author", "b",
                                      "--lib", str(lib), "--yes"])
    assert rename_novel.main() == 0

    for p, data in before.items():
        assert Path(p).is_file(), f"他书文件被改走：{p}"
        assert Path(p).read_bytes() == data, f"他书文件被改动：{p}"
    with IndexDB(str(lib)) as db:
        rows = db.conn.execute(
            "SELECT segment_path, out_path FROM segment_tasks ORDER BY segment_path").fetchall()
    by_seg = {r[0]: r[1] for r in rows}
    assert str(seg_b) in by_seg and by_seg[str(seg_b)] == str(idx_b), f"他书断点被改走：{rows}"
    # 本书确实改名到位
    assert (lib / "小说原文" / "book-b" / "book-b-第1章-001.md").is_file()
    assert (lib / "章节索引" / "book-b-第1章-001-idx.md").is_file()
    assert (lib / "小说索引" / "book-b.md").is_file()
