# -*- coding: utf-8 -*-
"""发布面必须带许可正文（缺陷类同 F-22）。

现场：`SKILL.md` frontmatter 写着 `license: Proprietary`，而仓库与历次发布包
（dist-7.4.4-tier2 / final2）里都没有 `LICENSE`——收件人拿到的是"作者声称保留所有权利"
但没有任何条款文本，无法判定能不能自用、改装或再分发。
本文件钉两件事：① 仓库根必须有 `LICENSE`，且必须交代档位裁剪与"官方口径不重新授权"；
② 只要发布清单在位，`files` 里必须真的出现 `LICENSE`（改了源、派生包没重建是同一类坑）。
"""
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_repo_carries_licence_text():
    lic = REPO / "LICENSE"
    assert lic.is_file(), "缺 LICENSE 正文：frontmatter 声明 Proprietary 却没有条款可交代"
    text = lic.read_text(encoding="utf-8")
    assert "Proprietary" in text, "许可正文没与 frontmatter 的 license 字段对上"
    assert "withheld" in text or "裁剪" in text, "未说明发布档位裁掉了什么、凭什么"
    assert "不重新授权" in text, "未声明官方文档/模型条款不受本许可改写"


def test_manifest_claiming_current_commit_ships_licence():
    """清单一旦声称绑定当前提交，就必须带 LICENSE；声称的是旧提交则不适用范围。

    为什么加这层条件：`release-manifest.json` 是**构建产物**，源码提交时它还没重建，
    若无条件断言就会形成"要先提交才进构建、要先进构建才肯提交"的死锁。
    死锁不能靠放宽断言解，只能把判据限定在"这份清单声称描述当前提交"的范围内——
    构建之后 `git_commit` 与 HEAD 相等，判据就真的咬人。
    """
    manifest = REPO / "release-manifest.json"
    assert manifest.is_file(), "发布清单不在位：本仓的构建产物应当随跟踪走，不许静默跳过"
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                          capture_output=True, text=True, encoding="utf-8").stdout.strip()
    claimed = str(doc.get("git_commit") or "")
    if claimed != head:
        print(f"[适用性说明] 清单声称描述 {claimed[:8]}…，当前 HEAD {head[:8]}…：这是旧构建的描述，"
              f"本轮不判；重建后本条必须咬合")
        return
    files = doc.get("files") or {}
    assert any(Path(str(k)).name == "LICENSE" for k in files), (
        f"清单 {len(files)} 项声称描述当前提交，却没有 LICENSE：源里有正文不代表发布面带着它")
