#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实 API 抽样手册的机械可执行性：命令块里的每个 --flag 必须是目标脚本真实存在的参数。

手册是"发给别人照做"的交付物，最容易烂的地方是脚本参数改名后文档没跟上（用户在真实环境里
才发现）。这里把手册代码块里的调用逐条抽出来，对着各自脚本的 `--help` 校验 `--flag` 名字，
顺带确认引用的夹具路径存在——不需要密钥、不发起任何请求。
"""
import re
from pathlib import Path

import unittest

from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MANUAL = REPO_ROOT / "docs" / "验收" / "v2.14.0-真实API抽样.md"

# 手册里允许出现的调用目标（脚本路径 → 用于取 help 的命令片段）
HELP_TIMEOUT = 60


def _blocks() -> list[str]:
    text = MANUAL.read_text(encoding="utf-8")
    return re.findall(r"```(?:powershell|bash)\n(.*?)```", text, re.S)


def _help_text(script_rel: str, subcmd: str = "") -> str:
    script = REPO_ROOT / script_rel
    if not script.is_file():
        raise AssertionError(f"手册引用的脚本不存在: {script_rel}")
    argv = [str(script)] + ([subcmd] if subcmd else []) + ["--help"]
    r = run_py(argv, timeout=HELP_TIMEOUT)
    if r.returncode != 0:
        raise AssertionError(f"{script_rel} {subcmd} --help 失败: {r.stdout[-200:]}{r.stderr[-200:]}")
    return r.stdout + r.stderr


def _invocations() -> list[tuple[str, str, list[str], str]]:
    """返回 (script_rel, subcmd, flags, 原始行)。"""
    out = []
    pat = re.compile(r"python3?\s+(?:-m\s+)?(?P<script>[\w\u4e00-\u9fff./-]+\.py)(?P<rest>[^\n]*)")
    for block in _blocks():
        for raw in block.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "$c.py" in line or "${c}.py" in line:
                continue  # 变量拼出的脚本名（PowerShell foreach）不在此机械校验范围
            m = pat.search(line)
            if not m:
                continue
            script = m.group("script")
            rest = m.group("rest")
            flags = re.findall(r"(?<![\w-])(--[A-Za-z][\w-]*)", rest)
            subcmd = ""
            if script.endswith("maintenance.py"):
                for cand in ("init", "verify", "status", "backup", "restore", "migrate"):
                    if re.search(rf"(?<![\w-]){cand}(?![\w-])", rest):
                        subcmd = cand
                        break
                if not subcmd:
                    continue  # 只写 --help 的子命令探测行不校验
            out.append((script, subcmd, flags, line))
    return out


class TestManualCommands(unittest.TestCase):
    def test_manual_exists_and_has_blocks(self):
        self.assertTrue(MANUAL.is_file(), f"手册缺失: {MANUAL}")
        self.assertGreaterEqual(len(_blocks()), 4, "手册代码块数量异常（PowerShell/bash 各两段以上）")

    def test_every_flag_exists_in_target_script(self):
        invocations = _invocations()
        self.assertGreaterEqual(len(invocations), 20, f"抽取到的调用太少，正则可能失效: {len(invocations)}")
        help_cache: dict[tuple[str, str], str] = {}
        bad = []
        for script, subcmd, flags, line in invocations:
            key = (script, subcmd)
            if key not in help_cache:
                help_cache[key] = _help_text(script, subcmd)
            help_text = help_cache[key]
            for flag in flags:
                if flag not in help_text:
                    bad.append(f"{script}{' ' + subcmd if subcmd else ''} 无 {flag} ← {line[:90]}")
        self.assertEqual(bad, [], "手册里的参数在脚本 --help 中不存在:\n  " + "\n  ".join(bad))

    def test_manual_references_existing_fixture(self):
        text = MANUAL.read_text(encoding="utf-8")
        fixture = REPO_ROOT / "测试" / "fixtures" / "live_smoke" / "短文本.md"
        self.assertTrue(fixture.is_file(), f"手册引用的夹具不存在: {fixture}")
        self.assertIn("测试/fixtures/live_smoke/短文本.md", text)
        self.assertIn("24faa5331ac585d582843d7480b8835809b0f4f385345751cdcdec7c1f0c5190", text,
                      "手册里的夹具 SHA-256 与文件名不符（改了夹具必须同步手册）")

    def test_manual_has_no_recursive_delete_of_fixed_paths(self):
        # 固定路径递归删除可能删掉用户数据；手册必须只用带时间戳的唯一运行目录
        for block in _blocks():
            for raw in block.splitlines():
                line = raw.strip()
                if line.startswith("#"):
                    continue
                self.assertNotIn("rm -rf", line, f"手册仍在递归删除: {line}")
                self.assertNotIn("Remove-Item -Recurse", line, f"手册仍在递归删除: {line}")
        text = MANUAL.read_text(encoding="utf-8")
        self.assertIn("$RUN", text, "手册应使用唯一运行目录变量 $RUN")

    def test_human_review_table_stays_blank(self):
        # 十项人审必须留空待人工判定，AI 不得预填（只看「期望核对表」那一节）
        text = MANUAL.read_text(encoding="utf-8")
        section = text.split("## 四、期望核对表")[1].split("## 五、执行记录")[0]
        rows = [raw for raw in section.splitlines() if re.match(r"^\|\s*\d+\s*\|", raw)]
        self.assertEqual(len(rows), 10, f"人审表应恰好 10 项，实际 {len(rows)}")
        for raw in rows:
            cells = [c.strip() for c in raw.strip().strip("|").split("|")]
            no = cells[0]
            self.assertEqual(len(cells), 5, f"人审表第 {no} 项列数异常（应 5 列）: {raw[:80]}")
            self.assertEqual((cells[3], cells[4]), ("", ""),
                             f"第 {no} 项人审「判定/证据位置」被预填: {cells[3]}｜{cells[4]}")


if __name__ == "__main__":
    unittest.main()
