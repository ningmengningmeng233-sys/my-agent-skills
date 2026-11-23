# -*- coding: utf-8 -*-
"""文档里的命令行承诺必须能兑现（第十七次同类缺陷：名不副实的入口）。

背景：SKILL.md 与 references/WORKFLOW.md 曾写着 `python -m dramacraft.premise_to_outline`，
而该模块根本没有入口——照文档敲命令的人会拿到 exit 0 且什么都没发生的**假成功**。
本用例把「文档敲得出、模块跑得动」钉成常驻断言（只扫合同类文档，CHANGELOG 是历史记录不约束）：
  ① 文档里每一条 `python -m dramacraft.<mod>` 都必须指向一个带 main/_cli_main 的模块；
  ② 文档里每一个 `dramacraft.<name>` 引用都必须解析得到（模块文件在，或包属性在）；
  ③ 文档里每一条 `python run_batch.py` 承诺的脚本文件必须在；
  ④ 「描述当前仓库」的文档（README / SKILL）不许抄测试条数——本轮就查出 `README.md`
     目录结构段还写着「351 项测试」，而同时实测 441，与作者风险登记 R9 的口径正好相反。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC_GLOBS = ["SKILL.md", "README.md", "references/*.md"]

CLI_RE = re.compile(r"python -m (dramacraft\.[a-z_][a-z0-9_]*)")
REF_RE = re.compile(r"\bdramacraft\.([a-z_][a-z0-9_]*)")

# 「描述当前仓库」的文档——不许抄测试条数（一抄必滞后）。
# CHANGELOG 与各带日期的对账节是时点快照，允许写数字，故不在此列。
CURRENT_STATE_DOCS = ["SKILL.md", "README.md"]
COUNT_RE = re.compile(r"\d+\s*(?:项|条|个)\s*测试|\d+\s+passed")


def _doc_texts():
    texts = {}
    for pattern in DOC_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            texts[path.relative_to(ROOT).as_posix()] = path.read_text("utf-8")
    assert len(texts) >= 3, f"没扫到足够的文档，断言会空转：{list(texts)}"
    return texts


def _cli_claims():
    """返回 {文档相对路径: [(行号, 模块名)]}。"""
    claims = {}
    for rel, text in _doc_texts().items():
        hits = [(i, mod) for i, line in enumerate(text.splitlines(), 1)
                for mod in CLI_RE.findall(line)]
        if hits:
            claims[rel] = hits
    assert claims, "一条命令行承诺都没扫到，断言会空转"
    return claims


def test_every_documented_module_cli_has_an_entry_point():
    import importlib
    for rel, hits in sorted(_cli_claims().items()):
        for line_no, mod in hits:
            module = importlib.import_module(mod)
            has_cli = callable(getattr(module, "main", None)) or \
                callable(getattr(module, "_cli_main", None))
            assert has_cli, f"{rel}:{line_no} 承诺了 {mod} 命令行入口，模块里没有 main/_cli_main"


def test_every_documented_dramacraft_reference_resolves():
    import dramacraft
    for rel, text in sorted(_doc_texts().items()):
        for line_no, line in enumerate(text.splitlines(), 1):
            for name in REF_RE.findall(line):
                resolved = (ROOT / "dramacraft" / f"{name}.py").exists() or \
                    hasattr(dramacraft, name)
                assert resolved, f"{rel}:{line_no} 引用了不存在的 dramacraft.{name}"


def test_run_batch_cli_claim_still_points_to_a_script():
    for rel, text in sorted(_doc_texts().items()):
        for line_no, line in enumerate(text.splitlines(), 1):
            if "run_batch.py" not in line or "python" not in line:
                continue
            assert (ROOT / "run_batch.py").exists(), f"{rel}:{line_no} 承诺了 run_batch.py 入口"


def test_scanner_would_catch_a_phantom_entry():
    """自证扫描面有效：把假入口塞进文本里必须被 CLI_RE 抓到且真的解析不到。"""
    hits = CLI_RE.findall("跑 `python -m dramacraft.ghost_module` 即可")
    assert hits == ["dramacraft.ghost_module"]
    import importlib
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(hits[0])


def test_current_state_docs_do_not_hardcode_test_counts():
    for rel in CURRENT_STATE_DOCS:
        path = ROOT / rel
        assert path.exists(), f"待扫文档 {rel} 不在，断言会空转"
        for line_no, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            hit = COUNT_RE.search(line)
            assert not hit, (f"{rel}:{line_no} 抄了测试条数「{hit.group(0)}」，必然滞后；"
                             "改写成「条数以 python -m pytest tests -q 实测为准」")


def test_count_scanner_would_catch_a_stale_number():
    """自证④有效：三种常见写法都要能被同一条正则抓到。"""
    for text in ["tests/              # 351 项测试（自带播种）",
                 "基线 355 条测试",
                 "本轮 439 passed，常规与 clean shell 各一次"]:
        assert COUNT_RE.search(text), text
