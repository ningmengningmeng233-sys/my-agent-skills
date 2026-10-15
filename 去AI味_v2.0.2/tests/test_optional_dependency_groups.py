# -*- coding: utf-8 -*-
"""TICKET-55'：核心 / 相似度可选依赖组契约。

把 105 行的 requirements-v2.lock 拆成轻量核心锁（24 行）与相似度扩展锁
（81 行）后，安装形态、运行契约与缺依赖披露必须同时成立：

  1. 两锁是基线的一刀两断：24 + 81 = 105，不相交、无重复、每行可解析，
     点名包（torch/sklearn/numpy/transformers 进 similarity；
     transitions/six/keyring/typesafe-sdk/pyyaml/pip/setuptools 留 core）落位；
  2. EXTERNAL_DEPENDENCIES 按组登记：core 三项 optional_group 为 None，
     similarity 六项为 "similarity"，且发行包名落在对应的锁里；
  3. OPTIONAL_LOCKS 映射存在且两锁都进 REQUIRED_RUNTIME_FILES；
  4. 缺相似度锁的树上，capability_report 明确披露缺哪把锁、怎么装，
     而不是报一把“看起来全绿”的能力清单。

受控 RED：本文件先于 runtime_contract.py 的修改落地；在契约修改前，
分组/映射/披露三组断言必须失败（缺 OPTIONAL_LOCKS 与 similarity 分组）。
全离线、只用 stdlib；不 import 任何第三方包。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import runtime_contract as rc

PKG_ROOT = Path(__file__).resolve().parents[1]
CORE_LOCK = "requirements-v2.lock"
SIM_LOCK = "requirements-v2-similarity.lock"

# M2 闭包划分的落定行数（PyPI requires_dist 全量求闭包，win32/py3.12/无 extras；
# 交集归 core，similarity 叠装在 core 之上）。行数变了就是划分漂了。
CORE_LINES_EXPECTED = 24
SIM_LINES_EXPECTED = 81

CORE_IMPORTS = ("transitions", "keyring", "typesafe_sdk")
SIM_IMPORTS = ("text_dedup", "semhash", "FlagEmbedding",
               "numpy", "datasets", "huggingface_hub")

# 发行包名（小写归一后）必须落在对应的锁里
CORE_DISTS_MUST = ("transitions", "six", "keyring", "typesafe-sdk",
                   "pyyaml", "pip", "setuptools")
SIM_DISTS_MUST = ("torch", "scikit-learn", "numpy", "transformers",
                  "text-dedup", "semhash", "flagembedding",
                  "datasets", "huggingface-hub")


def _lock_lines(name: str) -> list[str]:
    path = PKG_ROOT / name
    assert path.is_file(), f"锁文件缺失：{name}"
    raw = path.read_bytes()
    assert b"\r" not in raw, f"{name} 含 CR，运行文本必须 LF（构建器行尾合同）"
    lines = [ln.strip() for ln in raw.decode("utf-8").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert lines, f"{name} 为空"
    return lines


def _dist_of(line: str) -> str:
    """锁行 → 发行包名（小写）：只认 `name==ver` 与 `name @ ref` 两种形状。"""
    if "==" in line:
        name, version = line.split("==", 1)
    elif " @ " in line:
        name, version = line.split(" @ ", 1)
    else:
        raise AssertionError(f"锁行形状非法（既无 == 也无 @）：{line!r}")
    name, version = name.strip(), version.strip()
    assert name and version, f"锁行缺名或缺版本：{line!r}"
    # 发行名归一（huggingface_hub 与 huggingface-hub 是同一发行）再比对
    return re.sub(r"[-_.]+", "-", name).lower()


def test_lock_files_partition():
    """两锁是基线的一刀两断：24+81=105、不相交、无重复、行行可解析。"""
    core = _lock_lines(CORE_LOCK)
    sim = _lock_lines(SIM_LOCK)
    assert len(core) == CORE_LINES_EXPECTED, f"core 锁行数漂移：{len(core)}"
    assert len(sim) == SIM_LINES_EXPECTED, f"similarity 锁行数漂移：{len(sim)}"
    assert len(core) + len(sim) == 105, "并集不再是基线的 105 行"
    assert len(set(core)) == len(core), "core 锁内有重复行"
    assert len(set(sim)) == len(sim), "similarity 锁内有重复行"
    assert not (set(core) & set(sim)), "两锁相交：同一需求行出现在两边"
    core_dists = {_dist_of(ln) for ln in core}
    sim_dists = {_dist_of(ln) for ln in sim}
    assert not (core_dists & sim_dists), "同一发行包被两锁同时钉住"
    for dist in CORE_DISTS_MUST:
        assert dist in core_dists, f"core 锁缺点名包：{dist}"
    for dist in SIM_DISTS_MUST:
        assert dist in sim_dists, f"similarity 锁缺点名包：{dist}"
    for dist in ("torch", "scikit-learn", "numpy", "transformers"):
        assert dist not in core_dists, f"重量包漏进 core 锁，默认安装不再轻量：{dist}"


def test_dependency_groups_registered():
    """外部依赖按组登记，且发行包名落在对应的锁里。"""
    table = rc.EXTERNAL_DEPENDENCIES
    for name in CORE_IMPORTS:
        assert name in table, f"core 依赖掉出登记表：{name}"
        assert table[name]["optional_group"] is None, \
            f"core 依赖被标成可选组：{name}"
    for name in SIM_IMPORTS:
        assert name in table, f"similarity 依赖掉出登记表：{name}"
        assert table[name]["optional_group"] == "similarity", \
            f"similarity 依赖未分组（仍是核心）：{name}"
    core_dists = {_dist_of(ln) for ln in _lock_lines(CORE_LOCK)}
    sim_dists = {_dist_of(ln) for ln in _lock_lines(SIM_LOCK)}
    for name in CORE_IMPORTS:
        dist = re.sub(r"[-_.]+", "-", table[name]["distribution"]).lower()
        assert dist in core_dists, \
            f"core 登记的发行包不在 core 锁里：{name}"
    for name in SIM_IMPORTS:
        dist = re.sub(r"[-_.]+", "-", table[name]["distribution"]).lower()
        assert dist in sim_dists, \
            f"similarity 登记的发行包不在 similarity 锁里：{name}"


def test_optional_locks_mapping_and_required():
    """OPTIONAL_LOCKS 映射存在，两锁都进必需运行集合，清单仍自洽。"""
    assert rc.OPTIONAL_LOCKS == {"similarity": SIM_LOCK}, \
        f"可选锁映射漂移：{rc.OPTIONAL_LOCKS!r}"
    required = {str(r) for r in rc.REQUIRED_RUNTIME_FILES}
    assert CORE_LOCK in required, "core 锁掉出必需集合"
    assert SIM_LOCK in required, "similarity 锁未登记进必需集合"
    assert (PKG_ROOT / SIM_LOCK).is_file(), "similarity 锁文件不在包里"
    rc.validate_contract()  # 清单自洽门：子集/入口/场景关系全成立才算过


def test_missing_optional_lock_disclosure(tmp_path):
    """缺相似度锁的树：报告必须点名缺哪把锁、给安装指引，不许假绿。"""
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    shutil.copyfile(PKG_ROOT / CORE_LOCK, candidate / CORE_LOCK)
    report = rc.capability_report(root=candidate)
    assert report["lock_present"] is True, "core 锁在场却报缺失"
    entries = {e["group"]: e for e in report["optional_locks"]}
    assert "similarity" in entries, "报告丢了 similarity 组"
    missing = entries["similarity"]
    assert missing["lock_file"] == SIM_LOCK
    assert missing["present"] is False, "缺锁却报在场：假绿"
    assert SIM_LOCK in missing["install_hint"], "缺锁指引没点名锁文件"
    assert "pip install" in missing["install_hint"], "缺锁指引没给安装命令"

    full = rc.capability_report(root=PKG_ROOT)
    full_entries = {e["group"]: e for e in full["optional_locks"]}
    assert full_entries["similarity"]["present"] is True, "完整包的锁报缺失"
    assert full["lock_present"] is True


def test_guidance_for_import():
    """import 名 → 缺依赖指引：可选组点名锁与命令，核心组不误报。"""
    for name in SIM_IMPORTS:
        hint = rc.guidance_for_import(name)
        assert hint, f"可选依赖无指引：{name}"
        assert SIM_LOCK in hint, f"指引没点名锁文件：{name}"
        assert "pip install" in hint, f"指引没给安装命令：{name}"
        dist = rc.EXTERNAL_DEPENDENCIES[name]["distribution"].lower()
        assert dist in hint.lower(), f"指引没点名缺的发行包：{name}"
    for name in CORE_IMPORTS:
        assert rc.guidance_for_import(name) is None, \
            f"核心依赖被误报成缺可选件：{name}"
