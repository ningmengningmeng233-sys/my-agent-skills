import os

import pytest

from _common import parse_frontmatter, read_text_auto, resolve_under


def test_parse_frontmatter_basic():
    text = "---\nkey: value\n---\nbody line"
    meta, body = parse_frontmatter(text)
    assert meta["key"] == "value"
    assert body == "body line"


def test_parse_frontmatter_with_list():
    text = (
        "---\n"
        "包含元素:\n"
        "  - 标签A\n"
        "  - 标签B\n"
        "---\n"
        "body"
    )
    meta, body = parse_frontmatter(text)
    assert meta["包含元素"] == ["标签A", "标签B"]


def test_parse_frontmatter_invalid_returns_raw():
    text = "---\nnot a valid frontmatter line\n---\nbody"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_read_text_auto_utf8(tmp_path):
    path = tmp_path / "utf8.txt"
    path.write_text("中文", encoding="utf-8")
    assert read_text_auto(path) == "中文"


def test_read_text_auto_gbk(tmp_path):
    path = tmp_path / "gbk.txt"
    path.write_text("中文", encoding="gbk")
    assert read_text_auto(path) == "中文"


def test_resolve_under_escapes_root():
    root = os.path.abspath("/tmp/lib")
    assert resolve_under(root, "../etc/passwd") is None


def test_resolve_under_returns_absolute():
    root = os.path.abspath("/tmp/lib")
    got = resolve_under(root, "小说原文/x.md")
    assert got == os.path.join(root, "小说原文", "x.md")
