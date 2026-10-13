import os
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(ROOT, "scripts")


def _run(*args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, os.path.join(SCRIPTS, "build_novel_index.py"), *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)


def _make_index(lib: str, novel_author: str, rng: str, seg_no: int, tags: list[str]):
    idx_dir = os.path.join(lib, "章节索引")
    path = os.path.join(idx_dir, f"{novel_author}-{rng}-{seg_no:03d}-idx.md")
    tag_lines = "\n".join(f"  - {t}" for t in tags)
    content = (
        "---\n"
        f"小说名称: 《test》\n"
        f"作者: author\n"
        "索引类型: 章节索引\n"
        f"索引序号: '{seg_no:03d}'\n"
        f"章节范围: {rng}\n"
        f"总章节数: '2'\n"
        "是否完结: 连载中\n"
        f"包含元素:\n{tag_lines}\n"
        f"源文件: 小说原文/{novel_author}/{novel_author}-{rng}-{seg_no:03d}.md\n"
        "创建日期: 2026-08-18\n"
        "最后更新: 2026-08-18\n"
        "状态: 完成\n"
        "---\n"
        f"## {rng}\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_build_novel_index_creates_navigation(tmp_lib):
    _make_index(tmp_lib, "test-author", "第1章~第2章", 1, ["打脸"])
    _make_index(tmp_lib, "test-author", "第3章~第4章", 2, ["打脸", "升级"])

    result = _run("--novel", "test-author", "--lib", tmp_lib, "--force")
    assert result.returncode == 0, result.stderr

    nav = os.path.join(tmp_lib, "小说索引", "test-author.md")
    assert os.path.exists(nav)
    text = open(nav, encoding="utf-8").read()
    assert "《test》" in text
    assert "test-author-第1章~第2章-001-idx.md" in text
    assert "test-author-第3章~第4章-002-idx.md" in text
    assert "打脸" in text
    assert "升级" in text
    assert "最后索引章节: 第4章" in text


def test_build_novel_index_all_scans_library(tmp_lib):
    _make_index(tmp_lib, "test-author", "第1章~第2章", 1, ["打脸"])

    result = _run("--all", "--lib", tmp_lib, "--force")
    assert result.returncode == 0, result.stderr
    assert os.path.exists(os.path.join(tmp_lib, "小说索引", "test-author.md"))


def test_build_novel_index_missing_index_returns_error(tmp_lib):
    result = _run("--novel", "missing-none", "--lib", tmp_lib)
    assert result.returncode == 1
