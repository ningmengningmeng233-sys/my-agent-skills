#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 8 工单 8.3：中文长路径回归（规格 15.6）。

Windows 上中文 + 长路径是真实事故来源：文件能建却读不到、原子替换失败、子进程编码崩掉。
本文件在**深且全中文**的目录下跑三条真实命令链（核心状态、视觉状态、迁移只读扫描），
确认全程可读写、无控制字符、无编码异常。
"""
import json
import os
import sys
from pathlib import Path

import pytest
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEEP_SEGMENTS = (
    "资产提取长路径回归",
    "第一集剧本与分镜合集",
    "人物设定与服装参考素材库",
    "二〇二六年九月十八日交付批次",
    "深层嵌套目录用于验证中文与长度上限",
)


def _deep_lib(tmp_path: Path) -> Path:
    """构造一个「中文 + 深」的资产库根目录（Windows 上路径长度显著超过 100 字符）。"""
    lib = tmp_path.joinpath(*DEEP_SEGMENTS) / "资产反推_长路径测试_第01集_v2.16.0-rc.1"
    (lib / ".process").mkdir(parents=True)
    (lib / "原文段" / "测试-作者").mkdir(parents=True)
    (lib / "原文段" / "测试-作者" / "段01.md").write_text(
        "第一场 茶楼\n老罗把缺角粗陶碗放在桌上。\n", encoding="utf-8")
    index = {
        "schema_version": 1, "source_fingerprint": "sha256:long-path",
        "assets": [{
            "asset_id": "CH_老罗", "stable_key": "CHR:00001:1", "type": "人物",
            "name": "老罗", "priority": "P1", "fact_level": "剧本事实",
            "evidence": [{"segment": "段01.md", "chapter": "第一场", "line": "00001",
                          "quote": "老罗把缺角粗陶碗放在桌上"}],
            "dependencies": [], "relations": [], "status": "CONFIRMED",
        }],
    }
    (lib / ".process" / "资产索引.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8")
    return lib


def _run(args, *, env=None):
    base = {**os.environ, "ASSET_WORKERS": "4"}
    base.pop("GEMINI_API_KEY", None)
    base.update(env or {})
    return run_py([*[str(a) for a in args]], cwd=str(REPO_ROOT), env=base)


def test_long_cjk_path_is_really_long(tmp_path):
    lib = _deep_lib(tmp_path)
    assert len(str(lib)) > 100, f"夹具路径不够长：{len(str(lib))}"
    assert all(seg in str(lib) for seg in DEEP_SEGMENTS)
    assert lib.is_dir()


def test_core_status_works_under_long_cjk_path(tmp_path):
    lib = _deep_lib(tmp_path)
    proc = _run([REPO_ROOT / "scripts" / "maintenance.py", "status",
                 "--lib", lib, "--json"])
    payload = proc.stdout + proc.stderr
    assert proc.returncode == 0, payload
    assert "Traceback" not in payload, payload
    assert any(ch.isprintable() or ch in "\n\r\t" for ch in payload)
    assert not [ch for ch in payload if ord(ch) < 32 and ch not in "\n\r\t"], "输出含控制字符"


def test_visual_status_works_under_long_cjk_path(tmp_path):
    lib = _deep_lib(tmp_path)
    proc = _run([REPO_ROOT / "scripts" / "visual_production.py", "status",
                 "--lib", lib, "--json"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["enabled"] is False, "没有计划时应报「未启用」而不是崩掉"
    assert payload["lib"] == str(lib)


def test_migration_scan_is_read_only_under_long_cjk_path(tmp_path):
    from scripts.migrate_visual_library import scan_library

    lib = _deep_lib(tmp_path)
    (lib / "视觉资产" / "人物" / "老罗").mkdir(parents=True)
    from PIL import Image

    import io

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buffer, format="PNG")
    (lib / "视觉资产" / "人物" / "老罗" / "老罗_EP01正脸身份近景_3x4.png").write_bytes(
        buffer.getvalue())
    proposal = scan_library(lib)
    assert proposal["counts"]["images"] == 1
    assert proposal["library_label"].startswith("资产反推_长路径测试")


def test_planner_writes_and_reloads_plan_under_long_cjk_path(tmp_path):
    from scripts.engine.style_registry import resolve_style_record
    from scripts.engine.visual_planner import plan_visual_tasks
    from scripts.engine.visual_schema import load_visual_plan, write_visual_plan

    lib = _deep_lib(tmp_path)
    index = json.loads((lib / ".process" / "资产索引.json").read_text(encoding="utf-8"))
    plan = plan_visual_tasks(index, resolve_style_record("仙侠国风电影CG"))
    target = lib / "视觉资产" / "计划.toml"
    write_visual_plan(plan, target)
    reloaded = load_visual_plan(target)
    assert len(reloaded.tasks) == len(plan.tasks) > 0
    assert not [ch for ch in target.read_text(encoding="utf-8")
                if ord(ch) < 32 and ch not in "\n\r\t"], "计划文件不得含控制字符"


@pytest.mark.parametrize("command", ("status", "configure-visual"))
def test_cli_help_survives_cjk_cwd(tmp_path, command):
    """在中文目录下执行 CLI 不得因编码崩掉（子进程 stdout 一律 UTF-8）。"""
    lib = _deep_lib(tmp_path)
    proc = _run([REPO_ROOT / "scripts" / "maintenance.py", command, "--help"])
    assert proc.returncode == 0
    assert "usage" in (proc.stdout + proc.stderr).lower()
    assert lib.is_dir()
