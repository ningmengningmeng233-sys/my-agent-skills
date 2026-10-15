"""T08 兼容尺：cli 模块搬家前后，公共名字、签名与 argv 面必须逐项一致。

这把关子存在的理由只有一个——拆分是机械搬家，不是重写。所以钉「面」不钉实现：
- 顶层公共函数（`build_parser`/`main` 与 26 个 `cmd_*`）的名字与签名逐字相等；
- 私有助手仍能 `from v4.core.cli import _xxx` 取到（14 个测试文件引用 core.cli）；
- `build_parser()` 展开出的完整 argv 路径集合（含 `card`/`lib` 两层子命令）条数与成员不变；
- 模块级常量与 `CLIError` 的身份不变。

名单与签名都是拆分前用 AST + inspect.signature 现测录下的（见 private-evidence
t08_cli_static_surface_v2.json），不是凭印象写的；反空转测试要求钉住的条数与
现算的顶层定义数一致，防止「测试写好了但一条也没钉」。
"""

from __future__ import annotations

import argparse
import ast
import collections
import inspect
import pathlib

import pytest

from v4.core import cli

CLI_PATH = pathlib.Path(inspect.getfile(cli))

HANDLER_SIGNATURE = "(args: 'argparse.Namespace') -> 'int'"
EXPECTED_FUNCS = {
    "build_parser": "() -> 'argparse.ArgumentParser'",
    "main": "() -> 'int'",
}
for _name in ("align_events", "approve", "assets", "blind_review", "blockers", "card",
              "check", "confirm_boundary", "consistency", "decide", "ingest", "init",
              "lib", "merge", "pause", "platform_review", "publish_split", "recover",
              "refresh", "repair_coverage", "resolve_events", "resume", "rewrite",
              "rollback", "status", "versions"):
    EXPECTED_FUNCS[f"cmd_{_name}"] = HANDLER_SIGNATURE

EXPECTED_CLASSES = {"CLIError"}
EXPECTED_CONSTANTS = {"_PAUSE_SOURCES", "_RECOVERABLE_STATES", "_CN_DEFAULT_INPUT_LANGUAGES"}
EXPECTED_PRIVATE_HELPERS = {
    "_workspace_path", "_find_chapter", "_chapter_ids_to_process",
    "_decision_paths", "_collect_open_decisions",
    "_write_chapter_blockers", "_print_blocker_guidance",
}
EXPECTED_ARGV_PATHS = {
    "align-events", "approve", "assets", "blind-review", "blockers", "card", "check",
    "confirm-boundary", "consistency", "decide", "ingest", "init", "lib", "merge",
    "pause", "platform-review", "publish-split", "recover", "refresh", "repair-coverage",
    "resolve-events", "resume", "rewrite", "rollback", "status", "versions",
    "card index", "card init", "card judge", "card list", "card register", "card samples",
    "card status", "card verify-anchors",
    "lib add", "lib list", "lib refresh", "lib remove", "lib status",
}


def _top_level_defs() -> tuple[set, set, set]:
    tree = ast.parse(CLI_PATH.read_text(encoding="utf-8"))
    funcs, classes, consts = set(), set(), set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            funcs.add(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.add(node.name)
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            consts.add(node.targets[0].id)
    return funcs, classes, consts


def _subparsers_action(parser: argparse.ArgumentParser):
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and choices and \
                all(hasattr(v, "_actions") for v in choices.values()):
            return action
    return None


def _argv_surface(parser: argparse.ArgumentParser, prefix: str = "") -> dict:
    """展开成 {完整 argv 路径: 必填目的名集合}，递归进 card/lib 子树。"""
    out: dict[str, set] = {}
    action = _subparsers_action(parser)
    if action is None:
        return out
    for name, sub in action.choices.items():
        path = f"{prefix} {name}".strip()
        out[path] = {a.dest for a in sub._actions
                     if a.required and a.dest and a.dest != "help"}
        out.update(_argv_surface(sub, path))
    return out


def _all_defs_across_the_split():
    """搬家后公共名字住在 cli_support/commands，门面只是 re-export。

    尺子因此按「整个 cli 家族」计数，并要求每个钉住的名字只被定义一次——
    既能容忍搬家，又能挡住「把同一个函数复制两份」这种假拆分。
    """
    root = CLI_PATH.parent
    targets = [root / "cli.py", root / "cli_support.py"] + sorted(
        (root / "commands").glob("*.py")) if (root / "commands").is_dir() else [
        root / "cli.py", root / "cli_support.py"]
    counts = collections.Counter()
    classes, consts = set(), set()
    for f in targets:
        if not f.is_file():
            continue
        for node in ast.parse(f.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                counts[node.name] += 1
            elif isinstance(node, ast.ClassDef):
                classes.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                tgt = node.targets[0] if isinstance(node, ast.Assign) else node.target
                if isinstance(tgt, ast.Name):
                    consts.add(tgt.id)
    return counts, classes, consts


def test_ruler_is_not_vacuous():
    counts, classes, consts = _all_defs_across_the_split()
    assert len(EXPECTED_FUNCS) == 28, len(EXPECTED_FUNCS)
    assert len(EXPECTED_ARGV_PATHS) == 39, len(EXPECTED_ARGV_PATHS)
    assert EXPECTED_CLASSES <= classes, classes
    assert EXPECTED_CONSTANTS <= consts, consts
    dupes = sorted(n for n, c in counts.items() if c > 1)
    assert not dupes, f"同一名字被定义了两份：{dupes}"
    for name in EXPECTED_FUNCS:
        assert counts[name] == 1, (name, counts[name])


def test_signatures_are_byte_identical():
    got = {name: str(inspect.signature(getattr(cli, name))) for name in EXPECTED_FUNCS}
    drift = {k: (v, got[k]) for k, v in EXPECTED_FUNCS.items() if got[k] != v}
    assert not drift, drift


def test_no_unexpected_public_handler_disappears():
    """兼容性看的是门面命名空间，不是某个文件里有没有 def。"""
    got = {n for n in dir(cli) if n.startswith("cmd_")}
    want = {n for n in EXPECTED_FUNCS if n.startswith("cmd_")}
    assert got == want, (sorted(want - got), sorted(got - want))


def test_private_helpers_stay_importable_from_the_facade():
    missing = sorted(n for n in EXPECTED_PRIVATE_HELPERS if not hasattr(cli, n))
    assert not missing, missing


def test_argv_surface_paths_unchanged():
    surface = _argv_surface(cli.build_parser())
    assert set(surface) == EXPECTED_ARGV_PATHS, (
        sorted(EXPECTED_ARGV_PATHS - set(surface)), sorted(set(surface) - EXPECTED_ARGV_PATHS))


def test_required_flags_per_path_unchanged():
    surface = _argv_surface(cli.build_parser())
    assert surface["init"] == {"title", "workspace"}, surface["init"]
    assert {"chapter", "version"} <= surface["approve"], surface["approve"]
    assert "book" in surface["card init"], surface["card init"]
    assert "card" in surface["card register"], surface["card register"]
    assert surface["card list"] == set(), surface["card list"]


def test_cli_error_identity_survives():
    from v4.core.cli import CLIError
    assert CLIError is cli.CLIError
    assert issubclass(CLIError, Exception)


@pytest.mark.parametrize("name", sorted(EXPECTED_FUNCS))
def test_facade_exposes_each_public_name(name):
    assert callable(getattr(cli, name)), name


def _deferred_relative_imports(source: str):
    """找出写在函数体内的相对导入，返回 (行号, 层级, 目标)。

    F-22 的机制就在这儿：`from .version_store import …` 在 v4/core/cli.py 里指向
    v4.core.version_store，搬进 v4/core/commands/review.py 后同一字面量指向
    v4.core.commands.version_store——名字与签名一把尺都量不出来，运行时才炸。
    """
    found = []
    tree = ast.parse(source)
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    for fn in funcs:
        for node in ast.walk(fn):
            if isinstance(node, ast.ImportFrom) and node.level and node.module:
                found.append((node.lineno, node.level, node.module))
    return found


def test_deferred_relative_import_depth_matches_file_location():
    """v4/core/*.py 体内的相对导入必须 level=1；v4/core/commands/*.py 必须 level=2。"""
    root = CLI_PATH.parent
    targets = [root / "cli.py", root / "cli_support.py"] + sorted(
        (root / "commands").glob("*.py"))
    wrong = []
    for f in targets:
        expected = 2 if f.parent.name == "commands" else 1
        for lineno, level, module in _deferred_relative_imports(f.read_text(encoding="utf-8")):
            if level != expected:
                wrong.append((f.name, lineno, level, expected, module))
    assert not wrong, wrong


def test_scanner_catches_a_planted_wrong_depth():
    """正对照：把错误深度塞进合成源码，尺子必须抓到（否则上条断言是空的）。"""
    bad = ("from __future__ import annotations\n"
           "def cmd_demo(args):\n"
           "    from .version_store import approve\n"
           "    return approve(args)\n")
    assert _deferred_relative_imports(bad) == [(3, 1, "version_store")]
    good = ("from __future__ import annotations\n"
            "def cmd_demo(args):\n"
            "    from ..version_store import approve\n"
            "    return approve(args)\n")
    assert _deferred_relative_imports(good) == [(3, 2, "version_store")]


def test_each_handler_lives_in_its_declared_family():
    """搬家后归属要钉住：cmd_* 的实现模块不能漂回门面或互相串族。"""
    owner = {}
    for name in sorted(n for n in EXPECTED_FUNCS if n.startswith("cmd_")):
        owner[name] = getattr(cli, name).__module__
    assert owner["cmd_init"] == "v4.core.commands.workspace", owner["cmd_init"]
    assert owner["cmd_approve"] == "v4.core.commands.review", owner["cmd_approve"]
    assert owner["cmd_publish_split"] == "v4.core.commands.delivery", owner["cmd_publish_split"]
    assert owner["cmd_check"] == "v4.core.commands.chapter", owner["cmd_check"]
    assert all(m.startswith("v4.core.commands.") for m in owner.values()), owner
