import os
import shutil
import tempfile
import pytest

from dramacraft.ingestor import load_novel, Chapter


def test_load_single_file_with_chapters():
    text = """第1章 龙王归来
江南的冬夜，寒风刺骨。林晨跪在大理石地面上。

第2章 顾家的羞辱
顾雨薇踩着高跟鞋走近，从手包中抽出一叠打印纸，重重摔在林晨脸上。
"""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(text)
        temp_path = f.name

    try:
        chapters = load_novel(temp_path)
        assert len(chapters) == 2
        assert chapters[0].index == 1
        assert "第1章" in chapters[0].title
        assert "林晨跪在大理石地面上" in chapters[0].content
        assert chapters[1].index == 2
        assert "第2章" in chapters[1].title
        assert "顾家的羞辱" in chapters[1].title
        assert "重重摔在林晨脸上" in chapters[1].content
    finally:
        os.remove(temp_path)


def test_load_directory_of_chapters():
    temp_dir = tempfile.mkdtemp()
    try:
        f1 = os.path.join(temp_dir, "02_第二章.txt")
        f2 = os.path.join(temp_dir, "01_第一章.txt")
        with open(f1, "w", encoding="utf-8") as fp:
            fp.write("这是第二章的正文内容。")
        with open(f2, "w", encoding="utf-8") as fp:
            fp.write("这是第一章的正文内容。")

        chapters = load_novel(temp_dir)
        assert len(chapters) == 2
        # 自然排序：01 应该在 02 前面
        assert chapters[0].index == 1
        assert "第一章" in chapters[0].title
        assert "这是第一章的正文内容" in chapters[0].content
        assert chapters[1].index == 2
        assert "第二章" in chapters[1].title
        assert "这是第二章的正文内容" in chapters[1].content
    finally:
        shutil.rmtree(temp_dir)


def test_load_plain_text_sliding_window():
    # 纯无标题长文本
    raw_text = "这是一段没有章节标记的连续长篇叙事。" * 200  # 约 4000 字
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(raw_text)
        temp_path = f.name

    try:
        chapters = load_novel(temp_path, chunk_size=1500)
        assert len(chapters) >= 2
        assert chapters[0].index == 1
        assert chapters[0].title.startswith("分段 001")
        assert len(chapters[0].content) <= 2000
    finally:
        os.remove(temp_path)
