# -*- coding: utf-8 -*-
"""发布包常驻门：身份、脱敏、柜根引用申报、Node 自测入口。

这些不是「检查工具越修越精」，而是别人拿到 ZIP 后会不会当场撞墙的四件事：
作者家目录写死在文档里、没有许可声明、文档指挥收件人跑开发柜路径、
五套 Node 自测没有可读入口。每条都对应一次现场。
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 与包级公开扫描同一把尺：盘符+用户目录形状，或作者机器上的私人目录名
PRIVATE_SHAPE = re.compile(r"[A-Za-z]:[/\\](?:Users|home|迁移|Janson)")
CABINET_ONLY = re.compile(r"(?<![A-Za-z0-9_./\\])schemas/[A-Za-z0-9_.-]+")


def _is_text(f: Path) -> bool:
    """按扩展名筛文本，但无扩展名的分发件（LICENSE/NOTICE/VERSION）也必须进尺。"""
    if f.suffix.lower() in {".md", ".py", ".mjs", ".json", ".txt", ".yaml", ".yml",
                            ".html", ".css", ".js", ".ps1"}:
        return True
    return f.suffix == "" and f.name.upper() in {"LICENSE", "NOTICE", "VERSION", "README",
                                                 "CHANGELOG", "SHA256SUMS"}


def _shipped_text():
    """参与打包的文本件（跳过 .git 与缓存，跳不过二进制靠扩展名筛）。"""
    skip_parts = {".git", "__pycache__", ".pytest_cache", "workspace", "node_modules", ".venv"}
    for f in sorted(ROOT.rglob("*")):
        if not f.is_file() or any(p in skip_parts for p in f.parts):
            continue
        if not _is_text(f):
            continue
        yield f


def test_shipped_text_carries_no_author_home_path():
    """收件机上不该出现「盘符 + Users + 用户名」这种形状：既是私人路径，也是断链。

    现场：`SKILL.md` 与 `skills_runtime/novel-storyboard/references/h3-prompt.md` 把 H3 权威骨架
    的绝对家目录写进 `derived_from`。柜根对账用 `os.path.expanduser` 归一，
    写成 `~/.agents/skills/...` 一样能核，且不泄露作者机器。
    """
    hits = []
    for f in _shipped_text():
        for num, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if PRIVATE_SHAPE.search(line):
                hits.append(f"{f.relative_to(ROOT).as_posix()}:{num}")
    assert not hits, f"内嵌私人绝对路径：{hits[:6]}"


def test_license_and_version_identity_present():
    """分发要写「不许再分发」的依据；版本号要有第二处可对账，漂了当场说。"""
    lic = ROOT / "LICENSE"
    assert lic.is_file(), "缺 LICENSE：frontmatter 写了 license: Proprietary 却没有许可正文"
    assert "Proprietary" in lic.read_text(encoding="utf-8")
    body = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"^\s*version:\s*\"?([0-9.]+)\"?", body, re.M)
    assert m, "SKILL.md frontmatter 缺 metadata.version"
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    top = re.search(r"^## v([0-9.]+)", changelog, re.M)
    assert top, "CHANGELOG 顶部没有 vX.Y.Z 段"
    assert m.group(1) == top.group(1), f"版本漂了：SKILL.md={m.group(1)} CHANGELOG={top.group(1)}"


def test_cabinet_only_references_are_declared():
    """文档里指向开发柜（`schemas/...`）的句子必须逐条申报，未登记判红、登记失效也判红。

    柜根 `schemas/contract_check.py` 不在发布包内；收件人照文档执行必然「找不到文件」。
    申报件同时写清「收件人该怎么办」，不是把引用删掉了事。
    """
    registry = ROOT / "scripts" / "package_governance" / "CABINET_ONLY_REFS.json"
    declared = set()
    if registry.is_file():
        doc = json.loads(registry.read_text(encoding="utf-8"))
        declared = {str(r.get("reference", "")).strip() for r in doc.get("references", [])}
    used = set()
    for f in _shipped_text():
        # 申报件与尺子本身不参与扫描：它们的字面量是要检测的形状，不是对收件人的指令
        if f == registry or f == Path(__file__):
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            for m in CABINET_ONLY.finditer(line):
                used.add(m.group(0).strip(" `").replace("\\", "/"))
    unregistered = sorted(x for x in used if x not in declared)
    stale = sorted(x for x in declared if x not in used)
    assert not unregistered, f"未登记的柜根引用：{unregistered}"
    assert not stale, f"登记已失效（文档里已不引用）：{stale}"


def test_example_media_are_pinned_and_nothing_extra_sneaks_in():
    """包里的每张图都要在钉单里，且摘要对得上；多出一张没登记图的也是红。

    现场：6 张 README 样例图被构建器按 `UNREGISTERED_MEDIA` 拒建。二进制逃过文本扫描，
    换一张图不会有任何字面量变化——只能靠摘要钉住。钉单由柜侧执行体生成，不手抄。
    """
    import hashlib
    suffixes = {".webp", ".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mov"}
    pin = ROOT / "scripts" / "package_governance" / "MEDIA_PIN.json"
    assert pin.is_file(), "缺媒体钉单：新增示例图前先跑柜侧 scripts/media_pin.py 生成"
    media = json.loads(pin.read_text(encoding="utf-8")).get("media") or {}
    assert media, "钉单里没有条目"
    for rel, info in media.items():
        f = ROOT / rel
        assert f.is_file(), f"钉单指向的图不在树上：{rel}"
        assert hashlib.sha256(f.read_bytes()).hexdigest() == info["sha256"], f"图内容变了：{rel}"
    skip = {".git", "__pycache__", ".pytest_cache", "node_modules"}
    actual = {f.relative_to(ROOT).as_posix() for f in ROOT.rglob("*")
              if f.is_file() and f.suffix.lower() in suffixes
              and not (skip & set(f.relative_to(ROOT).parts))}
    assert sorted(actual - set(media)) == [], f"未登记的媒体：{sorted(actual - set(media))}"


def test_node_selftest_entries_are_reachable_and_documented():
    """五套 skills_runtime Node 自测要有入口，且 SKILL.md 里能查到怎么跑。

    现场：`selftest.mjs` 实测全过（1179 项），但 SKILL.md 速查表只写了 `validate.mjs`，
    收件人不会知道有这五个入口，也不会去跑——等于没有验证手段。
    """
    entries = sorted((ROOT / "skills_runtime").glob("*/scripts/selftest.mjs"))
    assert len(entries) == 5, f"Node 自测入口应有 5 个，实有 {len(entries)}：{[e.parent.parent.name for e in entries]}"
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "selftest.mjs" in skill, "SKILL.md 未记录 Node 自测入口"
