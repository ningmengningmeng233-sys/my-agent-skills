"""KB-01 受控 RED/GREEN：段索引降级不得伪造首章正文，引用须落在本节区间内。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_segment_index import expand_merged_sections, validate_index_output  # noqa: E402
import check_line_refs  # noqa: E402


FRONTMATTER = """---
小说名称: 测试小说
作者: 测试作者
索引类型: 段索引
索引序号: 001
章节范围: 第1章~第3章
总章节数: 3
是否完结: 否
包含元素:
源文件: 原文/测试-001.md
创建日期: 2026-01-01
最后更新: 2026-01-01
状态: 草稿
---
"""

UNIQUE_MARK = "UNIQUE_FIRST_BODY_12345"


def _seg_file(tmp_path):
    seg = tmp_path / "seg-001.md"
    lines = []
    lines.append("---")
    lines.append("小说名称: 测试小说")
    lines.append("作者: 测试作者")
    lines.append("章节范围: 第1章~第3章")
    lines.append("---")
    lines.append("第一章 第一节")
    for i in range(2, 12):
        lines.append(f"第一节正文第{i}行 {UNIQUE_MARK if i == 5 else ''}".strip())
    lines.append("第二章 第二节")
    for i in range(13, 22):
        lines.append(f"第二节正文第{i}行")
    lines.append("第三章 第三节")
    for i in range(23, 31):
        lines.append(f"第三节正文第{i}行")
    seg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return seg


def _merged_content():
    return (
        FRONTMATTER
        + "\n## 本段概况\n\n概况占位。\n\n"
        + f"## 第一章 第一节（行号：1 ~ 30）\n\n### 概要\n\n{UNIQUE_MARK} 首节真实正文。\n\n"
        + '### 关键引用\n\n"首节金句原文内容测试"（行5，简述）\n\n'
        + "## 本段总结\n\n总结占位。\n"
    )


def test_expand_must_not_copy_first_section_body(tmp_path):
    seg = _seg_file(tmp_path)
    chapters = ["第一章 第一节", "第二章 第二节", "第三章 第三节"]
    out = expand_merged_sections(_merged_content(), str(seg), chapters)
    # 缺节不得复制首节正文：独特标记只允许出现一次（首节自身）。
    assert out.count(UNIQUE_MARK) <= 1, "降级补节复制了首节正文（伪造）"
    # 缺节必须明确标记 NEEDS_REVIEW，或让整体校验失败（不得静默放行伪造节）。
    err = validate_index_output(out, str(seg), chapters)
    assert ("NEEDS_REVIEW" in out) or (err is not None), (
        "缺节既无 NEEDS_REVIEW 标记又通过校验，属静默绕过门禁"
    )


def test_cross_section_ref_must_be_flagged(tmp_path, monkeypatch, capsys):
    lib = tmp_path / "lib"
    yuan = lib / "原文"
    duan = lib / "段索引"
    yuan.mkdir(parents=True)
    duan.mkdir(parents=True)
    seg_lines = ["第一章 开头"] + [f"首章填充{i}" for i in range(2, 11)]
    seg_lines += [f"间隙填充{i}" for i in range(11, 20)]
    seg_lines += ["第二章 开头"] + [f"次章填充{i}" for i in range(21, 31)]
    (yuan / "seg-001.md").write_text("\n".join(seg_lines) + "\n", encoding="utf-8")
    idx = (
        "---\n源文件: 原文/seg-001.md\n---\n\n"
        + "## 第一章 开头（行号：1 ~ 10）\n\n"
        + '"跨节引用金句内容测试数据"（行25，首节引用次节行号）\n\n'
        + "## 第二章 开头（行号：20 ~ 30）\n\n次节正文。\n"
    )
    (duan / "seg-001-idx.md").write_text(idx, encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["check_line_refs.py", "--lib", str(lib)]
    )
    rc = check_line_refs.main()
    capsys.readouterr()
    # 行25落在全局某节区间内，但不在引用所在第一节（1~10）内，必须判章外失败。
    assert rc == 1, "跨节引用落在他节区间却被当章内放行（门禁绕过）"
