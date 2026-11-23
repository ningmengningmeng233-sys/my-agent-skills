#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单04-Task6：真实 Gemini 抽样手册的机械可执行性与"不代填"守卫。

只做机械校验，不需要密钥、不发起任何请求：
- 手册代码块里每个 `--flag` 必须是目标脚本真实存在的参数；
- 引用的夹具路径真实存在且可生成视觉计划；
- 手册不含真实密钥、不含递归删除固定路径的命令；
- 十项人工表必须保持空白（判定列不得被自动化代填）。
"""
import json
import re
import shutil
from pathlib import Path
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MANUAL = REPO_ROOT / "docs" / "验收" / "v2.15.0-Gemini视觉抽样.md"
FIXTURE = REPO_ROOT / "测试" / "fixtures" / "live_visual" / "最小资产库"
HELP_TIMEOUT = 60
SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
)
DESTRUCTIVE = ("rm -rf", "Remove-Item -Recurse", "rd /s", "del /f", "shutil.rmtree")


def _text() -> str:
    assert MANUAL.is_file(), f"缺少手册 {MANUAL}"
    return MANUAL.read_text(encoding="utf-8")


def _blocks() -> list[str]:
    return re.findall(r"```(?:powershell|bash)\n(.*?)```", _text(), re.S)


def _help_text(script_rel: str, subcmd: str = "") -> str:
    script = REPO_ROOT / script_rel
    if not script.is_file():
        raise AssertionError(f"手册引用的脚本不存在: {script_rel}")
    argv = [str(script)] + ([subcmd] if subcmd else []) + ["--help"]
    r = run_py(argv, timeout=HELP_TIMEOUT)
    if r.returncode != 0:
        raise AssertionError(f"{script_rel} --help 失败: {r.stdout[-200:]}{r.stderr[-200:]}")
    return r.stdout + r.stderr


def _invocations() -> list[tuple[str, str, list[str], str]]:
    """返回 (脚本相对路径, 子命令, flags, 原始行)。"""
    out: list[tuple[str, str, list[str], str]] = []
    pat = re.compile(r"python3?\s+(?:-m\s+)?(?P<script>[\w\u4e00-\u9fff./\\-]+\.py)(?P<rest>[^\n]*)")
    for block in _blocks():
        for raw in block.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("Write-Host"):
                continue
            if "$" in line.split(".py")[0]:
                continue  # 变量拼出的脚本名不在机械校验范围
            m = pat.search(line)
            if not m:
                continue
            script = m.group("script").replace("\\", "/")
            rest = m.group("rest")
            subcmd = ""
            sub = re.match(r"\s+([a-z][a-z-]+)\s", rest)
            if sub and not sub.group(1).startswith("-"):
                subcmd = sub.group(1)
            flags = [f for f in re.findall(r"(?<![\w-])(--[A-Za-z][A-Za-z0-9-]*)", rest)]
            out.append((script, subcmd, flags, line))
    return out


def test_manual_exists():
    assert MANUAL.is_file()


def test_every_flag_exists_in_target_help():
    problems = []
    for script, subcmd, flags, line in _invocations():
        if not flags:
            continue
        help_text = _help_text(script, subcmd)
        for flag in flags:
            if flag not in help_text:
                problems.append(f"{script} {subcmd}：{flag}（来自: {line[:70]}）")
    assert problems == [], "手册参数与脚本 --help 不一致:\n  " + "\n  ".join(problems)


def test_manual_invokes_real_scripts():
    for script, _sub, _flags, _line in _invocations():
        assert (REPO_ROOT / script).is_file(), script


def test_manual_has_no_secret_literals():
    """手册不得含真实密钥字面值；任何密钥赋值必须是占位符形式。"""
    text = _text()
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(text), "手册含密钥字面值"
    for match in re.finditer(r"(GEMINI_API_KEY|OPENAI_API_KEY|GOOGLE_API_KEY)\s*=", text):
        tail = text[match.end():].lstrip()
        assert tail.startswith(("<", '"<')), f"密钥赋值必须是占位符：{text[match.start():match.end() + 20]!r}"


def test_manual_forbids_secret_in_artifacts():
    text = _text()
    assert "密钥只存在于" in text or "密钥" in text
    assert "不要" in text and "密钥" in text


def test_manual_forbids_destructive_cleanup():
    text = _text()
    for pattern in DESTRUCTIVE:
        assert pattern not in text, f"手册不得包含递归删除：{pattern}"


def test_manual_uses_unique_run_dir():
    text = _text()
    assert "唯一运行目录" in text
    assert "Get-Date -Format" in text
    assert "asset_visual_live-" in text


def test_manual_documents_cost_stop_and_request_cap():
    text = _text()
    assert "费用停止点" in text
    assert "36 次调用" in text or "36" in text
    assert "最多 3 轮" in text or "最多自动返工 3 轮" in text


def test_manual_documents_recovery_and_result_paths():
    text = _text()
    assert "恢复方法" in text
    assert "视觉交付清单.json" in text
    assert "attempt-NN" in text or "attempt-01" in text


def test_manual_lists_laoluo_six_tasks():
    text = _text()
    for task in ("CH_老罗-正脸-v01", "CH_老罗-后脑-v01", "CH_老罗-服装独立正面-v01",
                 "CH_老罗-服装独立背面-v01", "CH_老罗-人物全身正面-v01",
                 "CH_老罗-人物全身背面-v01"):
        assert task in text, task


def test_manual_keeps_human_table_blank():
    """十项人工表的判定列必须逐行空白，不得被自动化代填。"""
    text = _text()
    rows = [line for line in text.splitlines()
            if line.startswith("| ") and "|" in line and re.match(r"^\|\s*\d+\s*\|", line)]
    assert len(rows) >= 10, f"人工表行数不足：{len(rows)}"
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert len(cells) >= 5, row
        assert cells[2] == "", f"判定列必须空白：{row}"
        assert cells[3] == "", f"证据列必须空白：{row}"


def test_manual_states_rc_not_formal_when_unverified():
    text = _text()
    assert "候选版" in text or "rc" in text
    assert "不得称正式版" in text or "不得把空白视为通过" in text


# --- 夹具 ------------------------------------------------------------------------

def test_fixture_exists_and_is_self_contained():
    assert FIXTURE.is_dir()
    for rel in (".process/资产索引.json", ".start-state.json", "反推_最终.md"):
        assert (FIXTURE / rel).is_file(), rel
    index = json.loads((FIXTURE / ".process" / "资产索引.json").read_text(encoding="utf-8"))
    ids = {a["asset_id"] for a in index["assets"]}
    assert "CH_老罗" in ids
    types = {a["type"] for a in index["assets"]}
    assert {"人物", "场景", "道具", "服装"} <= types


def test_fixture_has_no_absolute_paths_or_secrets():
    for path in FIXTURE.rglob("*"):
        if not path.is_file() or path.suffix not in (".json", ".md", ".toml"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert ":" + chr(92) + "Users" + chr(92) not in text, path.name
        for pattern in SECRET_PATTERNS:
            assert not pattern.search(text), path.name


def test_fixture_generates_visual_plan(tmp_path):
    lib = tmp_path / "资产库"
    shutil.copytree(FIXTURE, lib)
    r = run_py(
            [str(REPO_ROOT / "scripts" / "create_visual_plan.py"), "--lib", str(lib), "--style", "仙侠国风电影CG"],
            cwd=str(REPO_ROOT),
            timeout=300,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    plan = (lib / "视觉资产" / "计划.toml").read_text(encoding="utf-8")
    for task in ("CH_老罗-正脸-v01", "CH_老罗-后脑-v01", "CH_老罗-服装独立正面-v01",
                 "CH_老罗-服装独立背面-v01", "CH_老罗-人物全身正面-v01",
                 "CH_老罗-人物全身背面-v01"):
        assert task in plan, task


def test_fixture_copy_is_pristine():
    """夹具必须先复制再操作：文档与测试都不得改动夹具本体。"""
    readme = (FIXTURE / "README.md").read_text(encoding="utf-8")
    assert "复制" in readme and "不要在夹具内直接跑生成" in readme


def test_manual_is_referenced_from_docs_index():
    changelog = (REPO_ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "v2.15.0" in changelog
