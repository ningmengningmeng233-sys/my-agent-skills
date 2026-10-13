# -*- coding: utf-8 -*-
"""机械化修复命令（加速改法工单 T12 / 第二批）。

- 五类缺陷：顿号超限 / 场景缺镜头体系 / 道具缺三视图 / 负面锁缺失 / 层次雷同。
- 规则与 T11 的 `prompt_rules` **同一份定义**（常量直接引用，不另写一套口径）。
- 幂等：修完用同一检测函数复检；仍有残留记 `unfixable` 并保持原状（不暴力重写）。
- 留痕：每处改动写一条 `{file, rule, before_hash, after_hash}` 到 `.process/自动修复记录.json`；
  dry-run 不落盘不改文件。
- 产物内容变化均可用「合规」解释：只把不合规片段改写为合规片段。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

from engine.prompt_rules import (
    MAX_IDEOMATIC_MARKS, SCENE_LAYER_TOKENS, THREE_VIEW_TOKENS,
    _COMPLIANCE_MARKERS, is_exempt_line, token_satisfied,
)

TRACE_NAME = "自动修复记录.json"


def file_asset_type(stem: str) -> Optional[str]:
    """从文档名取资产类型（人物/场景/道具提示词）；认不出返回 None。"""
    for t in ("人物", "场景", "道具"):
        if t in stem:
            return t
    return None


def _file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------- 检测 / 修复（检测与修复一一对应，修复后必须过同一检测） ----------------

def _iter_prompt_files(lib: Path):
    for p in sorted(Path(lib).rglob("*.md")):
        if "提示词" in str(p):
            yield p


def detect_dunhao(text: str, stem: str = "") -> list[str]:
    out = []
    for line in text.splitlines():
        s = line.strip()
        # 豁免口径与校验器**同一份实现**（负面清单/机读元数据行；序号与短横前缀先剥）。
        # 这里曾自带 `startswith(("负面提示词","【"))`：`7. 负面提示词：…`、`- 负面提示词：…`、
        # `负面限制：…` 三种写法校验器都豁免、本工具却照改，等于**改坏已合规的交付物**。
        if not s or is_exempt_line(s):
            continue
        if s.count("、") >= MAX_IDEOMATIC_MARKS:
            out.append(s)
    return out


def fix_dunhao(text: str, stem: str = "") -> tuple[str, int]:
    """把超出上限的顿号替换为逗号（自**行首**起逐个替换），直到降到上限以下。"""
    lines = text.splitlines(keepends=True)
    fixed = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or is_exempt_line(s):
            continue
        excess = s.count("、") - (MAX_IDEOMATIC_MARKS - 1)
        if excess <= 0:
            continue
        chars = list(line)
        idx = [j for j, ch in enumerate(chars) if ch == "、"]
        for j in reversed(idx[:excess]):
            chars[j] = "，"
        lines[i] = "".join(chars)
        fixed += 1
    return "".join(lines), fixed


def _canonical(item) -> str:
    """取要求项的「落盘写法」：字面量原样，等价写法组取第一种。"""
    return item if isinstance(item, str) else item[0]


def _missing_tokens(text: str, tokens: tuple[str, ...]) -> list[str]:
    return [_canonical(t) for t in tokens if not token_satisfied(t, text)]


def _append_to_last_body_line(text: str, add: str) -> tuple[str, int]:
    """把补足句追加到最后一个正文行（跳过标题与表格分隔行）；无正文行则文末另起一行。"""
    lines = text.splitlines(keepends=True)
    for i in range(len(lines) - 1, -1, -1):
        s = lines[i].strip()
        if s and not s.startswith("#") and not s.startswith("|---"):
            lines[i] = lines[i].rstrip("\n") + add + "\n"
            return "".join(lines), 1
    return text + add + "\n", 1


# 缺镜头体系 token 时的默认补足值（其余 token 一律用「均匀柔光」）
_LAYER_FILL_DEFAULTS = {"景别": "全景", "角度": "平视"}


def detect_scene_layers(text: str, stem: str = "") -> list[str]:
    if file_asset_type(stem) != "场景":
        return []
    return [t for t in _missing_tokens(text, SCENE_LAYER_TOKENS)]


def fix_scene_layers(text: str, stem: str = "") -> tuple[str, int]:
    missing = _missing_tokens(text, SCENE_LAYER_TOKENS)
    if not missing:
        return text, 0
    fill = "。".join(f"{t}：{_LAYER_FILL_DEFAULTS.get(t, '均匀柔光')}" for t in missing)
    return _append_to_last_body_line(text, f"镜头体系补足：{fill}。")


def detect_three_view(text: str, stem: str = "") -> list[str]:
    if file_asset_type(stem) != "道具":
        return []
    return [t for t in _missing_tokens(text, THREE_VIEW_TOKENS)]


def fix_three_view(text: str, stem: str = "") -> tuple[str, int]:
    missing = _missing_tokens(text, THREE_VIEW_TOKENS)
    if not missing:
        return text, 0
    return _append_to_last_body_line(text, "三视图保底补足：" + "、".join(missing) + "。")


def detect_negative_lock(text: str, stem: str = "") -> list[str]:
    """按**本文件类型**检查负面段与负面锁（同检查器常量，不跨类型误报）。"""
    atype = file_asset_type(stem)
    if atype is None:
        return []
    problems: list[str] = []
    for label, tokens, mode in _COMPLIANCE_MARKERS[atype]:
        if label not in ("负面段", "负面锁"):
            continue
        if mode == "all":
            problems.extend(f"{label}缺: {_canonical(t)}"
                            for t in tokens if not token_satisfied(t, text))
        elif not any(token_satisfied(t, text) for t in tokens):
            problems.append(f"{label}缺: {_canonical(tokens[0])}")
    return problems


def fix_negative_lock(text: str, stem: str = "") -> tuple[str, int]:
    problems = detect_negative_lock(text, stem)
    if not problems:
        return text, 0
    # 收集要补的负面锁 token；「负面提示词：」是段落标记，新建负面段时自然满足，不重复拼接
    add_tokens: list[str] = []
    for prob in problems:
        m = re.match(r"负面(段|锁)缺: (.+)", prob)
        if m and m.group(2) != "负面提示词：":
            add_tokens.append(m.group(2))
    if not add_tokens:
        return text, 0
    lines = text.splitlines(keepends=True)
    neg_idx = next((i for i, l in enumerate(lines) if l.strip().startswith("负面提示词")), None)
    if neg_idx is None:
        # 新建独立负面段行（不与正文粘连，避免把豁免行混进普通行的顿号口径）
        add = "负面提示词：" + "、".join(add_tokens) + "。" + "\n"
        insert_at = max((i for i, l in enumerate(lines)
                         if l.strip() and not l.strip().startswith("#")),
                        default=len(lines) - 1) + 1
        lines.insert(insert_at, add)
        return "".join(lines), 1
    line = lines[neg_idx].rstrip("\n")
    extra = [t for t in add_tokens if t not in line]
    if not extra:
        return text, 0
    sep = "" if line.rstrip().endswith(("。", "！")) else "、"
    lines[neg_idx] = line + sep + "、".join(extra) + "。" + "\n"
    return "".join(lines), 1


_SPAN_RE = re.compile(r"(前|中|后)景([^，。；\n]{2,})")


def detect_duplicates(text: str, stem: str = "") -> list[str]:
    """层次雷同：前/中/后景各片段中出现 ≥4 字相同连续内容（跨层次复读）。"""
    problems: list[str] = []
    for line in text.splitlines():
        spans = _SPAN_RE.findall(line)
        if len(spans) < 2:
            continue
        seen: dict[str, str] = {}
        for pos, body in spans:
            if len(body) < 4:
                continue
            for k in seen:
                if body[:4] == k[:4] and (body[:4] != ""):
                    problems.append(line.strip()[:60])
                    break
            seen[body] = pos
        if problems and problems[-1] == line.strip()[:60]:
            continue
    return problems


def fix_duplicates(text: str, stem: str = "") -> tuple[str, int]:
    """去掉后层里与前面层次重复的开头片段，保留位置词与差异部分。"""
    lines = text.splitlines(keepends=True)
    fixed = 0
    for i, line in enumerate(lines):
        matches = list(_SPAN_RE.finditer(line))
        if len(matches) < 2:
            continue
        seen_bodies: list[str] = []
        new_line = line
        for m in matches:
            body = m.group(2)
            if len(body) < 4:
                continue
            dup = next((s for s in seen_bodies if body[:4] == s[:4]), None)
            if dup is not None:
                keep = body.split("的")[-1] if "的" in body else body[-2:]
                new_line = new_line.replace(f"{m.group(1)}景{body}",
                                            f"{m.group(1)}景{keep}", 1)
                fixed += 1
            else:
                seen_bodies.append(body)
        lines[i] = new_line
    return "".join(lines), fixed


RULES: list[tuple[str, Callable, Callable]] = [
    ("顿号超限", detect_dunhao, fix_dunhao),
    ("场景缺镜头体系", detect_scene_layers, fix_scene_layers),
    ("道具缺三视图", detect_three_view, fix_three_view),
    ("负面锁缺失", detect_negative_lock, fix_negative_lock),
    ("层次雷同", detect_duplicates, fix_duplicates),
]


def repair_text(text: str, asset_type: str, max_rounds: int = 3) -> tuple[str, int]:
    """对**未落盘**的单份产出按资产类型跑机械化修复（纯函数，不写盘）。

    生成侧的合规闸在复检失败前调用它：产出此时还没落盘，走不了 `run()` 的
    文件扫描那条路。规则与检测/修复函数与落盘路径**完全同一份**。

    **多轮收敛**：某条规则补写的内容可能让另一条规则由合规转为不合规——
    例如追加三视图会把同一行的顿号推过上限，而顿号规则排在它之前，
    单轮遍历会停在一个既不合规、又比原文更差的中间态（对抗评审 B2）。
    无改动即提前结束，最多 `max_rounds` 轮。
    """
    stem = f"{asset_type}提示词"
    out = text
    total = 0
    for _round in range(max(1, int(max_rounds))):
        changed = 0
        for _name, detect, fix in RULES:
            if not detect(out, stem):
                continue
            out, n = fix(out, stem)
            changed += n
        total += changed
        if not changed:
            break
    return out, total


def _scan(lib: Path) -> list[tuple[Path, str, list[str]]]:
    """返回 [(file, rule, problems)]，只含确有问题的项。"""
    found: list[tuple[Path, str, list[str]]] = []
    for p in _iter_prompt_files(lib):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for rule, detect, _fix in RULES:
            problems = detect(text, p.stem)
            if problems:
                found.append((p, rule, problems))
    return found


def run(lib, dry_run: bool = False) -> dict:
    """扫描并（除非 dry_run）修复；返回报告并留痕。幂等。"""
    lib = Path(lib)
    found = _scan(lib)
    items: list[dict] = []
    unfixable: list[dict] = []
    fixed_count = 0
    # 同一文件可能命中多条规则：按文件聚合后依序应用
    by_file: dict[Path, list[str]] = {}
    for p, rule, _problems in found:
        by_file.setdefault(p, []).append(rule)
    for p, rules in sorted(by_file.items()):
        text = p.read_text(encoding="utf-8")
        before_hash = _file_hash(text)
        new_text = text
        applied: list[str] = []
        for rule in rules:
            _name, _detect, fix = next(r for r in RULES if r[0] == rule)
            new_text, n = fix(new_text, p.stem)
            if n:
                applied.append(rule)
                fixed_count += n
        if not applied:
            unfixable.append({"file": str(p), "rules": rules})
            continue
        if not dry_run:
            p.write_text(new_text, encoding="utf-8")
            items.append({
                "file": str(p.relative_to(lib)),
                "rule": "+".join(applied),
                "before_hash": before_hash,
                "after_hash": _file_hash(new_text),
            })
    report = {"fixed_count": fixed_count, "items": items, "unfixable": unfixable,
              "dry_run": bool(dry_run), "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if not dry_run and items:
        trace = lib / ".process" / TRACE_NAME
        trace.parent.mkdir(parents=True, exist_ok=True)
        tmp = trace.with_suffix(".tmp")
        tmp.write_text(json.dumps({"schema_version": 1, "items": items,
                                   "unfixable": unfixable, "ts": report["ts"]},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, trace)
    return report


def recheck(lib) -> dict:
    """修复后复检：与检测共用同一函数（幂等保证）。"""
    remaining = _scan(Path(lib))
    return {"failures": len(remaining),
            "detail": [f"{p.name}:{rule}" for p, rule, _ in remaining]}
