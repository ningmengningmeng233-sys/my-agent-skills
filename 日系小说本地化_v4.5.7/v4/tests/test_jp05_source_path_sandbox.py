# -*- coding: utf-8 -*-
"""JP-05: 索引库 frontmatter「源文件:」字段沙箱校验。

受控 RED→GREEN：源文件字段为库外绝对路径或 `../` 穿越时，必须被沙箱拦截
（解析器抛 ValueError；读取侧返回空串且绝不把库外内容送入 prompt）；
合法库内相对路径保持可读。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from v4.core import card_factory  # noqa: E402
from v4.enhance import index_lib  # noqa: E402
from v4.enhance.index_lib import IndexLibrary  # noqa: E402

SECRET_MARK = "TOP-SECRET-MARKER-7f3a9c"


@pytest.fixture()
def lib_with_evil_source(tmp_path):
    """索引库 + 库外“敏感文件”：源文件字段指向库外绝对路径。"""
    root = tmp_path / "lib"
    (root / "章节索引").mkdir(parents=True)
    (root / "小说原文" / "good-作者").mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text(
        f"{SECRET_MARK} 第一章 相遇，森田推门进入大厅开始他的冒险旅程。\n"
        "\n"
        "第二段正文内容足够长可以作为文风范例被抽取出来使用。\n",
        encoding="utf-8",
    )
    (root / "章节索引" / "evil-x-idx.md").write_text(
        "---\n"
        "小说名称: evil\n"
        "作者: x\n"
        f"源文件: {secret}\n"
        "---\n"
        "## 第1章（行号：1 ~ 2）\n"
        "### 剧情概要\n- 条目\n",
        encoding="utf-8",
    )
    (root / "小说原文" / "good-作者" / "seg-001.md").write_text(
        "---\n小说名称: good\n---\n第1章 开篇\n\n合法库内正文段落，长度足够可以作为文风范例。\n",
        encoding="utf-8",
    )
    return root, secret


def test_prose_fragment_blocks_absolute_outside_path(lib_with_evil_source):
    root, secret = lib_with_evil_source
    lib = IndexLibrary(root)
    fragment = lib.original_prose_fragment(str(secret), None)
    assert fragment == "", "库外绝对路径必须被拦截，不得返回任何内容"
    assert SECRET_MARK not in fragment


def test_prose_fragment_blocks_traversal(lib_with_evil_source):
    root, _ = lib_with_evil_source
    lib = IndexLibrary(root)
    assert lib.original_prose_fragment("../secret.txt", None) == ""


def test_structure_digest_blocks_outside_path(lib_with_evil_source):
    root, secret = lib_with_evil_source
    lib = IndexLibrary(root)
    assert lib.original_structure_digest(str(secret), None) == "", \
        "结构摘要也不得成为库外文件的存在/统计预言机"


def test_resolver_rejects_escape_shapes(lib_with_evil_source):
    root, secret = lib_with_evil_source
    resolve = getattr(index_lib, "resolve_index_source", None)
    assert callable(resolve), "index_lib 必须提供源文件沙箱解析器"
    with pytest.raises(ValueError):
        resolve(root, str(secret))
    with pytest.raises(ValueError):
        resolve(root, "../secret.txt")
    with pytest.raises(ValueError):
        resolve(root, "/etc/passwd")
    with pytest.raises(ValueError):
        resolve(root, "C:\\Windows\\System32\\drivers\\etc\\hosts")
    # 合法相对路径放行
    ok = resolve(root, "小说原文/good-作者/seg-001.md")
    assert ok.is_relative_to(root.resolve())


def test_collect_samples_skips_evil_source(lib_with_evil_source, tmp_path):
    root, _ = lib_with_evil_source
    doc = card_factory.init_card(book="evil", author="x", lib=root,
                                 workdir=tmp_path / "cards")
    card_dir = Path(tmp_path / "cards" / doc["slug"])
    with pytest.raises(card_factory.CardFactoryError):
        card_factory.collect_samples(card_dir)
    samples = card_dir / "samples.md"
    assert not samples.exists() or SECRET_MARK not in samples.read_text(encoding="utf-8")


def test_legit_relative_source_still_readable(lib_with_evil_source):
    root, _ = lib_with_evil_source
    lib = IndexLibrary(root)
    fragment = lib.original_prose_fragment("小说原文/good-作者/seg-001.md", None)
    assert "合法库内正文段落" in fragment
    assert lib.original_structure_digest("小说原文/good-作者/seg-001.md", None) != ""
