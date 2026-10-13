#!/usr/bin/env python3
"""P30 回归守卫：契约用例必须对 pytest 可见（防「0 collected 却全绿」复发）。

历史缺陷：`scripts/test_contracts.py` 的 30 条契约函数命名为 `t_*`，pytest 一条也不收集，
CI 因此长期把这个文件当成绿色通过——门不存在却让人以为有。现在契约经
`test_contract[...]` 参数化暴露给 pytest，本文件用 `--collect-only` 实测收集结果，
与 `ALL_TESTS` 逐名比对：改名、移出 testpaths、适配器被删，任何一条漏收集都判红。
"""
import codecs
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))
from conftest import run_py  # noqa: E402  B1 范式：起 Python 子进程的唯一出口
import test_contracts as tc  # noqa: E402


def _node_ids(stdout: str) -> set[str]:
    """pytest 把非 ASCII 参数 id 转义成 \\uXXXX 打印，还原后才能与 ALL_TESTS 名称比对。"""
    return {codecs.decode(raw, "unicode_escape")
            for raw in re.findall(r"test_contract\[(.+?)\]", stdout)}


def test_pytest_collects_every_contract_case():
    """pytest 收集到的契约用例 = ALL_TESTS 全名（缺一条即红，条数退化即红）"""
    res = run_py(["-m", "pytest", str(_HERE / "test_contracts.py"),
                  "--collect-only", "-q", "--no-header", "-p", "no:cacheprovider"],
                 cwd=str(Path(tc.SKILL)), timeout=600)
    assert res.returncode == 0, f"collect-only 退出码 {res.returncode}\n{res.stdout}{res.stderr}"
    collected = _node_ids(res.stdout)
    names = [name for name, _ in tc.ALL_TESTS]
    missing = [name for name in names if name not in collected]
    assert not missing, f"pytest 未收集到 {len(missing)} 条契约用例: {missing}"
    assert len(names) >= 30, f"契约用例总数退化为 {len(names)}（应 ≥30）"
