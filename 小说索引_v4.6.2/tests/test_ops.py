import os
import subprocess
import sys

import pytest


@pytest.fixture
def ops_env(tmp_lib, sample_segment, sample_index):
    """Populate a tiny novel with segment, index, nav, and breakpoint records."""
    nav_dir = os.path.join(tmp_lib, "小说索引")
    nav_path = os.path.join(nav_dir, "test-novel-author.md")
    with open(nav_path, "w", encoding="utf-8") as f:
        f.write("# test nav\n")

    # breakpoint record for the segment
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    from index_db import IndexDB

    with IndexDB(tmp_lib) as db:
        db.mark_done(sample_segment, sample_index, prompt_tokens=100, completion_tokens=200)

    return {"novel": "test", "author": "author", "lib": tmp_lib}


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(ROOT, "scripts")


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, os.path.join(SCRIPTS, script), *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)


def test_delete_novel_removes_files_and_db(ops_env):
    lib = ops_env["lib"]
    novel, author = ops_env["novel"], ops_env["author"]

    result = _run("delete_novel.py", "--novel", novel, "--author", author, "--lib", lib, "--yes")
    assert result.returncode == 0, result.stderr

    raw_dir = os.path.join(lib, "小说原文", f"{novel}-{author}")
    nav_file = os.path.join(lib, "小说索引", f"{novel}-{author}.md")
    assert not os.path.exists(raw_dir)
    assert not os.path.exists(nav_file)

    sys.path.insert(0, SCRIPTS)
    from index_db import IndexDB

    with IndexDB(lib) as db:
        rows = db.conn.execute("SELECT COUNT(*) FROM segment_tasks").fetchone()[0]
        assert rows == 0


def test_delete_novel_missing_is_noop(tmp_lib):
    result = _run("delete_novel.py", "--novel", "missing", "--author", "none", "--lib", tmp_lib, "--yes")
    assert result.returncode == 0


def test_rename_novel_updates_paths_and_frontmatter(ops_env):
    lib = ops_env["lib"]

    result = _run(
        "rename_novel.py",
        "--old-novel", "test",
        "--old-author", "author",
        "--new-novel", "renamed",
        "--new-author", "writer",
        "--lib", lib,
        "--yes",
    )
    assert result.returncode == 0, result.stderr

    old_raw = os.path.join(lib, "小说原文", "test-author")
    new_raw = os.path.join(lib, "小说原文", "renamed-writer")
    assert not os.path.exists(old_raw)
    assert os.path.isdir(new_raw)

    idx_dir = os.path.join(lib, "章节索引")
    old_idx = os.path.join(idx_dir, "test-author-第1章~第2章-001-idx.md")
    new_idx = os.path.join(idx_dir, "renamed-writer-第1章~第2章-001-idx.md")
    assert not os.path.exists(old_idx)
    assert os.path.exists(new_idx)

    text = open(new_idx, encoding="utf-8").read()
    assert "小说名称: 《renamed》" in text
    assert "作者: writer" in text
    assert "renamed-writer" in text

    new_seg = os.path.join(new_raw, "renamed-writer-第1章~第2章-001.md")
    sys.path.insert(0, SCRIPTS)
    from index_db import IndexDB

    with IndexDB(lib) as db:
        assert db.get(new_seg) is not None


def test_migrate_library_copies_and_updates_paths(tmp_path):
    from_dir = tmp_path / "from"
    to_dir = tmp_path / "to"
    from_dir.mkdir()
    (from_dir / ".index_state.db").write_text("", encoding="utf-8")
    seg = from_dir / "小说原文" / "x-y"
    seg.mkdir(parents=True)

    sys.path.insert(0, SCRIPTS)
    from index_db import IndexDB

    with IndexDB(str(from_dir)) as db:
        db.mark_done(str(seg / "x-y-第1章~第2章-001.md"), str(seg / "x-y-第1章~第2章-001.md"))

    result = _run("migrate_library.py", "--from", str(from_dir), "--to", str(to_dir), "--copy", "--yes")
    assert result.returncode == 0, result.stderr
    assert (to_dir / "小说原文" / "x-y").exists()

    with IndexDB(str(to_dir)) as db:
        rows = db.conn.execute("SELECT segment_path FROM segment_tasks").fetchall()
        assert all(str(to_dir) in r[0] for r in rows)
