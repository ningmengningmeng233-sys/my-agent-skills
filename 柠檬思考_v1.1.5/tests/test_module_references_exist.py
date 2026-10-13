# -*- coding: utf-8 -*-
"""门面文件的引用完整性（工单 T10 项：SKILL.md 指向的模块必须逐条真的在）。

现场：`SKILL.md` 是单一事实源骨架，模块细则靠 `modules/<名>/MODULE.md` 指路；
上游把门面改名（SKILL.md → MODULE.md）是为了避免 15 个幽灵技能条目。
改名或挪目录时漏改指向，收件人按骨架读细则会当场撞空文件——没人专门量它就不会报错。

我第一版只抓反引号里的路径，跑「把 modules/brainstorming 改名」的变异时**三条全绿**：
骨架的状态机段是在围栏代码块里用 `modules/brainstorming` 这种裸写法指路的，
反引号正则一条也没匹配到，于是"缺失 0 项"是空集合给的假清白。
现在按两条判据量：① 骨架里（含代码块）提到的每个模块名都必须有目录且带 MODULE.md；
② modules/ 下每个目录都必须被骨架提到，不认孤儿模块。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MENTION = re.compile(r"modules/([A-Za-z0-9][A-Za-z0-9_\-]*)")


def _skill_text() -> str:
    return (ROOT / "SKILL.md").read_text(encoding="utf-8")


def _module_dirs() -> set[str]:
    base = ROOT / "modules"
    return {p.name for p in base.iterdir() if p.is_dir()} if base.is_dir() else set()


def test_every_module_mentioned_in_the_facade_exists_with_a_facade_file():
    mentioned = set(MENTION.findall(_skill_text()))
    assert mentioned, "骨架里一条模块引用都没匹配到——这条尺本身失效了，别当通过用"
    dirs = _module_dirs()
    missing = sorted(m for m in mentioned if m not in dirs)
    assert not missing, f"骨架指向的模块目录不存在（收件人会撞空引用）：{missing}"
    no_facade = sorted(m for m in mentioned if m in dirs
                       and not (ROOT / "modules" / m / "MODULE.md").is_file())
    assert not no_facade, f"这些模块目录里没有门面文件 MODULE.md：{no_facade}"


def test_no_orphan_module_dirs():
    """modules/ 下每个目录名都必须出现在骨架里（索引表用的是裸名，不是 modules/ 前缀）。

    判据按目录名自身查：目录被改名时新名自然查不到，就会红——
    这比"从文本里抠 modules/ 引用"更宽，也正好覆盖索引表那一列的裸写法。
    """
    text = _skill_text()
    orphans = sorted(d for d in _module_dirs() if d not in text)
    assert not orphans, f"这些模块存在但骨架没指路（等于收件人永远不会读到）：{orphans}"


def test_no_upstream_named_facade_left_behind():
    bad = sorted(d for d in _module_dirs()
                 if (ROOT / "modules" / d / "SKILL.md").is_file())
    assert not bad, f"这些模块还留着上游原名 SKILL.md（会注册成幽灵技能）：{bad}"
