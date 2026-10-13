# -*- coding: utf-8 -*-
"""发布自证：身份件、启动器契约、柜根引用与合成密钥申报。

四条都是现场实测出来的缺口，不是设想：
- 发布清单曾写 5.4.5 而冻结树处处写 5.4.4，版本没有单一权威点就会签出错版号；
- 树内没有 LICENSE，只有 frontmatter 一句话，分发时收件人无法核验许可；
- SKILL.md 让读者跑 `schemas/contract_check.py`（柜根件），独立安装照做必失败；
- `scripts/failover_drill.py` 用三条 `sk-` 形状的合成 key 演练故障转移，
  公开扫描因行内含 FAKE 而整条抑制，于是包级读数报「凭据命中 0」——那是尺子闭眼，
  不是包干净。所以这里要求它**逐条申报**（含用途与对应测试），不申报即红。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOV = ROOT / "scripts" / "package_governance"


def _front_version(text: str) -> str | None:
    m = re.search(r"\n\s*version:\s*\"?([\w.\-]+)\"?", text)
    return m.group(1) if m else None


def test_version_has_single_authority_across_three_points():
    declared = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    skill = _front_version((ROOT / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
    pyproject = re.search(r'^version\s*=\s*"([^"]+)"',
                          (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
                          re.M)
    assert skill, "SKILL.md frontmatter 里没有 version"
    assert pyproject, "pyproject.toml 里没有 version"
    trio = {"VERSION": declared, "SKILL.md": skill, "pyproject.toml": pyproject.group(1)}
    assert len(set(trio.values())) == 1, f"三处版本不一致：{trio}"


def test_license_text_ships_with_the_package():
    license_file = ROOT / "LICENSE"
    assert license_file.is_file(), "缺 LICENSE：frontmatter 的 license 声明需要落成可核验文本"
    text = license_file.read_text(encoding="utf-8")
    assert "柠檬才不萌" in text and "Proprietary" in text, text[:200]


def test_launcher_contract_holds():
    ps = (ROOT / "start_web.ps1").read_text(encoding="utf-8", errors="replace")
    assert "$PSScriptRoot" in ps, "启动器不得依赖调用方当前目录"
    assert "exit $LASTEXITCODE" in ps, "子进程退出码必须透传，否则服务启动失败会被读成成功"
    assert "$env:KNOWLEDGE_INDEX_PYTHON" in ps, "解释器必须可由参数或环境覆盖"
    assert not re.search(r"[A-Za-z]:[\\/](?:Users|迁移)", ps), "启动器不得留个人绝对路径回退"


def test_cabinet_only_references_are_declared():
    registry = GOV / "CABINET_ONLY_REFS.json"
    assert registry.is_file(), "缺柜根引用登记册：文档指向包外路径就必须逐条说明"
    declared = {item["reference"] for item in
                json.loads(registry.read_text(encoding="utf-8"))["references"]}
    found = set()
    for p in ROOT.rglob("*.md"):
        if ".git" in p.parts or "node_modules" in p.parts:
            continue
        found |= set(re.findall(r"schemas/[\w.\-]+",
                                 p.read_text(encoding="utf-8", errors="replace")))
    present_in_package = {r for r in found if (ROOT / r).is_file()}
    missing = sorted(found - present_in_package - declared)
    stale = sorted(declared - found)
    assert not missing, f"未登记的柜根引用（收件人照做必失败）：{missing}"
    assert not stale, f"登记已失效（说明文档改了但登记没跟）：{stale}"


def test_synthetic_key_literals_are_declared_one_by_one():
    registry = GOV / "DECLARED_TEST_SECRETS.json"
    assert registry.is_file(), "包内含 key 形状字面量却没有申报"
    doc = json.loads(registry.read_text(encoding="utf-8"))
    declared = {(item["path"], item["line"]) for item in doc["entries"]}
    found = set()
    shape = re.compile(r"\b(?:sk|ghp|github_pat|xox[bap])[-_][A-Za-z0-9\-]{16,}")
    for p in (ROOT / "scripts").rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        for num, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if shape.search(line):
                found.add((rel, num))
    undeclared = sorted(found - declared)
    assert not undeclared, f"未申报的 key 形状字面量：{undeclared}"
    for rel, num in sorted(found):
        entry = next(i for i in doc["entries"] if i["path"] == rel and i["line"] == num)
        assert entry["verdict"] == "TEST_PLACEHOLDER", entry
        assert entry["covered_by"], "合成件也要说明它撑着哪条测试"


def test_scripts_do_not_probe_other_users_home_directories():
    """脚本不得内嵌「盘符 + 家目录」式路径去猜别人的模型目录。

    现场：hardware_profile 在 home 下的模型目录之外，又拼了一次 Windows 用户目录去猜
    同名 .ollama/models。收件机上它既是私有路径形状（脱敏扫描按真件报），也是越界探测
    别的用户目录，而 home 那条已覆盖本机场景，属纯冗余。
    """
    # 判据与包内门同源：只写到家目录一级、没有尾随分隔符的拼法也要抓到，
    # 否则现场那条 hardware_profile 的写法会从测试眼里溜过去。
    pattern = re.compile(r"[A-Za-z]:[/\\](?:Users|home|迁移|Janson)")
    offenders = []
    for f in sorted((ROOT / "scripts").rglob("*.py")):
        rel = f.relative_to(ROOT).as_posix()
        for num, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{rel}:{num}")
    assert not offenders, f"内嵌用户目录路径：{offenders[:5]}"
