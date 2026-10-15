# -*- coding: utf-8 -*-
"""IDX-02 回归：重命名同步段文件与元数据，冲突预检 + 逆序回滚。

缺陷：rename_novel.py 只把段目录整体改名，段文件的文件名与 frontmatter
（小说名称/作者/对应索引）仍是旧书名；而章节索引的源文件回指与断点库路径
已更新为新书名，导致索引/断点库与物理文件悬空脱节。且改名前无冲突预检、
中途失败无回滚，会留下半改名坏死状态。

受控 RED：以下断言在修复前必须失败，修复后通过。全离线，不调模型。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)


def _run(*args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, os.path.join(SCRIPTS, "rename_novel.py"), *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)


@pytest.fixture
def rename_env(tmp_lib, sample_segment, sample_index):
    """一本最小可改名的书：段 + 索引 + 导航 + 断点记录。"""
    nav_path = os.path.join(tmp_lib, "小说索引", "test-author.md")
    with open(nav_path, "w", encoding="utf-8") as f:
        f.write("---\n小说名称: 《test》\n作者: author\n---\n# test nav\n")
    from index_db import IndexDB
    with IndexDB(tmp_lib) as db:
        db.mark_done(sample_segment, sample_index,
                     prompt_tokens=100, completion_tokens=200)
    return {"lib": tmp_lib, "seg": sample_segment, "idx": sample_index, "nav": nav_path}


def _db_paths(lib):
    from index_db import IndexDB
    with IndexDB(lib) as db:
        return db.conn.execute("SELECT segment_path, out_path FROM segment_tasks").fetchall()


def test_rename_syncs_segment_names_and_frontmatter(rename_env):
    """段文件名与 frontmatter 必须同步为新书名，索引回指/断点不得悬空。"""
    lib = rename_env["lib"]
    result = _run("--old-novel", "test", "--old-author", "author",
                  "--new-novel", "renamed", "--new-author", "writer",
                  "--lib", lib, "--yes")
    assert result.returncode == 0, result.stderr

    new_raw = os.path.join(lib, "小说原文", "renamed-writer")
    new_seg = os.path.join(new_raw, "renamed-writer-第1章~第2章-001.md")
    assert os.path.isfile(new_seg), f"段文件未同步重命名：{sorted(os.listdir(new_raw))}"
    assert not os.path.exists(os.path.join(lib, "小说原文", "test-author"))

    seg_text = open(new_seg, encoding="utf-8").read()
    assert "小说名称: 《renamed》" in seg_text, "段 frontmatter 书名未更新"
    assert "作者: writer" in seg_text, "段 frontmatter 作者未更新"
    assert "对应索引: renamed-writer-第1章~第2章-001-idx.md" in seg_text, "段 frontmatter 对应索引未更新"

    new_idx = os.path.join(lib, "章节索引", "renamed-writer-第1章~第2章-001-idx.md")
    idx_text = open(new_idx, encoding="utf-8").read()
    assert "小说原文/renamed-writer/renamed-writer-第1章~第2章-001.md" in idx_text.replace("\\", "/")
    # 索引回指的物理文件必须真实存在（不得悬空）
    assert os.path.isfile(os.path.join(lib, "小说原文", "renamed-writer",
                                       "renamed-writer-第1章~第2章-001.md"))

    rows = _db_paths(lib)
    assert len(rows) == 1
    assert os.path.isfile(rows[0][0]), f"断点 segment_path 悬空：{rows[0][0]}"
    assert os.path.isfile(rows[0][1]), f"断点 out_path 悬空：{rows[0][1]}"


def test_rename_conflict_precheck_leaves_tree_untouched(rename_env):
    """目标冲突必须在零改动前拦截：非零退出 + 原树字节级 intact + 不吞冲突件。"""
    lib = rename_env["lib"]
    before = {k: Path(rename_env[k]).read_bytes() for k in ("seg", "idx", "nav")}
    db_before = _db_paths(lib)

    victim = Path(lib) / "章节索引" / "renamed-writer-第1章~第2章-001-idx.md"
    victim.write_text("SENTINEL-DO-NOT-CLOBBER\n", encoding="utf-8")

    result = _run("--old-novel", "test", "--old-author", "author",
                  "--new-novel", "renamed", "--new-author", "writer",
                  "--lib", lib, "--yes")
    assert result.returncode != 0, "目标路径冲突必须预检失败并拒绝改名"
    assert victim.read_text(encoding="utf-8") == "SENTINEL-DO-NOT-CLOBBER\n", "冲突件被覆盖"
    for k, data in before.items():
        assert Path(rename_env[k]).read_bytes() == data, f"原文件 {k} 被改动"
    assert not os.path.exists(os.path.join(lib, "小说原文", "renamed-writer")), "残留半改名目录"
    assert not os.path.exists(os.path.join(lib, "小说索引", "renamed-writer.md")), "残留半改名导航"
    assert _db_paths(lib) == db_before, "断点库被改动"


def test_rename_mid_failure_rolls_back_in_reverse(rename_env, monkeypatch):
    """中途失败必须按已完成操作逆序回滚，不留悬空坏死状态。"""
    import rename_novel

    lib = rename_env["lib"]
    before = {k: Path(rename_env[k]).read_bytes() for k in ("seg", "idx", "nav")}
    db_before = _db_paths(lib)

    real_write_text = Path.write_text
    calls = {"n": 0}

    def flaky_write_text(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:  # 第一个文件成功落盘后失败 → 回滚必须撤销它
            raise OSError("injected mid-rename failure")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky_write_text)
    monkeypatch.setattr(sys, "argv", ["rename_novel.py",
                                      "--old-novel", "test", "--old-author", "author",
                                      "--new-novel", "renamed", "--new-author", "writer",
                                      "--lib", lib, "--yes"])
    assert calls["n"] == 0
    rc = rename_novel.main()
    assert rc == 1, "中途失败必须返回非零"
    assert calls["n"] >= 2, "注入未命中：需要至少一次成功写入后再失败"

    for k, data in before.items():
        assert Path(rename_env[k]).read_bytes() == data, f"原文件 {k} 未回滚"
    assert not os.path.exists(os.path.join(lib, "小说原文", "renamed-writer")), "残留半改名目录"
    assert not os.path.exists(
        os.path.join(lib, "章节索引", "renamed-writer-第1章~第2章-001-idx.md")), "残留半改名索引"
    assert not os.path.exists(os.path.join(lib, "小说索引", "renamed-writer.md")), "残留半改名导航"
    assert _db_paths(lib) == db_before, "断点库未回滚"
