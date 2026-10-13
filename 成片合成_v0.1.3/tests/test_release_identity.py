# -*- coding: utf-8 -*-
"""发布身份：许可正文、frontmatter 与来源说明必须三者齐全且互相自洽。

现场（2026-10-02）：本树 frontmatter 写 `license: Proprietary`，而仓库与历次发布包
**都没有 LICENSE 正文**，也没有来源说明——收件人拿到"保留所有权利"却看不到条款，
也不知道哪些文件是自写、哪些来自治理仓模板或从 SKILL.md 外移。
作者已裁定：我拥有授权权利的自有内容采用 MIT，且必须保留作者署名，
第三方内容保留其原有版权与声明、不得冒称自有。

本文件钉四件事：① LICENSE 在位且与 frontmatter 同一口径；
② 版权人是本技能作者，不是上游作者；③ 来源说明件在位并逐类覆盖（自写／模板生成／文件外移）；
④ 判据本身不许空转——必须真的扫到文件并读到字段。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _skill_text() -> str:
    return (ROOT / "SKILL.md").read_text(encoding="utf-8", errors="replace")


def test_guard_reads_the_real_surface():
    """正对照：入口文件与 frontmatter 字段都必须读得到，否则下面的断言是空转。"""
    skill = _skill_text()
    assert (ROOT / "SKILL.md").is_file(), "读不到 SKILL.md，这条尺没在量东西"
    assert re.search(r"^license:\s*(\S+)", skill, re.M), "frontmatter 没有 license 字段"
    assert re.search(r"^\s*version:\s*\"?([0-9.]+)", skill, re.M), "frontmatter 没有 version 字段"


def test_licence_text_present_and_consistent_with_frontmatter():
    skill = _skill_text()
    fm = re.search(r"^license:\s*(\S+)", skill, re.M).group(1).strip().strip('"\'')
    lic = ROOT / "LICENSE"
    assert lic.is_file(), "缺 LICENSE 正文：frontmatter 声明了授权方式却没有条款可交代"
    text = lic.read_text(encoding="utf-8", errors="replace")
    assert fm.lower() in text.lower(), f"LICENSE 与 frontmatter 的 license={fm} 对不上"
    assert "Permission is hereby granted" in text if fm.lower() == "mit" else True


def test_copyright_holder_is_this_skill_author_not_upstream():
    """版权人行必须等于 SKILL.md 的 author（自有内容不得写成上游作者）。"""
    skill = _skill_text()
    author = re.search(r"^author:\s*(.+)$", skill, re.M).group(1).strip().strip('"\'')
    lic = (ROOT / "LICENSE").read_text(encoding="utf-8", errors="replace")
    holders = re.findall(r"Copyright \((?:c|C)\)\s*\d{4}(?:-\d{4})?\s+(.+)", lic)
    assert holders, "LICENSE 里没有版权人行"
    assert any(author in h for h in holders), f"版权人写成了 {holders}，与 author={author} 不符"


def test_source_provenance_note_covers_all_three_kinds():
    """来源说明必须三类都在：自写／治理仓模板生成／从 SKILL.md 外移。"""
    cands = sorted(ROOT.glob("SOURCE-NOTES.md")) + sorted(ROOT.glob("docs/SOURCE-NOTES.md"))
    assert cands, "缺来源说明件（SOURCE-NOTES.md）：不能让人以为整包都是同一来源"
    text = cands[0].read_text(encoding="utf-8", errors="replace")
    for kind, pat in (("自写", r"自写|自有|本仓"),
                      ("模板生成", r"模板|gitguard|治理仓"),
                      ("文件外移", r"外移|搬移|SKILL\.md")):
        assert re.search(pat, text), f"来源说明缺「{kind}」这一类：{cands[0].name}"
