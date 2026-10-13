r"""上游等价机制回归用例（R05-D02 / R05-D03）。

来源=本地同族「成人文学索引」v4.6.5 / v4.6.3 的等价机制，非 54Lynnn 作者代码。
覆盖本产品此前零保护的两处行为缺口：
- R05-D02：路径分隔符 `\` 与 `/` 混写不得产生重复断点行；旧库已有重复行在
  IndexDB 打开时自动归并（done>failed>pending、retries 取大、token 相加、
  保留行路径改写为归一化字面量），且归并幂等。
- R05-D03：书名含 glob / SQL LIKE 元字符（`[`、`%`、`_`）时，删除/重命名只能
  命中该书自身产物与断点行。

本文件只导入本产品的 scripts/，禁止跨品导入（会得到假绿）。
"""
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from index_db import DB_NAME, IndexDB, file_hash  # noqa: E402

_ALT_SEP = "/" if os.sep == "\\" else "\\"
_TABLE_COLS = "segment_path, status, retries, prompt_tokens, completion_tokens"


# ---------------------------------------------------------------- 合成库工具


def _mixed_separator_writing(text):
    """把偶数个路径分隔符换成另一写法，得到「混合写法」。"""
    chars = list(text)
    seen = 0
    for i, ch in enumerate(chars):
        if ch == os.sep:
            seen += 1
            if seen % 2 == 0:
                chars[i] = _ALT_SEP
    return "".join(chars)


def _writings(path):
    """同一路径的 native / 全换写法 / 混合写法；平台不区分分隔符时整文件跳过。"""
    native = os.path.normpath(str(path))
    variants = list(dict.fromkeys(
        [native, native.replace(os.sep, _ALT_SEP), _mixed_separator_writing(native)]
    ))
    if any(os.path.normpath(v) != native for v in variants):
        pytest.skip("本平台路径分隔符写法不影响判重（仅 Windows 有意义）")
    return variants


def _book(lib, novel, author):
    """在合成索引库里造一本书，返回 (书名键, 段文件, 章节索引, 小说导航)。"""
    key = f"{novel}-{author}"
    seg = lib / "小说原文" / key / f"{key}-第1章~第2章-001.md"
    idx = lib / "章节索引" / f"{key}-第1章~第2章-001-idx.md"
    nav = lib / "小说索引" / f"{key}.md"
    seg.parent.mkdir(parents=True, exist_ok=True)
    idx.parent.mkdir(parents=True, exist_ok=True)
    nav.parent.mkdir(parents=True, exist_ok=True)
    seg.write_text("## 第1章 开篇\n第一行\n第二行\n", encoding="utf-8")
    idx.write_text(
        "---\n"
        f"小说名称: 《{novel}》\n"
        f"作者: {author}\n"
        "索引类型: 章节索引\n"
        f"源文件: 小说原文/{key}/{seg.name}\n"
        "---\n## 第1章 开篇（行号：1 ~ 2）\n剧情\n",
        encoding="utf-8",
    )
    nav.write_text(f"# {key}\n", encoding="utf-8")
    return key, seg, idx, nav


def _raw_rows(lib):
    """绕过 IndexDB 直接读断点表，看到的才是磁盘真实状态。"""
    conn = sqlite3.connect(str(Path(lib) / DB_NAME))
    try:
        return conn.execute(f"SELECT {_TABLE_COLS} FROM segment_tasks ORDER BY rowid").fetchall()
    finally:
        conn.close()


def _run(script, *args):
    cmd = [sys.executable, str(SCRIPTS / script), *[str(a) for a in args]]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)


# ------------------------------------------------- R05-D02：分隔符归一化与归并


def test_mixed_separator_writes_do_not_duplicate_done_row(tmp_path):
    """同一断用 `\\` 与 `/` 两种写法各 mark_done 一次，只能有一行。"""
    _, seg, idx, _ = _book(tmp_path, "甲书", "某作者")
    native, alt, _ = _writings(seg)
    seg_hash = file_hash(native)

    with IndexDB(str(tmp_path)) as db:
        db.mark_done(native, str(idx), prompt_tokens=10, completion_tokens=20, seg_hash=seg_hash)
        db.mark_done(alt, str(idx), prompt_tokens=10, completion_tokens=20, seg_hash=seg_hash)

        statuses, (p, c) = db.stats()
        assert statuses.get("done", 0) == 1, f"混写分隔符产生了 {statuses} 行，应只有 1 行 done"
        assert (p, c) == (10, 20), "UPSERT 覆盖语义下同一行不得重复计 token"
        assert db.get(alt) is not None, "另一写法必须能查到同一行"
        assert db.get(alt)[0] == native, "保留行路径必须是归一化后的字面量"


def test_every_read_write_entry_shares_one_normalized_row(tmp_path):
    """mark_pending / mark_done / mark_failed / get 四个入口都要走同一归一化键。"""
    _, seg, idx, _ = _book(tmp_path, "乙书", "某作者")
    native, alt, mixed = _writings(seg)

    with IndexDB(str(tmp_path)) as db:
        db.mark_pending(alt, str(idx))
        db.mark_done(native, str(idx), prompt_tokens=3, completion_tokens=4)
        assert len(_raw_rows(tmp_path)) == 1, "pending/done 混写产生了重复行"
        assert db.get(mixed)[2] == "done"

        db.mark_failed(alt, "boom")
        row = db.get(native)
        assert len(_raw_rows(tmp_path)) == 1, "mark_failed 混写产生了重复行"
        assert row[2] == "failed" and row[3] == 1


def test_legacy_duplicate_rows_merge_on_open(tmp_path):
    """旧库已有的混写重复行：打开连接即归并，状态/retries/token/路径按规则保留。"""
    _, seg, idx, _ = _book(tmp_path, "丙书", "某作者")
    native, alt, mixed = _writings(seg)
    seg_hash = file_hash(native)

    with IndexDB(str(tmp_path)):
        pass  # 只建表

    # 裸 SQL 造 3 条混写行，故意让「最优状态」不在第一位（验证优先级而非插入顺序）
    with sqlite3.connect(str(Path(tmp_path) / DB_NAME)) as conn:
        for path, status, retries, err, pt, ct in (
            (native, "pending", 0, None, 3, 1),
            (alt, "failed", 3, "旧错误", 5, 7),
            (mixed, "done", 1, None, 10, 20),
        ):
            conn.execute(
                "INSERT INTO segment_tasks (segment_path, out_path, status, retries, error,"
                " seg_hash, prompt_tokens, completion_tokens) VALUES (?,?,?,?,?,?,?,?)",
                (path, str(idx), status, retries, err, seg_hash, pt, ct),
            )

    with IndexDB(str(tmp_path)) as db:
        statuses, (p, c) = db.stats()
        assert statuses == {"done": 1}, f"归并后应只剩 1 行 done，实际 {statuses}"
        assert (p, c) == (18, 28), "token 必须相加保留（两次执行都真实消耗了 API）"

        row = db.get(alt)
        assert row is not None
        assert row[0] == native, "保留行路径必须改写为归一化后的字面量"
        assert row[2] == "done", "状态优先级 done > failed > pending"
        assert row[3] == 3, "retries 取各行最大值"

    disk = _raw_rows(tmp_path)
    assert len(disk) == 1
    assert disk[0][0] == native and disk[0][3] == 18 and disk[0][4] == 28


def test_merge_is_idempotent_across_reopen(tmp_path):
    """归并必须幂等：重开不能把已归并的行再归并一次（token 翻倍）。"""
    _, seg, idx, _ = _book(tmp_path, "丁书", "某作者")
    writings = _writings(seg)
    seg_hash = file_hash(seg)

    with IndexDB(str(tmp_path)):
        pass
    with sqlite3.connect(str(Path(tmp_path) / DB_NAME)) as conn:
        for path in writings:
            conn.execute(
                "INSERT INTO segment_tasks (segment_path, out_path, status, seg_hash,"
                " prompt_tokens, completion_tokens) VALUES (?, ?, 'done', ?, 10, 20)",
                (path, str(idx), seg_hash),
            )

    expected_prompt, expected_completion = 10 * len(writings), 20 * len(writings)
    with IndexDB(str(tmp_path)):
        pass
    first = _raw_rows(tmp_path)
    for _ in range(2):
        with IndexDB(str(tmp_path)):
            pass
        assert _raw_rows(tmp_path) == first, "重开后磁盘状态发生变化（归并不幂等）"

    assert len(first) == 1
    assert first[0][0] == os.path.normpath(str(seg))
    assert (first[0][3], first[0][4]) == (expected_prompt, expected_completion)


# --------------------------------------------- R05-D03：删除/重命名的元字符安全


def test_delete_novel_bracket_name_only_touches_that_book(tmp_path):
    """书名含 `[` 时 glob 会把它当字符类：只能删《a[1]》，不得误删《a1》。"""
    _, t_seg, t_idx, t_nav = _book(tmp_path, "a[1]", "x")
    _, v_seg, v_idx, v_nav = _book(tmp_path, "a1", "x")

    result = _run("delete_novel.py", "--novel", "a[1]", "--author", "x",
                  "--lib", tmp_path, "--yes")
    assert result.returncode == 0, result.stderr

    assert v_idx.exists() and v_nav.exists() and v_seg.parent.exists(), "误删了他书产物"
    assert not t_idx.exists() and not t_nav.exists() and not t_seg.parent.exists(), "目标书没删干净"


@pytest.mark.parametrize("novel,victim_novel", [("a%b", "ab"), ("a_b", "axb")])
def test_delete_novel_like_metachar_keeps_other_books_breakpoint_rows(tmp_path, novel, victim_novel):
    """书名含 `%` / `_` 时 SQL LIKE 会当通配：只删该书断点行，报告计数与实际命中一致。"""
    _, t_seg, t_idx, _ = _book(tmp_path, novel, "x")
    _, v_seg, _, _ = _book(tmp_path, victim_novel, "x")

    with IndexDB(str(tmp_path)) as db:
        db.mark_done(str(t_seg), str(t_idx), prompt_tokens=10, completion_tokens=20)
        db.mark_done(str(v_seg), str(tmp_path / "章节索引" / f"{victim_novel}-x-第1章~第2章-001-idx.md"),
                     prompt_tokens=1, completion_tokens=2)
    before = _raw_rows(tmp_path)
    assert len(before) == 2

    result = _run("delete_novel.py", "--novel", novel, "--author", "x",
                  "--lib", tmp_path, "--yes")
    assert result.returncode == 0, result.stderr

    after = _raw_rows(tmp_path)
    assert [r[0] for r in after] == [os.path.normpath(str(v_seg))], (
        f"误删/误改了他书断点行：{before} -> {after}"
    )
    reported = re.findall(r"已清理 (\d+) 条断点记录", result.stdout)
    assert reported == [str(len(before) - len(after))], (
        f"报告计数 {reported} 与实际命中 {len(before) - len(after)} 不一致"
    )


def test_rename_novel_bracket_name_only_touches_that_book(tmp_path):
    """重命名同理：glob 元字符不得连带改走、改坏他书产物与断点行。"""
    _, t_seg, t_idx, t_nav = _book(tmp_path, "a[1]", "x")
    _, v_seg, v_idx, v_nav = _book(tmp_path, "a1", "x")
    v_idx_before = v_idx.read_text(encoding="utf-8")

    with IndexDB(str(tmp_path)) as db:
        db.mark_done(str(t_seg), str(t_idx), prompt_tokens=10, completion_tokens=20)
        db.mark_done(str(v_seg), str(v_idx), prompt_tokens=1, completion_tokens=2)

    result = _run("rename_novel.py", "--old-novel", "a[1]", "--old-author", "x",
                  "--new-novel", "新名", "--new-author", "y", "--lib", tmp_path, "--yes")
    assert result.returncode == 0, result.stderr

    # 他书：文件、正文、断点行全部原样
    assert v_idx.exists() and v_nav.exists() and v_seg.parent.exists(), "他书产物被误改走"
    assert v_idx.read_text(encoding="utf-8") == v_idx_before, "他书章节索引内容被改写"
    rows = {r[0] for r in _raw_rows(tmp_path)}
    assert os.path.normpath(str(v_seg)) in rows, "他书断点行路径被误改"

    # 目标书：确实改名为 新名-y
    new_key = "新名-y"
    new_seg = tmp_path / "小说原文" / new_key / f"{new_key}-第1章~第2章-001.md"
    new_idx = tmp_path / "章节索引" / f"{new_key}-第1章~第2章-001-idx.md"
    assert new_seg.parent.is_dir() and new_idx.exists() and (tmp_path / "小说索引" / f"{new_key}.md").exists()
    assert not t_idx.exists() and not t_nav.exists() and not t_seg.parent.exists()
    assert f"源文件: 小说原文{os.sep}{new_key}" in new_idx.read_text(encoding="utf-8").replace("/", os.sep)
    reported = re.findall(r"已更新 (\d+) 条断点记录路径", result.stdout)
    assert reported == ["1"], f"报告计数 {reported} 与实际命中 1 不一致"
