import os

import tags


def test_parse_builtin_vocab_extracts_groups():
    text = (
        "# 包含元素·标签词汇表\n\n"
        "## 爽点 · 装逼打脸系\n"
        "`装逼打脸 | 打脸 | 扮猪吃虎`\n\n"
        "## 剧情标签 · 修炼体系\n"
        "`修炼 | 功法`\n"
    )
    groups = tags.parse_builtin_vocab(text)
    assert "爽点 · 装逼打脸系" in groups
    assert groups["爽点 · 装逼打脸系"] == ["装逼打脸", "打脸", "扮猪吃虎"]
    assert groups["剧情标签 · 修炼体系"] == ["修炼", "功法"]


def test_load_custom_tags_list_format(tmp_path):
    path = tmp_path / "tags.custom.yaml"
    path.write_text(
        "- group: 爽点 · 装逼打脸系\n"
        "  tags: [装逼打脸, 打脸, 新标签]\n"
        "- group: 世界观元素 · 赛博朋克\n"
        "  tags: [赛博朋克, 义体]\n",
        encoding="utf-8",
    )
    groups = tags.load_custom_tags(path)
    assert groups["爽点 · 装逼打脸系"] == ["装逼打脸", "打脸", "新标签"]
    assert groups["世界观元素 · 赛博朋克"] == ["赛博朋克", "义体"]


def test_load_custom_tags_missing_returns_empty(tmp_path):
    groups = tags.load_custom_tags(tmp_path / "nope.yaml")
    assert groups == {}


def test_merge_vocab_custom_overrides_builtin():
    builtin = {"A": ["a", "b"]}
    custom = {"A": ["x"], "B": ["y"]}
    merged = tags.merge_vocab(builtin, custom)
    assert merged["A"] == ["x"]
    assert merged["B"] == ["y"]


def test_load_tag_vocabulary_merges(tmp_lib):
    custom = os.path.join(tmp_lib, "tags.custom.yaml")
    with open(custom, "w", encoding="utf-8") as f:
        f.write("- group: 爽点 · 装逼打脸系\n  tags: [装逼打脸, 打脸, 新标签]\n")

    text = tags.load_tag_vocabulary(tmp_lib)
    assert "新标签" in text
    assert "装逼打脸" in text
