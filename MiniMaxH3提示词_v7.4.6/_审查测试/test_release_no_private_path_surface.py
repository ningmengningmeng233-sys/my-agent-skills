# -*- coding: utf-8 -*-
"""发布面不得带作者机绝对路径，也不得把柜内过程文档当交付物（缺陷类同 F-11/F-22）。

现场（2026-10-02）：dist-7.4.4-final3 的包级公开扫描报出 25 条阻断级 PRIVATE_PATH，
分布在 6 份「文档/柠檬」下的交接、工单、设计、验收工作件里——内容是盘符加 Users 加
用户名的工作树/技能目录绝对路径。收件人不需要这些过程文档，而它们把作者机器形状
带进了分发面。我用自己手搓的正则扫解压树时报 0 命中（转义被 shell 吃掉），
是包级权威扫描替我兜住了这次假清白。

两条判据：① 清单里不允许出现「文档/柠檬」开头的条目；
② 清单里每一份文本件都不许命中私路形状。
"""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PRIVATE = re.compile(r"[A-Za-z]:[\\/]{1,2}(?:Users|home|迁移)")
USERNAME = re.compile(r"[A-Za-z]:[\\/]?.{0,80}Janson", re.I)
TEXT_SUFFIX = {".md", ".txt", ".json", ".py", ".yml", ".yaml", ".html", ".cfg", ".toml"}
INTERNAL_PREFIX = "文档/柠檬"


def _manifest_files():
    manifest = REPO / "release-manifest.json"
    assert manifest.is_file(), "发布清单不在位，判据无法执行（不许静默跳过）"
    return json.loads(manifest.read_text(encoding="utf-8")).get("files") or {}


def test_internal_process_docs_are_not_shipped():
    bad = sorted(str(k) for k in _manifest_files() if str(k).startswith(INTERNAL_PREFIX))
    assert not bad, f"柜内过程文档进了发布面：{bad[:5]}（共 {len(bad)} 项）"


def test_no_shipped_text_file_carries_author_machine_paths():
    offenders = []
    for rel in _manifest_files():
        rel = str(rel)
        p = REPO / rel
        if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIX:
            continue
        for num, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if PRIVATE.search(line) or USERNAME.search(line):
                offenders.append(f"{rel}:{num}")
    assert not offenders, f"发布面文本件带作者机绝对路径：{offenders[:6]}（共 {len(offenders)} 处）"
