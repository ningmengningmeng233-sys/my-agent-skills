#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Python 3.10 兼容契约（缺陷 F33）。

实测根因：`pyproject.toml` 声明 `requires-python = ">=3.10"`，但 `tomllib` 是 **3.11** 才进标准库。
8 个模块写了 `try: import tomllib / except: import tomli as tomllib` 的回退，而 `tomli`
**既不在 requirements.txt 也不在 pyproject.toml**。在真的 3.10 上，其中 5 处位于模块顶层，
import 即抛 ModuleNotFoundError；因为 `maintenance.py` 顶层要读 `engine.project_settings`，
连 `maintenance.py status` 都会退出码 1——**整个技能起不来**，不是某条支线坏。

补上依赖声明后，真 3.10.21 又暴露出第二层问题：**测试码另有 5 处裸写 `import tomllib`、
根本没有回退**（`test_static_video_separation` / `test_stylepack_sync` ×2 /
`test_visual_schema` / `test_docs_alignment_rc2`），所以运行时通了但测试套件仍 5 红。
现全部改走 `engine.common.toml_module()`，并加守卫禁止任何文件"引用 tomllib 却不处理 tomli"。

本文件钉四件事：
① 有回退点就必须有条件依赖声明（声明与实际用法不得脱钩，这是本仓反复出现的"多份口径"）；
② 回退机制本身是通的（挡掉 tomllib、保证 tomli 可用，模块必须 import 成功）——
   证明缺的只是声明，不是代码；
③ 条件标记必须写成"仅 <3.11"：3.11+ 装 tomli 是多余依赖，会污染干净环境；
④ 全仓不得有"引用 tomllib 却不处理 tomli"的文件（F33 的根治条款）。
"""
import ast
import os
import re
from pathlib import Path

import pytest
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"

# 5 处**模块顶层**回退：import 即崩的那些，必须逐个点名（函数级的崩在调用时，另计）
MODULE_LEVEL_FALLBACKS = (
    "engine/project_settings.py",
    "engine/style_registry.py",
    "engine/visual_io.py",
    "engine/image_qc.py",
    "build_release.py",
)


def _fallback_sites() -> list[str]:
    """全仓哪些 .py 写了 `import tomli as tomllib` 回退（相对 scripts/ 的路径）。"""
    out = []
    for path in sorted(SCRIPTS.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if re.search(r"^\s*import\s+tomli\s+as\s+tomllib", path.read_text(encoding="utf-8"), re.M):
            out.append(path.relative_to(SCRIPTS).as_posix())
    return out


def _is_module_level(rel: str) -> bool:
    """该文件的 tomli 回退是否在模块顶层（而不是某个函数里）。"""
    tree = ast.parse((SCRIPTS / rel).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Try):
            for stmt in node.body + [h for hs in node.handlers for h in hs.body]:
                if isinstance(stmt, ast.Import):
                    if any(a.name == "tomli" for a in stmt.names):
                        return True
    return False


def _requirement_lines(name: str) -> list[str]:
    out = []
    for f in (REPO_ROOT / "requirements.txt", REPO_ROOT / "pyproject.toml"):
        if f.is_file():
            for line in f.read_text(encoding="utf-8").splitlines():
                if name in line.lower() and not line.strip().startswith("#"):
                    out.append(line.strip())
    return out


def test_every_module_level_tomli_fallback_is_named():
    """回退点必须被逐个登记：新增一处顶层回退却没加进名单，本测试就该红。

    这是"声明与实际用法脱钩"的入口——上一版就是因为没人数这 5 处，才漏了整条依赖。
    """
    sites = _fallback_sites()
    module_level = sorted(p for p in sites if _is_module_level(p))
    assert module_level == sorted(MODULE_LEVEL_FALLBACKS), (
        f"模块顶层 tomli 回退点与名单不符：实测 {module_level}")
    assert sites, "scripts/ 下已无 tomli 回退；若确认放弃 3.10 支持，" \
                  "请同时把 requires-python 提到 >=3.11 并删除本文件"


@pytest.mark.parametrize("rel", MODULE_LEVEL_FALLBACKS)
def test_fallback_path_works_when_tomli_present(rel):
    """挡掉 tomllib → 该模块必须仍能靠 tomli 起来。

    证明**代码路径本身没坏**，缺的只是"3.10 上 tomli 从哪来"这一件事。
    真实现优先取 tomllib（3.11+），取不到就用已装的 tomli（真 3.10 环境）；
    只有在两者都没有时才桩一个委托出去——本函数在 3.10 与 3.12 上都必须是绿的。
    """
    runner = r'''
import sys, types, importlib
try:
    _real = importlib.import_module("tomllib")
except ModuleNotFoundError:
    _real = importlib.import_module("tomli")

class _Block:
    def find_spec(self, name, path=None, target=None):
        if name == "tomllib":
            raise ModuleNotFoundError("No module named 'tomllib'（模拟 3.10）")
        return None

sys.modules.pop("tomllib", None)
sys.meta_path.insert(0, _Block())
try:
    importlib.import_module("tomli")
except ModuleNotFoundError:
    _stub = types.ModuleType("tomli")
    _stub.load = _real.load
    _stub.loads = _real.loads
    sys.modules["tomli"] = _stub

mod = importlib.import_module(sys.argv[1])
print("OK", mod.__name__)
'''
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(SCRIPTS), str(SCRIPTS / "engine")])
    r = run_py(
            ["-c", runner, rel.replace("/", ".").removesuffix(".py")],
            env=env,
            cwd=str(REPO_ROOT),
    )
    assert r.returncode == 0, (
        f"{rel} 在「tomllib 被挡 + tomli 可用」下 import 失败：{r.stderr[-600:]}")
    assert r.stdout.startswith("OK"), r.stdout + r.stderr


def test_no_file_uses_tomllib_without_handling_tomli():
    """守卫：任何引用 `tomllib` 的 .py 必须**同时**处理 `tomli`，否则判红。

    这条才是 F33 的根治。上一版的事实是：生产码 8 处各自写了回退，测试码另有 5 处
    **裸写 `import tomllib` 根本没有回退**——所以"支持 3.10"从来没成立过（真 3.10 上
    测试 5 红），而且没有任何一处检查会发现这件事。
    新代码请改用 `engine.common.toml_module()`，那是唯一一份兼容口径。
    """
    offenders = []
    for base in (SCRIPTS, REPO_ROOT / "测试"):
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if "tomllib" in text and "tomli" not in text.replace("tomllib", ""):
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert not offenders, (
        "以下文件引用了 tomllib 却没处理 tomli，在 Python 3.10 上会 ModuleNotFoundError：\n  "
        + "\n  ".join(offenders) +
        "\n改法：from engine.common import toml_module → toml_module().loads(text)")


def test_tomli_declared_as_conditional_dependency():
    """有回退点就必须有条件依赖声明——本测试在修复前必须是红的（缺陷 F33 的证据）。"""
    sites = _fallback_sites()
    if not sites:
        pytest.skip("无 tomli 回退点")
    declared = [l for l in _requirement_lines("tomli") if not l.startswith("#")]
    assert declared, (
        f"{len(sites)} 个模块写了 `import tomli as tomllib` 回退，但 requirements.txt / "
        f"pyproject.toml 都没声明 tomli —— 在真的 Python 3.10 上这些模块 import 即崩，"
        f"而 maintenance.py 顶层就依赖它们，整个技能起不来。声明清单：{declared}")
    joined = " ".join(declared).lower()
    assert "python_version" in joined and "3.11" in joined, (
        f"tomli 必须写成条件依赖（仅 <3.11 才装），否则 3.11+ 环境多装一个无用依赖：{declared}")
    assert not re.search(r"tomli\s*$", "\n".join(declared), re.M), (
        f"不允许无条件声明 tomli（3.11+ 自带 tomllib）：{declared}")
