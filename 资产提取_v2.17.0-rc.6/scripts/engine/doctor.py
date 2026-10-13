# -*- coding: utf-8 -*-
"""统一体检（加速改法工单 T10 / 设计方案 §3.6）。

- 每项返回 `{name, ok, detail, fix}`；失败项必须给修法。
- **只读探测，绝不发收费调用**；密钥只报「已配置/未配置」，绝不显示值。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent.parent

_REQUIRED_MODULES = ("requests", "dotenv", "docx", "ebooklib", "bs4", "PIL")


def _check_python() -> dict[str, Any]:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 10)
    return {"name": "Python 版本", "ok": ok,
            "detail": f"{v.major}.{v.minor}.{v.micro}",
            "fix": "" if ok else "请安装 Python 3.10 或更高版本后重试"}


def _check_dependencies() -> dict[str, Any]:
    missing = []
    for mod in _REQUIRED_MODULES:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    ok = not missing
    return {"name": "依赖包", "ok": ok,
            "detail": "齐全（" + ", ".join(_REQUIRED_MODULES) + "）" if ok else f"缺少: {', '.join(missing)}",
            "fix": "" if ok else "pip install -r requirements.txt"}


def _config_values() -> tuple[dict[str, str], Path | None]:
    """体检看到的配置 = 运行时看到的配置（engine.common 同一来源，只读不写进程环境）。"""
    from engine.common import env_values
    return env_values()


def _check_env_config() -> dict[str, Any]:
    values, path = _config_values()
    if values.get("OPENAI_API_KEY"):
        where = "进程环境" if os.environ.get("OPENAI_API_KEY") else str(path)
        return {"name": ".env 配置", "ok": True, "detail": f"已配置（{where}，密钥不显示）", "fix": ""}
    return {"name": ".env 配置", "ok": False, "detail": "未配置 OPENAI_API_KEY",
            "fix": "复制 .env.example 为 ~/.asset-extract/.env 并填写文本三件套（KEY/URL/MODEL）"}


def _check_node_pool() -> dict[str, Any]:
    from engine import llm_pool
    values, _ = _config_values()
    nodes = llm_pool.load_nodes(env=values)
    if nodes:
        usable = len(nodes)
        return {"name": "节点池", "ok": usable > 0, "detail": f"{usable} 个节点（llm_nodes.json，按序消耗）",
                "fix": "" if usable else "检查 llm_nodes.json 的 nodes 数组"}
    one = llm_pool.single_node_from_env(values)
    if one is not None:
        return {"name": "节点池", "ok": True, "detail": "无池配置，单端点回落（SUMMARY_API_URL 三件套）", "fix": ""}
    return {"name": "节点池", "ok": False, "detail": "无 llm_nodes.json 且 SUMMARY_API_URL 未配置",
            "fix": "填 ~/.asset-extract/.env 的文本三件套，或建包外 llm_nodes.json（见 llm_nodes.json.example）"}


def _check_style_pack() -> dict[str, Any]:
    styles_dir = SKILL_DIR / "references" / "styles"
    if not styles_dir.is_dir():
        return {"name": "权威风格包", "ok": False, "detail": f"缺少目录 {styles_dir}",
                "fix": "重新解压技能包（references/styles 不可缺）"}
    packs = sorted(p.name for p in styles_dir.iterdir() if p.is_dir())
    if not packs:
        return {"name": "权威风格包", "ok": False, "detail": "references/styles 为空",
                "fix": "重新解压技能包或执行 install_stylepack"}
    detail = f"{len(packs)} 个风格包: {', '.join(packs[:4])}" + ("…" if len(packs) > 4 else "")
    return {"name": "权威风格包", "ok": True, "detail": detail, "fix": ""}


def _check_lib_writable(lib) -> dict[str, Any]:
    lib = Path(lib)
    if not lib.is_dir():
        return {"name": "资产库可写", "ok": False, "detail": f"目录不存在: {lib}",
                "fix": "python scripts/maintenance.py init --lib <资产库> --input-source <原文> --genre <题材> --style <画风>"}
    probe = lib / ".process"
    try:
        probe.mkdir(parents=True, exist_ok=True)
        target = probe / ".doctor-probe"
        target.write_text("ok", encoding="utf-8")
        target.unlink()
        return {"name": "资产库可写", "ok": True, "detail": str(lib), "fix": ""}
    except OSError as e:
        return {"name": "资产库可写", "ok": False, "detail": f"不可写: {e}",
                "fix": "检查目录权限，或换一个可写路径重新 init"}


def run_checks(lib) -> list[dict[str, Any]]:
    """统一体检：只读、零收费；返回逐项 {name, ok, detail, fix}。"""
    checks = [_check_python(), _check_dependencies(), _check_env_config(),
              _check_node_pool(), _check_style_pack(), _check_lib_writable(lib)]
    return checks
