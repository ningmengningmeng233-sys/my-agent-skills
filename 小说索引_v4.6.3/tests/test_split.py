import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(ROOT, "scripts")


def _run(*args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, os.path.join(SCRIPTS, "split_segments.py"), *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)


def test_split_by_chapters_creates_segments(tmp_path):
    lib = tmp_path / "lib"
    source = tmp_path / "novel.txt"
    lines = []
    for i in range(1, 8):
        lines.append(f"第{i}章 标题{i}")
        lines.append(f"这是第{i}章的内容。")
        lines.append("")
    source.write_text("\n".join(lines), encoding="utf-8")

    result = _run(
        str(source),
        "--novel", "test",
        "--author", "author",
        "--lib", str(lib),
        "--chapters-per-seg", "5",
    )
    assert result.returncode == 0, result.stderr

    seg_dir = lib / "小说原文" / "test-author"
    files = sorted(seg_dir.glob("*.md"))
    assert len(files) == 2
    assert files[0].name == "test-author-第1章~第5章-001.md"
    assert files[1].name == "test-author-第6章~第7章-002.md"

    text0 = files[0].read_text(encoding="utf-8")
    assert "第1章 标题1" in text0
    assert "第5章 标题5" in text0
    assert "对应索引: test-author-第1章~第5章-001-idx.md" in text0


def test_split_skips_when_unchanged(tmp_path):
    lib = tmp_path / "lib"
    source = tmp_path / "novel.txt"
    source.write_text("第1章 a\ncontent\n第2章 b\ncontent\n", encoding="utf-8")

    _run(str(source), "--novel", "test", "--author", "author", "--lib", str(lib))
    seg_dir = lib / "小说原文" / "test-author"
    assert len(list(seg_dir.glob("*.md"))) == 1

    result = _run(str(source), "--novel", "test", "--author", "author", "--lib", str(lib))
    assert result.returncode == 0, result.stderr
    assert "跳过" in result.stdout or result.stdout == ""


def test_split_by_chars_when_no_chapters(tmp_path):
    lib = tmp_path / "lib"
    source = tmp_path / "novel.txt"
    para = "a" * 3000
    source.write_text("\n\n".join([para] * 5), encoding="utf-8")

    result = _run(
        str(source),
        "--novel", "test",
        "--author", "author",
        "--lib", str(lib),
        "--chars-per-seg", "5000",
    )
    assert result.returncode == 0, result.stderr
    seg_dir = lib / "小说原文" / "test-author"
    files = sorted(seg_dir.glob("*.md"))
    assert len(files) >= 2
    assert all("按字数切分" in f.read_text(encoding="utf-8") for f in files)


def test_split_missing_source_returns_error(tmp_path):
    result = _run(
        str(tmp_path / "no_such.txt"),
        "--novel", "test",
        "--author", "author",
        "--lib", str(tmp_path / "lib"),
    )
    assert result.returncode == 1
