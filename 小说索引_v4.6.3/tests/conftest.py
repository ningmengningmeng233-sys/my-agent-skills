import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


@pytest.fixture
def tmp_lib():
    """Yield a temporary index library directory."""
    path = tempfile.mkdtemp(prefix="novel-index-test-")
    for d in ("小说原文", "小说索引", "章节索引"):
        os.makedirs(os.path.join(path, d), exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def sample_segment(tmp_lib):
    """Create a minimal segment file and return its path."""
    novel_author = "test-author"
    novel_dir = os.path.join(tmp_lib, "小说原文", novel_author)
    os.makedirs(novel_dir, exist_ok=True)
    path = os.path.join(novel_dir, f"{novel_author}-第1章~第2章-001.md")
    content = (
        "---\n"
        "小说名称: 《test》\n"
        "作者: author\n"
        "章节范围: 第1章~第2章\n"
        f"对应索引: {novel_author}-第1章~第2章-001-idx.md\n"
        "分段方式: 按章节切分\n"
        "---\n"
        "## 第1章 开篇\n第一行\n第二行\n"
        "## 第2章 继续\n第三行\n第四行\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


@pytest.fixture
def sample_index(tmp_lib, sample_segment):
    """Create a minimal chapter index file and return its path."""
    novel_author = "test-author"
    idx_dir = os.path.join(tmp_lib, "章节索引")
    path = os.path.join(idx_dir, f"{novel_author}-第1章~第2章-001-idx.md")
    content = (
        "---\n"
        "小说名称: 《test》\n"
        "作者: author\n"
        "索引类型: 章节索引\n"
        "索引序号: '001'\n"
        "章节范围: 第1章~第2章\n"
        "总章节数: '2'\n"
        "是否完结: 连载中\n"
        "包含元素:\n  - 打脸\n"
        f"源文件: 小说原文/{novel_author}/{novel_author}-第1章~第2章-001.md\n"
        "创建日期: 2026-08-18\n"
        "最后更新: 2026-08-18\n"
        "状态: 完成\n"
        "---\n"
        "## 第1章 开篇（行号：1 ~ 2）\n"
        "### 剧情概要\n剧情\n"
        "### 金句\n- \"第一行\"（行1，开篇）\n"
        "## 第2章 继续（行号：3 ~ 4）\n"
        "### 剧情概要\n剧情\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path
