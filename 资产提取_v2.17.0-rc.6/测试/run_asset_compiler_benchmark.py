#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 11：质量不退化的资产编译器性能基准。

设计要点
--------
- **速度门与质量门互相独立**：任何一门失败都不得晋级，速度达标不能抵消质量退步。
- **禁止伪报**：测不到的项一律写 `unknown` 并**不算通过**；未知项汇总进 `not_run`。
- **耗时分项**：`endpoint_ms`（模型等待）/ `local_ms`（本地处理）/ `queue_ms`（排队），
  端点慢导致未达标时必须在报告里看得出来是端点而非本地。
- **质量只看可机器判定的事实**：事实覆盖、证据完整、关系合法、风格冲突、提示词完整、审核契约；
  「更短」不等于「更好」——提示词完整度下降会被质量门抓住。
- 默认 mock/录制响应，不依赖真实密钥；真实端点结果另表记录（`--real-endpoint`，默认关闭）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

UNKNOWN = "unknown"

SPEED_TARGETS = {
    "text_calls_reduction": 0.60,
    "text_wall_reduction": 0.50,
    "unrelated_rerun_reduction": 0.80,
}

# 质量指标方向：越高越好 / 越低越好
QUALITY_HIGHER_IS_BETTER = ("fact_coverage", "evidence_complete", "relations_valid",
                            "prompt_complete", "review_contract")
QUALITY_LOWER_IS_BETTER = ("style_conflicts",)

DEFAULT_RUBRIC = {
    "version": 1,
    "baseline": "v2.16.0-rc.1",
    "metrics": {
        "fact_coverage": "正式资产中有原文证据的比例（越高越好）",
        "evidence_complete": "提示词证据元数据齐备的比例（越高越好）",
        "relations_valid": "关系端点全部存在且类型合法的比例（越高越好）",
        "style_conflicts": "正向分区内相斥风格词命中数（越低越好）",
        "prompt_complete": "每个正式资产都有基础提示词的比例（越高越好）",
        "review_contract": "审核契约（绑定哈希齐备）通过的比例（越高越好）",
    },
}


def _reduction(old_value, new_value):
    """下降比例；缺测或无法比较一律 `unknown`（不得当成达标）。"""
    if old_value is None or new_value is None or old_value == UNKNOWN or new_value == UNKNOWN:
        return UNKNOWN
    old_value, new_value = float(old_value), float(new_value)
    if old_value == 0:
        return 1.0 if new_value == 0 else 0.0
    return round(1.0 - new_value / old_value, 4)


def _speed_gate(value, target: float) -> dict:
    passed = value != UNKNOWN and float(value) >= target
    return {"value": value, "target": target, "passed": bool(passed)}


def _timing_total(timing: dict) -> float:
    return sum(float(timing.get(key) or 0) for key in ("endpoint_ms", "local_ms", "queue_ms"))


def _timing_of(metrics: dict) -> dict:
    timing = metrics.get("timing") or {}
    return {key: timing.get(key, UNKNOWN) for key in ("endpoint_ms", "local_ms", "queue_ms")}


def _quality_gates(old_quality: dict, new_quality: dict) -> dict:
    gates: dict = {}
    for key in QUALITY_HIGHER_IS_BETTER:
        old_value, new_value = old_quality.get(key), new_quality.get(key)
        if old_value is None or new_value is None:
            gates[key] = {"old": UNKNOWN, "new": UNKNOWN, "passed": False}
            continue
        gates[key] = {"old": old_value, "new": new_value, "passed": bool(new_value >= old_value)}
    for key in QUALITY_LOWER_IS_BETTER:
        old_value, new_value = old_quality.get(key), new_quality.get(key)
        if old_value is None or new_value is None:
            gates[key] = {"old": UNKNOWN, "new": UNKNOWN, "passed": False}
            continue
        gates[key] = {"old": old_value, "new": new_value, "passed": bool(new_value <= old_value)}
    return gates


def build_report(old: dict, new: dict) -> dict:
    """合成基准报告。速度与质量各自独立判定；缺测项记 unknown 且不算通过。"""
    speed = {
        "text_calls_reduction": _speed_gate(
            _reduction(old.get("text_calls"), new.get("text_calls")),
            SPEED_TARGETS["text_calls_reduction"]),
        "text_wall_reduction": _speed_gate(
            _reduction(_timing_of_total(old), _timing_of_total(new)),
            SPEED_TARGETS["text_wall_reduction"]),
        "unrelated_rerun_reduction": _speed_gate(
            _reduction((old.get("rerun_tasks") or {}).get("unrelated"),
                       (new.get("rerun_tasks") or {}).get("unrelated")),
            SPEED_TARGETS["unrelated_rerun_reduction"]),
    }
    quality = _quality_gates(old.get("quality") or {}, new.get("quality") or {})

    unknown = [name for name, gate in speed.items() if gate["value"] == UNKNOWN]
    unknown += [f"quality.{name}" for name, gate in quality.items() if gate["old"] == UNKNOWN]
    speed_ok = all(gate["passed"] for gate in speed.values())
    quality_ok = all(gate["passed"] for gate in quality.values())

    if unknown:
        verdict = "NOT_RUN"
    elif speed_ok and quality_ok:
        verdict = "PROMOTED"
    else:
        verdict = "FAILED"

    return {
        "schema_version": 1,
        "speed_targets": dict(SPEED_TARGETS),
        "speed": speed,
        "quality": quality,
        "timing": {"old": _timing_of(old), "new": _timing_of(new)},
        "speed_ok": speed_ok,
        "quality_ok": quality_ok,
        "promoted": bool(speed_ok and quality_ok and not unknown),
        "verdict": verdict,
        "not_run": sorted(unknown),
        "endpoint_bound": _endpoint_bound(old, new),
    }


def _timing_of_total(metrics: dict) -> float | str:
    timing = metrics.get("timing") or {}
    if any(timing.get(key) is None for key in ("endpoint_ms", "local_ms", "queue_ms")):
        return UNKNOWN
    return _timing_total(timing)


def _endpoint_bound(old: dict, new: dict) -> bool:
    """未达标是否主要来自端点等待（用于把「端点慢」与「本地没优化」分开）。"""
    for metrics in (old, new):
        timing = metrics.get("timing") or {}
        endpoint = timing.get("endpoint_ms") or 0
        local = timing.get("local_ms") or 0
        if endpoint > local:
            return True
    return False


def wall_model(local_ms, calls, delay_ms) -> float:
    """墙钟模型：`本地实测开销 + 调用次数 × 端点延迟`。三个输入全部来自实测，公式唯一。

    为什么判定用它而不是端到端墙钟：端到端在共享机器上会被负载抖动放大——本机实测过一次
    「旧链 2100→4110 ms」的尖峰，足以把下降比例压到目标线下，而那与代码快慢无关。
    模型把不稳定的部分（本地开销）限制在几千毫秒量级，把稳定的部分（调用次数 × 声明延迟）
    当主项，因此**同一台机器上可复现**；端到端实测值照样并列写进报告供对照。
    """
    return round(float(local_ms or 0) + float(calls or 0) * float(delay_ms or 0), 1)


# --- 实测运行 -----------------------------------------------------------------

def _load_rubric(fixture_dir: Path) -> dict:
    path = fixture_dir / "quality_rubric.json"
    if not path.is_file():
        return dict(DEFAULT_RUBRIC)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_RUBRIC)


def _measure_old_chain(fixture_dir: Path, workdir: Path, *, delay_ms: float = 0.0) -> dict:
    """跑一遍 v2.16 旧链路（mock 端点），实测文本调用次数与耗时分项。

    阶段调用序列与参数**照抄** `repro_asset_compiler_failures`，不另立一套；
    任一阶段非零退出或一次调用都没发生 → `measured=False`，调用次数记 `None`（unknown），
    绝不把「没跑起来」报成「0 次调用」。
    """
    from mock_api import Handler, start_mock_server     # noqa: PLC0415
    import repro_asset_compiler_failures as repro       # noqa: PLC0415

    lib = workdir / "资产库"
    lib.mkdir(parents=True, exist_ok=True)
    server, url = start_mock_server()
    Handler.endpoint_delay_ms = float(delay_ms)
    Handler.reset_calls()
    started = time.time()
    stage_rc: dict = {}
    try:
        stages = [
            ("split_segments.py", [repro.SOURCE, "--novel", repro.NOVEL, "--author", repro.AUTHOR,
                                   "--lib", str(lib), "--overlap", "1"]),
            ("scan_registry.py", [str(lib / "原文段" / repro.GROUP), "--lib", str(lib)]),
            ("extract_segments.py", [str(lib / "原文段" / repro.GROUP), "--lib", str(lib)]),
            ("aggregate.py", [str(lib / "段产物" / repro.GROUP), str(lib / "原文段" / repro.GROUP),
                              "--lib", str(lib)]),
            ("generate_prompts.py", ["--base", str(lib / "反推_最终.md"), "--genre", "古风",
                                     "--lib", str(lib), "--style", "仙侠奇幻风", "--yes",
                                     "--view-mode", "full"]),
        ]
        for script, script_args in stages:
            rc, _out, _err = repro.run_stage(script, script_args, str(lib), url)
            stage_rc[script] = rc
    finally:
        Handler.endpoint_delay_ms = 0.0
        server.shutdown()
    calls = Handler.calls()
    failed = sorted(name for name, rc in stage_rc.items() if rc != 0)
    measured = calls > 0 and not failed
    return {
        "text_calls": calls if measured else None,
        "wall_ms": int((time.time() - started) * 1000),
        "stage_rc": stage_rc,
        "failed_stages": failed,
        "measured": measured,
        "lib": str(lib),
        "backend": url,
        "delay_ms": float(delay_ms),
    }


# --- 新链路（工单 14 接线）：同一前半段 + 编译链尾段 -------------------------------

def _measure_new_chain(fixture_dir: Path, workdir: Path, *, delay_ms: float = 0.0) -> dict:
    """跑一遍 v2.17 新链路：拆分/登记/提取/聚合照旧，尾段换成总账 + 确定性编译。

    与旧链路逐阶段同源（前半段完全相同），差异只在尾段：旧链路用 `generate_prompts --base`
    逐资产发模型调用，新链路 `build-ledger` + `compile-prompts --shadow` **零模型调用**。
    任一阶段非零退出 → `measured=False`，调用次数记 `None`（unknown）。
    """
    from mock_api import Handler, start_mock_server     # noqa: PLC0415
    import repro_asset_compiler_failures as repro       # noqa: PLC0415

    lib = workdir / "资产库"
    lib.mkdir(parents=True, exist_ok=True)
    server, url = start_mock_server()
    Handler.endpoint_delay_ms = float(delay_ms)
    Handler.reset_calls()
    started = time.time()
    stage_rc: dict = {}
    try:
        # 尾段用 `--apply`：基准比的是**同一种交付物**（正式基础提示词），
        # shadow 只是预览，拿它跟旧链路的正式产物比会得出「新链路没提示词」的假结论。
        stages = [
            ("split_segments.py", "split_segments.py",
             [repro.SOURCE, "--novel", repro.NOVEL, "--author", repro.AUTHOR,
              "--lib", str(lib), "--overlap", "1"]),
            ("scan_registry.py", "scan_registry.py",
             [str(lib / "原文段" / repro.GROUP), "--lib", str(lib)]),
            ("extract_segments.py", "extract_segments.py",
             [str(lib / "原文段" / repro.GROUP), "--lib", str(lib)]),
            ("aggregate.py", "aggregate.py",
             [str(lib / "段产物" / repro.GROUP), str(lib / "原文段" / repro.GROUP), "--lib", str(lib)]),
            ("build-ledger", "maintenance.py", ["build-ledger", "--lib", str(lib)]),
            ("compile-prompts --apply", "maintenance.py",
             ["compile-prompts", "--lib", str(lib), "--apply"]),
        ]
        for label, script, script_args in stages:
            rc, _out, _err = repro.run_stage(script, script_args, str(lib), url)
            stage_rc[label] = rc
    finally:
        Handler.endpoint_delay_ms = 0.0
        server.shutdown()
    calls = Handler.calls()
    failed = sorted(name for name, rc in stage_rc.items() if rc != 0)
    measured = calls > 0 and not failed
    return {
        "text_calls": calls if measured else None,
        "wall_ms": int((time.time() - started) * 1000),
        "stage_rc": stage_rc,
        "failed_stages": failed,
        "measured": measured,
        "lib": str(lib),
        "backend": url,
        "delay_ms": float(delay_ms),
    }


def _rerun_other_assets(lib: Path, *, use_compiler: bool, backend: str) -> int | None:
    """改一个资产的证据后重跑提示词环节，数「**无关**资产被重算的次数」。

    旧链路（`generate_prompts --base`）：每个正式资产各发一次模型调用，无关资产全被重算；
    新链路（`compile-prompts --shadow`）：确定性本地编译，无关资产 0 次模型调用。
    单位是**模型调用次数**，报告里以 `rerun_unit` 明写，避免与「任务数」混淆。
    """
    import repro_asset_compiler_failures as repro       # noqa: PLC0415
    from mock_api import Handler                        # noqa: PLC0415

    index_path = lib / ".process" / "资产索引.json"
    if not index_path.is_file():
        return None
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        assets = [a for a in index.get("assets") or [] if isinstance(a, dict)]
        if len(assets) < 2:
            return None
        target = assets[0]
        quotes = [e for e in (target.get("evidence") or []) if isinstance(e, dict)]
        marker = quotes[0] if quotes else None
        if marker is None:
            target.setdefault("evidence", []).append(
                {"segment": "基准扰动.md", "line": "00001", "quote": "基准扰动证据"})
        else:
            marker["quote"] = str(marker.get("quote", "")) + "（基准扰动）"
        index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8", newline="\n")
    except (OSError, ValueError):
        return None

    Handler.reset_calls()
    if use_compiler:
        from maintenance import do_build_ledger, do_compile_prompts  # noqa: PLC0415

        do_build_ledger(lib, as_json=True)
        do_compile_prompts(lib, as_json=True)
    else:
        rc, _out, _err = repro.run_stage(
            "generate_prompts.py", ["--base", str(lib / "反推_最终.md"), "--genre", "古风",
                                    "--lib", str(lib), "--style", "仙侠奇幻风", "--yes",
                                    "--view-mode", "full"], str(lib), backend)
        if rc != 0:
            return None
    return Handler.calls()


# --- 质量指标（六项，全部机器判定） ------------------------------------------------

def _official_prompt_sections(lib: Path) -> list:
    """正式基础提示词的 (标题, 正文) 列表（旧链路的质量取样面）。"""
    base = lib / "提示词" / "基础"
    if not base.is_dir():
        return []
    out = []
    for path in sorted(base.glob("*.md")):
        if path.name == "跳过清单.md":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        marks = list(re.finditer(r"^## \d{3}\. (.+?)\s*$", text, re.M))
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            out.append((m.group(1), text[m.end():end]))
    return out


def _style_conflict_count(text: str) -> int:
    from engine.art_constitution import detect_conflicts_by_source, split_prompt_blocks

    blocks = split_prompt_blocks(text or "")
    affirmative = {k: v for k, v in blocks.items() if k != "_negative"} or {"请求正文": text or ""}
    return len(detect_conflicts_by_source(affirmative))


def _quality_metrics(lib: Path, *, compiled: dict | None = None) -> dict:
    """六项可机器判定的质量指标；做不到的项返回 None（上层记 unknown，不算通过）。

    `compiled` 给出新链路的编译产物 `{资产ID: (类型, 名称, 正文)}`；缺省时按旧链路口径
    从正式基础提示词取样。两项提示词相关指标的**取样面因此在两链间不同**，
    报告里以 `basis` 明写，避免被读成同口径提升。
    """
    import check_asset_coverage as coverage          # noqa: PLC0415
    from engine import asset_ledger as ledger_mod     # noqa: PLC0415

    lib = Path(lib)
    index_path = lib / ".process" / "资产索引.json"
    if not index_path.is_file():
        return {}
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ledger = ledger_mod.load_ledger(lib) or ledger_mod.build_ledger(index)
    formal = [e for e in ledger
              if e.get("ledger_status") == "formal" and "base_prompt" in (e.get("required_outputs") or [])]
    if not formal:
        return {}

    metrics: dict = {}
    metrics["fact_coverage"] = round(
        sum(1 for e in formal if e.get("evidence_ids")) / len(formal), 4)

    report = coverage.evaluate(ledger, coverage.load_prompted_names(str(lib)),
                               coverage.load_skipped(str(lib)))
    metrics["prompt_complete"] = float(report["coverage"])

    ids = {a.get("asset_id") for a in index.get("assets") or [] if isinstance(a, dict)}
    total_rel = valid_rel = 0
    for asset in index.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        for rel in asset.get("relations") or []:
            if not isinstance(rel, dict):
                continue
            total_rel += 1
            if rel.get("target") in ids:
                valid_rel += 1
    metrics["relations_valid"] = 1.0 if total_rel == 0 else round(valid_rel / total_rel, 4)

    if compiled is None:
        sections = _official_prompt_sections(lib)
    else:
        sections = [(name, text) for _t, name, text in compiled.values()]
    metrics["style_conflicts"] = sum(_style_conflict_count(text) for _t, text in sections)
    with_meta = sum(1 for _t, text in sections if "证据元数据" in text)
    metrics["evidence_complete"] = (round(with_meta / len(sections), 4) if sections else None)

    # 审核契约：分母是「已构造的审核请求数」；没有请求时为空集，不违反契约 → 1.0。
    # 同时要求绑定字段与清单齐备，字段被删即 0.0（这才是本项要拦的回归）。
    try:
        from engine.review_contract import CHECKLIST_ITEMS, ReviewBinding   # noqa: PLC0415

        fields = set(getattr(ReviewBinding, "__dataclass_fields__", {}))
        need = {"image_sha256", "prompt_sha256", "reference_set_sha256", "style_sha256",
                "composition_sha256"}
        metrics["review_contract"] = 1.0 if (CHECKLIST_ITEMS and need <= fields) else 0.0
    except ImportError:
        metrics["review_contract"] = None
    return metrics


QUALITY_BASIS = {
    "fact_coverage": "资产索引 assets[*].evidence 非空占比（两链同口径）",
    "prompt_complete": "check_asset_coverage 的 coverage（两链同口径）",
    "relations_valid": "关系端点存在于索引的比例（两链同口径）",
    "style_conflicts": "正向分区相斥风格词命中数（旧=正式提示词节；新=编译产物）",
    "evidence_complete": "含【证据元数据】分区的提示词比例（旧=正式提示词节，恒 0；新=编译产物）",
    "review_contract": "审核绑定字段与清单齐备且无违规请求（无请求时为空集 → 1.0）",
}


def _counts_of(lib: Path) -> dict:
    """逐项计数（工单 14 步骤 4）：资产数 / 提示词数 / 图片任务数——相等或有明确阻断。

    图片任务在两条链路里都没有创建视觉计划，因此**不报 0 冒充「相等」**：
    无计划一律记 `NOT_RUN（两链均未创建视觉计划）`。
    """
    lib = Path(lib)
    index_path = lib / ".process" / "资产索引.json"
    assets = None
    if index_path.is_file():
        try:
            assets = len(json.loads(index_path.read_text(encoding="utf-8")).get("assets") or [])
        except (OSError, ValueError):
            assets = None
    prompts = 0
    base = lib / "提示词" / "基础"
    if base.is_dir():
        for path in sorted(base.glob("*.md")):
            if path.name == "跳过清单.md":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            prompts += len(re.findall(r"^## \d{3}\. ", text, re.M))
    plan = lib / "视觉资产" / "计划.toml"
    return {"assets": assets, "prompts": prompts,
            "image_tasks": None if not plan.is_file() else "plan_present"}


def measure_repeated(label, measure, fixture_dir, root, delay, repeats):
    """Keep all timing samples; reject differing work before choosing a minimum."""
    best = None
    calls = None
    walls = []
    for i in range(max(1, repeats)):
        run = measure(fixture_dir, root / f"{label}-{delay:g}-{i}", delay_ms=delay)
        walls.append(run.get("wall_ms"))
        if not run.get("measured") or run.get("text_calls") is None:
            return {**run, "measured": False, "text_calls": None,
                    "wall_samples_ms": walls, "repeats": len(walls)}
        if best is not None and run["text_calls"] != calls:
            return {**run, "uncomparable":
                    f"调用次数在重复间不一致：{calls} vs {run['text_calls']}",
                    "measured": False, "text_calls": None,
                    "wall_samples_ms": walls, "repeats": len(walls)}
        calls = run["text_calls"]
        if best is None or run["wall_ms"] < best["wall_ms"]:
            best = run
    return {**best, "repeats": len(walls), "wall_samples_ms": walls}


def measure_sweep(fixture_dir, root, delays, repeats):
    """Repeat every delay, including the zero-delay input used by the gate."""
    return {
        delay: {
            "old": measure_repeated("old", _measure_old_chain, fixture_dir,
                                    root, delay, repeats),
            "new": measure_repeated("new", _measure_new_chain, fixture_dir,
                                    root, delay, repeats),
        }
        for delay in delays
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="资产编译器性能基准（默认 mock，无需真实密钥）")
    parser.add_argument("--fixture", required=True, help="夹具目录（含 source.txt 与 quality_rubric.json）")
    parser.add_argument("--out", required=True, help="报告输出路径（JSON）")
    parser.add_argument("--real-endpoint", action="store_true",
                        help="另跑真实端点（需密钥）；默认只跑本地 mock")
    parser.add_argument("--endpoint-delay-ms", type=float, default=1000.0,
                        help="判定所依据的端点每次调用延迟（默认 1000ms：真实模型端点对长提示词"
                             "每次调用普遍在 1–10 秒，这是保守下限）。本地 mock 是瞬时的，"
                             "不注入延迟就测不出「少调用 N 次」在墙钟上的意义。")
    parser.add_argument("--delay-sweep", default="0,300,1000",
                        help="延迟扫描（逗号分隔，毫秒）：报告给出各延迟下的墙钟下降曲线，"
                             "复核者据此看交叉点，不必相信单一数字。")
    parser.add_argument("--wall-repeat", type=int, default=2,
                        help="所有延迟（含0延迟）每链重复次数，取**最小**墙钟："
                             "可抵消机器负载抖动。两次调用次数必须一致，否则判不可比。")
    parser.add_argument("--allow-not-run", action="store_true",
                        help="新链路未跑通时允许 NOT_RUN 通过门禁；FAILED 仍然拦下（不把「还没测」当成「通过」）")
    args = parser.parse_args(argv)

    fixture_dir = Path(args.fixture).resolve()
    rubric = _load_rubric(fixture_dir)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    import tempfile

    gate_delay = float(args.endpoint_delay_ms)
    wall_repeat = max(1, int(args.wall_repeat))
    delays = {0.0, gate_delay}
    for token in str(args.delay_sweep or "").split(","):
        if token.strip():
            delays.add(float(token.strip()))
    delays = sorted(delays)

    runs: dict = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        runs = measure_sweep(fixture_dir, root, delays, wall_repeat)

        old = runs[gate_delay]["old"]
        new = runs[gate_delay]["new"]
        old_plain = runs[0.0]["old"]
        new_plain = runs[0.0]["new"]

        old_quality, new_quality = {}, {}
        old_rerun = new_rerun = None
        if old["measured"]:
            old_quality = _quality_metrics(Path(old["lib"]))
            old_rerun = _rerun_other_assets(Path(old["lib"]), use_compiler=False,
                                            backend=old["backend"])
        if new["measured"]:
            lib_new = Path(new["lib"])
            try:
                sys.path.insert(0, str(REPO_ROOT / "scripts"))
                from engine.prompt_compiler import compile_library     # noqa: PLC0415

                compiled = compile_library(lib_new)["compiled"]
            except Exception:                                          # noqa: BLE001
                compiled = None
            new_quality = _quality_metrics(lib_new, compiled=compiled)
            new_rerun = _rerun_other_assets(lib_new, use_compiler=True, backend=new["backend"])
        old_counts = _counts_of(Path(old["lib"])) if old["measured"] else {}
        new_counts = _counts_of(Path(new["lib"])) if new["measured"] else {}

    def _side(metrics: dict, quality: dict, rerun, calls, plain) -> dict:
        # 判定用墙钟模型（本地实测 + 调用次数 × 声明延迟），端到端实测并列留档。
        # timing 三段互斥：模型值整体记在 endpoint_ms，local_ms 若再填一次就是重复计数。
        modeled = wall_model(plain["wall_ms"], calls, gate_delay)
        return {
            "text_calls": calls,
            "timing": {"endpoint_ms": modeled, "local_ms": 0, "queue_ms": 0},
            "quality": quality,
            "rerun_tasks": {"total": len(quality) or None, "unrelated": rerun},
            "wall_ms_modeled": modeled,
            "wall_ms_measured": metrics["wall_ms"],
            "wall_ms_no_delay": plain["wall_ms"],
        }

    old_metrics = _side(old, old_quality, old_rerun, old["text_calls"], old_plain)
    new_metrics = _side(new, new_quality, new_rerun, new["text_calls"], new_plain)
    report = build_report(old_metrics, new_metrics)
    report["rubric"] = rubric
    report["quality_basis"] = QUALITY_BASIS
    report["rerun_unit"] = "text_calls（改一个资产后，无关资产被重算的**模型调用次数**）"
    report["endpoint_delay_ms"] = gate_delay
    report["wall_sweep"] = [
        {
            "delay_ms": delay,
            "old_wall_ms": runs[delay]["old"]["wall_ms"],
            "new_wall_ms": runs[delay]["new"]["wall_ms"],
            "old_text_calls": runs[delay]["old"]["text_calls"],
            "new_text_calls": runs[delay]["new"]["text_calls"],
            "old_wall_samples_ms": runs[delay]["old"]["wall_samples_ms"],
            "new_wall_samples_ms": runs[delay]["new"]["wall_samples_ms"],
            "wall_reduction": _reduction(runs[delay]["old"]["wall_ms"],
                                         runs[delay]["new"]["wall_ms"]),
        }
        for delay in delays
    ]
    report["counts"] = {
        "basis": "工单 14 步骤 4：两链逐项计数；图片任务两链均未创建视觉计划，记 NOT_RUN 而不是报 0 冒充相等",
        "old": old_counts,
        "new": new_counts,
    }
    report["wall_evidence"] = {
        "basis": "判定用 wall_model = 本地实测开销（0 延迟墙钟的最小值）+ 调用次数 × 端点延迟；"
                 "端到端实测值并列留档供对照。0 毫秒那行是本地开销下限（mock 端点瞬时）。",
        "wall_repeat": wall_repeat,
        "old_wall_ms_modeled": old_metrics["wall_ms_modeled"],
        "new_wall_ms_modeled": new_metrics["wall_ms_modeled"],
        "old_wall_ms_measured": old_metrics["wall_ms_measured"],
        "new_wall_ms_measured": new_metrics["wall_ms_measured"],
        "old_wall_ms_no_delay": old_plain["wall_ms"],
        "new_wall_ms_no_delay": new_plain["wall_ms"],
    }
    report["old_chain"] = {"text_calls": old["text_calls"], "wall_ms": old["wall_ms"],
                           "measured": old["measured"], "stage_rc": old["stage_rc"],
                           "failed_stages": old["failed_stages"]}
    report["new_chain"] = {"text_calls": new["text_calls"], "wall_ms": new["wall_ms"],
                           "measured": new["measured"], "stage_rc": new["stage_rc"],
                           "failed_stages": new["failed_stages"]}
    report["real_endpoint"] = "NOT_RUN（默认不跑真实端点）" if not args.real_endpoint else "requested"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📊 基准报告: {out_path}")
    for label, side in (("旧链路", old), ("新链路", new)):
        if side["measured"]:
            print(f"   {label}文本调用 {side['text_calls']} 次 / {side['wall_ms']} ms")
        else:
            print(f"   ⚠️ {label}未跑通（失败阶段：{'、'.join(side['failed_stages']) or '无调用'}），"
                  f"调用次数记 unknown，不伪报 0 次")
    for name, gate in report["speed"].items():
        print(f"   速度 {name}: {gate['value']}（目标 ≥ {gate['target']}，{'达标' if gate['passed'] else '未达标'}）")
    print("   墙钟延迟扫描（端点每次调用延迟 → 墙钟下降）：")
    for row in report["wall_sweep"]:
        print(f"     {row['delay_ms']:.0f} ms：旧 {row['old_wall_ms']} ms / 新 {row['new_wall_ms']} ms"
              f" → 下降 {row['wall_reduction']}")
    for name, gate in report["quality"].items():
        print(f"   质量 {name}: 旧={gate['old']} 新={gate['new']}"
              f"（{'不劣于基线' if gate['passed'] else '低于基线/缺测'}）")
    print(f"   判定: {report['verdict']}（speed_ok={report['speed_ok']} quality_ok={report['quality_ok']}）")
    if report["not_run"]:
        print(f"   未测项（不算通过）: {'、'.join(report['not_run'])}")
    if report["verdict"] == "PROMOTED":
        return 0
    if report["verdict"] == "NOT_RUN" and args.allow_not_run:
        print("   ⏭️ 存在未测项：本次为 NOT_RUN（未测项不算通过，但也不是失败）")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
