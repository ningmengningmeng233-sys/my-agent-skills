#!/usr/bin/env python3
"""资产库运维：status（断点定位+产物统计）/ init（手动初始化，E2）/ backup（时间戳 zip）/ restore / verify（验证收口，ADR-0079/0080）/ migrate（老库迁移，ADR-0084）。"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(SKILL_DIR))

# 需在 sys.path 就绪后导入：`python scripts/maintenance.py` 与
# `python -c "from scripts.maintenance import …"` 两种调用都要能解析 engine 包
from engine.project_settings import MODE_LABELS  # noqa: E402

TYPES = ("人物", "场景", "道具")


def count_sections(p: Path) -> int:
    if not p.is_file():
        return 0
    return len(re.findall(r"^## \d{3}\. ", p.read_text(encoding="utf-8"), re.M))


def collect_status(lib: Path) -> dict:
    """按 SKILL.md 断点定位表判定已完成步骤与产物统计（只读，不编造）。"""
    seg_dirs = [d for d in (lib / "原文段").glob("*") if d.is_dir()] if (lib / "原文段").is_dir() else []
    segments = sum(1 for d in seg_dirs for _ in d.glob("*.md"))
    registries = list(lib.glob("*-登记表.md"))
    seg_products = sum(1 for d in (lib / "段产物").glob("*") if d.is_dir()) if (lib / "段产物").is_dir() else 0
    desc = {t: count_sections(lib / "描述" / f"{t}描述.md") for t in TYPES}
    base = {t: count_sections(lib / "提示词" / "基础" / f"{t}提示词.md") for t in TYPES}
    deriv = {t: count_sections(lib / "提示词" / "衍生" / f"{t}提示词.md") for t in TYPES}
    sched = lib / "提示词" / "调度清单.md"
    sched_rows = 0
    if sched.is_file():
        sched_rows = sum(
            1 for l in sched.read_text(encoding="utf-8").splitlines()
            if l.strip().startswith("|") and not l.strip().startswith("|---") and not l.strip().startswith("| 类型")
        )
    done = []
    if segments:
        done.append("1 拆段")
    if registries:
        done.append("1.5 登记表")
    if seg_products:
        done.append("2 段级提取")
    if (lib / "反推_最终.md").is_file():
        done.append("3 聚合裁决")
    if any(desc.values()):
        done.append("4 描述补全")
    if any(base.values()):
        done.append("5 基础提示词")
    if any(deriv.values()):
        done.append("6 衍生提示词")
    next_step = "空库，将从步骤 1 开始"
    if any(deriv.values()):
        next_step = "步骤 7 完整性检查（或步骤 8 完成报告）"
    elif any(base.values()):
        next_step = "步骤 6 衍生提示词"
    elif any(desc.values()):
        next_step = "步骤 5 基础提示词"
    elif (lib / "反推_最终.md").is_file():
        next_step = "步骤 4 描述补全"
    elif seg_products:
        next_step = "步骤 3 聚合裁决"
    elif registries:
        next_step = "步骤 2 段级提取"
    elif segments:
        next_step = "步骤 1.5 全局登记表"
    return {
        "segments": segments,
        "registry": registries[0].name if registries else "",
        "final": (lib / "反推_最终.md").is_file(),
        "desc": desc,
        "base": base,
        "deriv": deriv,
        "sched_rows": sched_rows,
        "done": done,
        "next_step": next_step,
    }


def do_init(lib: Path, input_source: Path | None, genre: str, style: str,
            force: bool = False, output_root: str | None = None,
            sources: list[Path] | None = None, source_order_file: Path | None = None) -> int:
    """手动初始化入口（E2）：原子写入 .start-state.json，供非交互/手动主机使用。

    字段与 ADR-0075 开始界面一致（input_source/genre/style/emphasis_hash/output_root/
    last_confirmed_at），使后续 verify 的签发门（E1：源可读 + 画风可核验）成立。
    只记录真实存在的原始输入；已存在且参数不同的状态文件必须显式 --force 才覆盖。

    多文件项目或 DOCX/EPUB 输入（规格 4）：先规范化，input_source 记录规范化原文，
    另记 source_manifest_path/source_manifest_hash/source_order_file 供指纹追溯。
    """
    from engine import style as style_lib
    from engine.source_normalizer import (
        SourceNormalizeError, needs_normalization, normalize_sources, normalized_dir, write_normalized,
    )

    lib = Path(lib)
    declared = [Path(p) for p in (sources or [])]
    if not declared and input_source is not None and str(input_source).strip():
        declared = [Path(input_source)]
    if not declared:
        print("❌ 缺少输入源：请用 --input-source（单文件）或 --source（可重复，多文件项目）")
        return 1
    for src in declared:
        if not src.is_file():
            print(f"❌ 原始输入不存在: {src}（init 只记录真实文件，不预支源证据）")
            return 1

    normalized_meta: dict[str, str] = {}
    if needs_normalization(declared, source_order_file):
        try:
            result = normalize_sources(declared, source_order_file)
        except SourceNormalizeError as e:
            print(f"❌ 输入规范化失败（{e.code}）：{e.detail}")
            return 1
        out = write_normalized(result, normalized_dir(lib))
        src = Path(out["markdown"])
        normalized_meta = {
            "source_manifest_path": str(Path(out["manifest"]).resolve()),
            "source_manifest_hash": hashlib.sha256(Path(out["manifest"]).read_bytes()).hexdigest(),
            "source_order_file": str(source_order_file.resolve()) if source_order_file else "",
        }
        print(f"🔤 已规范化 {len(declared)} 个来源 → {len(result.units)} 个段落"
              f"（顺序来源: {result.manifest['order_source']}）")
    else:
        src = declared[0]

    src = src.resolve()
    genre = (genre or "").strip()
    if not genre:
        print("❌ 缺少 --genre（题材）")
        return 1
    style = (style or "").strip()
    if not style:
        print("❌ 缺少 --style（画风名/编号，如 A2 或 仙侠奇幻风）")
        return 1
    card = style_lib.find_card_by_name(style)
    style_card = str(card.get("id", "")) if card else ""
    style_mode = "card" if card else "free"
    if card:
        print(f"🎨 画风: {style_card} {card.get('name', '')}（输入「{style}」）")
    else:
        print(f"⚠️ 画风「{style}」未命中画风库卡片 → 按自由描述处理（无分层/无风格包/无负面并集）；"
              "如属笔误请改用卡片名或编号重跑 init——降级必须可见，不得静默丢画风锁")
    lib.mkdir(parents=True, exist_ok=True)
    emphasis_path = lib / "强调清单.md"
    emphasis_hash = ("sha256:" + hashlib.sha256(emphasis_path.read_bytes()).hexdigest()[:16]
                     if emphasis_path.is_file() else "sha256:none")
    payload = {
        "input_source": str(src),
        "genre": genre,
        "style": style,
        "style_card": style_card,
        "style_mode": style_mode,
        "emphasis_hash": emphasis_hash,
        "output_root": str(Path(output_root).resolve()) if output_root else str(lib.resolve()),
        "last_confirmed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        **normalized_meta,
    }
    state_path = lib / ".start-state.json"
    if state_path.is_file():
        try:
            old = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        if not isinstance(old, dict):
            old = {}
        same = all(old.get(k) == payload[k] for k in ("input_source", "genre", "style", "output_root"))
        if same:
            print(f"ℹ️ .start-state.json 与本次参数一致，无需改写: {state_path}")
            return 0
        if not force:
            print(f"❌ .start-state.json 已存在且参数不同: {state_path}")
            print(f"   现有: 源={old.get('input_source')}｜题材={old.get('genre')}｜画风={old.get('style')}")
            print(f"   本次: 源={payload['input_source']}｜题材={genre}｜画风={style}")
            print("   确认覆盖请加 --force（覆盖会改变源证据，已签发交付清单将立即失效）")
            return 1
    tmp = state_path.with_name(".start-state.json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, state_path)
    print(f"✅ 启动状态已写入: {state_path}")
    print(f"   源: {payload['input_source']}（须保持原位；源变更会使交付清单立即失效）")
    if normalized_meta:
        print(f"   来源清单: {normalized_meta['source_manifest_path']}")
        print("   原始来源文件同样须保持原位；任一源变更都会使交付清单立即失效")
    print("   下一步: python scripts/split_segments.py --lib <资产库>")
    return 0


def do_backup(lib: Path, out: Path | None = None) -> Path:
    if not lib.is_dir():
        raise FileNotFoundError(f"资产库不存在: {lib}")
    out_dir = out if out else lib.parent / "资产库备份"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_path = out_dir / f"资产库备份-{stamp}.zip"
    n = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(lib.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                zf.write(p, str(p.relative_to(lib.parent)))
                n += 1
    print(f"✅ 备份完成: {zip_path}（{n} 个文件，{zip_path.stat().st_size} 字节）")
    return zip_path


def do_restore(zip_path: Path, lib: Path, force: bool = False) -> int:
    if not zip_path.is_file():
        print(f"❌ 备份文件不存在: {zip_path}")
        return 1
    root = lib.name
    if lib.exists() and any(lib.iterdir()) and not force:
        print(f"❌ 目标资产库非空: {lib}；覆盖恢复请加 --force")
        return 1
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            parts = Path(name).parts
            if not parts or parts[0] != root or ".." in parts:
                print(f"❌ 备份包含越界路径，已中止: {name}")
                return 1
        zf.extractall(lib.parent)
    print(f"✅ 恢复完成: {len(zf.namelist())} 个文件 → {lib}")
    return 0


def cmd_status(lib: Path, as_json: bool = False) -> int:
    from engine.pipeline_state import NEXT_COMMAND, collect_pipeline_state
    from io import StringIO
    import contextlib
    s = collect_status(lib)
    has_env = bool(_has_llm_config())
    # 同一推导结果驱动文本与 JSON：已完成步骤/下一步均来自状态机，不再各算各的
    state = collect_pipeline_state(lib, has_config=has_env)
    next_cmd = state["next_command"].replace("<资产库>", str(lib))
    done, next_step = _describe_from_state(state["state"], s)
    payload = {**s, "pipeline_state": state["state"], "reason_code": state["reason_code"],
               "evidence_path": state["evidence_path"], "next_command": next_cmd,
               "data_delivery": state["data_delivery"], "visual_delivery": state["visual_delivery"],
               "visual_stats": state["visual_delivery"].get("counts", {}),
               "compiler_stages": state.get("compiler_stages", {}),
               "done": done, "next_step": next_step}
    if as_json:
        buf = StringIO()
        with contextlib.redirect_stdout(buf):
            pass
        # stdout 必须是单一合法 JSON：不夹杂普通日志
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 0
    print("🔍 资产库状态")
    print(f"  资产库: {lib}")
    print("  已有文件统计: "
          f"段文件 {s['segments']}｜登记表 {s['registry'] or '无'}｜反推总表 {'有' if s['final'] else '无'}｜"
          + " / ".join(f"{k}{s[k]}" for k in ("desc", "base", "deriv")) + f"｜调度清单 {s['sched_rows']} 行")
    print(f"  已完成步骤: {'、'.join(done) if done else '无'}")
    print(f"  下一步: {next_step}")
    print(f"  流水线状态: {state['state']}（{state['reason_code']}）| 下一命令: {next_cmd}")
    print(f"  资产资料交付: {state['data_delivery']['state']}")
    visual = state["visual_delivery"]
    print(f"  视觉资产交付: {_visual_delivery_label(visual['state'])}"
          + (f"（阶段 {visual['stage']}）" if visual.get("stage") else ""))
    stages = state.get("compiler_stages", {})
    if stages:
        print("  编译链阶段: " + " → ".join(
            f"{name}[{item.get('state', '')}]" for name, item in stages.items()))
        for note in _stage_notes(stages):
            print(f"    ↳ {note}")
    _ = NEXT_COMMAND
    return 0


# 未完成阶段才展开 detail：DONE/NOT_APPLICABLE 是合法状态，逐条展开只会刷屏
_STAGE_UNFINISHED = ("PENDING", "BLOCKED")


def _stage_notes(stages: dict) -> list[str]:
    """未完成阶段的「名字: detail」清单（签发格属视觉线，靠 detail 与资料线区分）。"""
    return [f"{name}: {item['detail']}" for name, item in stages.items()
            if item.get("state") in _STAGE_UNFINISHED and item.get("detail")]


def _visual_delivery_label(state: str) -> str:
    """视觉交付线的中文展示（规格 9）：未启用不得显示为失败。"""
    return {
        "NOT_ENABLED": "未启用（可选模块，不影响资产资料交付）",
        "PLANNED": "已规划未生成（待生成）",
        "PARTIAL": "部分完成",
        "DELIVERED": "已交付",
    }.get(state, state)


def do_migrate_visual(lib: Path, style: str, backup: bool = True) -> int:
    """`migrate-visual`：旧视觉产物非破坏迁移（规格 10；不覆盖任何旧文件）。"""
    sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
    from engine.visual_migration import MigrationError, migrate_legacy_visuals

    try:
        result = migrate_legacy_visuals(lib, style, backup=backup)
    except MigrationError as e:
        print(f"❌ 迁移失败（{e.code}）：{e.detail}")
        return 1
    if result["already_migrated"]:
        print(f"ℹ️ 该库已完成旧视觉产物迁移（登记哈希 {result['legacy_index_hash'][:16]}…），幂等跳过")
        return 0
    print(f"✅ 旧视觉产物迁移完成：{lib}")
    if result["backup"]:
        print(f"   备份: {result['backup']}（旧文件未覆盖，可随时恢复）")
    print(f"   旧风格: {result['legacy_style'] or '（未记录）'}｜建议映射: "
          f"{result['suggested_style'] or '无（需用户显式确认）'}")
    print(f"   新风格: {result['style_name']}@{result['style_version']}"
          f"（哈希 {result['style_hash'][:12]}…）")
    print(f"   登记旧图片: {result['legacy_images']} 张（legacy_unreviewed，默认不可作为参考）")
    print(f"   新视觉计划任务: {result['plan_tasks']} 项（依据当前资产索引，非旧提示词）")
    for warning in result.get("warnings") or []:
        print(f"   ⚠️ {warning}")
    print("   下一步: python scripts/maintenance.py visual-plan --lib <资产库> --style <画风>")
    return 0


def _run_script(cmd: list[str]) -> int:
    """子进程执行技能内脚本：原样回显 stdout，stderr 截断回显，返回退出码。"""
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.stderr.strip():
        print(r.stderr.strip()[:400])
    return r.returncode


def do_verify_visual(lib: Path, as_json: bool = False) -> int:
    """`verify-visual`：独立视觉交付门（资料 verify 不调用本门，规格 9）。"""
    return _run_script(
        [sys.executable, str(SKILL_DIR / "scripts" / "check_visual_delivery.py"),
         "--lib", str(lib)] + (["--json"] if as_json else []))


def do_build_ledger(lib: Path, as_json: bool = False) -> int:
    """`build-ledger`：由资产索引建唯一类型化总账与库级依赖图（工单 2/8）。

    总账是机器层唯一库存源：四表、守恒门、提示词编译与依赖图都只读它，不再各自解析 Markdown。
    """
    sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    index_path = lib / ".process" / "资产索引.json"
    if not index_path.is_file():
        print(f"❌ 资产索引不存在：{index_path}（先跑到步骤 3 聚合裁决）")
        return 1
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"❌ 资产索引不可解析：{e}")
        return 1
    try:
        import importlib

        from engine import artifact_graph as _graph
        from engine import delivery_checks as _checks
        from engine.asset_ledger import load_ledger

        aggregate = importlib.import_module("aggregate")
        aggregate.emit_ledger_and_post(lib, index)
        graph = _graph.build_library_graph(lib)
        graph_path = graph.save(lib)
        ledger = load_ledger(lib)
        report = _checks.coverage_report(lib)
    except Exception as e:  # noqa: BLE001 - 建总账失败必须非零，不得静默留半份产物
        print(f"❌ 建总账失败：{type(e).__name__}: {e}")
        return 1

    by_type: dict[str, int] = {}
    for entry in ledger:
        key = entry.get("asset_type", "")
        by_type[key] = by_type.get(key, 0) + 1
    graph_dict = graph.to_dict()
    payload = {
        "lib": str(lib),
        "entries": len(ledger),
        "by_type": by_type,
        "ledger_path": str(lib / ".process" / "asset-ledger.json"),
        "graph": {"nodes": len(graph_dict["nodes"]), "edges": len(graph_dict["edges"]),
                  "path": str(graph_path)},
        "conservation": {"returncode": report["exit_code"], "counts": report["counts"],
                         "coverage": report["coverage"]},
    }
    if as_json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 0
    print(f"📒 资产总账已建立：{payload['ledger_path']}")
    print(f"   记录 {len(ledger)} 条（" + "／".join(f"{k} {v}" for k, v in sorted(by_type.items())) + "）")
    print(f"   依赖图：{len(graph_dict['nodes'])} 节点 / {len(graph_dict['edges'])} 边 → {graph_path}")
    c = report["counts"]
    print(f"   数量守恒：正式 {c['formal']}｜已产提示词 {c['prompted']}｜"
          f"待审/阻断 {c['pending_or_blocked']}｜违规 {c['violations']}｜覆盖率 {report['coverage']:.1%}")
    for item in report["violations"][:5]:
        print(f"   ⚠️ [{item['type']}] {item['asset_id']} {item['name']}：{item['reason']}")
    # 建总账只管「建成」：守恒违规是质量结论，由 check_asset_coverage / verify 判定并阻断签发，
    # 不在这里用非零退出码冒充构建失败（否则刚聚合完还没出提示词的库会永远「建不起来」）。
    return 0


def do_compile_prompts(lib: Path, *, apply_official: bool = False, as_json: bool = False) -> int:
    """`compile-prompts`：由总账确定性编译提示词（工单 6）。

    默认 **shadow**：只写 `.process/shadow-提示词差异.md` 差异报告，**不覆盖正式产物**；
    只有显式 `--apply` 才把编译结果并入 `提示词/基础`（先备份原文件、只改已存在节的正文）。
    """
    sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
    from engine.asset_ledger import load_ledger
    from engine.prompt_compiler import apply_compiled, run_shadow

    if not load_ledger(lib):
        print(f"❌ 总账不存在或为空：{lib / '.process' / 'asset-ledger.json'}"
              "（先跑 python scripts/maintenance.py build-ledger --lib <资产库>）")
        return 1
    if not apply_official:
        report = run_shadow(lib)
        payload = {"lib": str(lib), "mode": "shadow", "report": report,
                   "official_untouched": True}
        if as_json:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            return 0
        print(f"🌓 shadow 编译完成：{report}（正式提示词未改写）")
        return 0
    result = apply_compiled(lib)
    payload = {"lib": str(lib), "mode": "apply", **result}
    if as_json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 0 if not result["errors"] else 1
    print(f"📝 已并入正式提示词：改写 {result['updated']} 节，新增 {result['added']} 节")
    if result["backup"]:
        print(f"   备份: {result['backup']}")
    for name in result["errors"]:
        print(f"   ❌ {name}")
    return 0 if not result["errors"] else 1


def do_run_visual(lib: Path, batch: int | None = None) -> int:
    """`run-visual`：按当前产品结果派发图片生成（单批数量可调，视图规格不可调）。

    视图规格由 Omni 标准定义，**不作为参数暴露**：只能选择「这次生成几张」，
    不能改「人物要哪几张视图」。模式门、暂停门、密钥门与并发上限全部复用既有内核。
    """
    cmd = [sys.executable, str(SKILL_DIR / "scripts" / "visual_production.py"),
           "run-ready", "--lib", str(lib)]
    if batch is not None:
        cmd += ["--parallel", str(batch)]
    return _run_script(cmd)


def do_visual_doctor(lib: Path | None, as_json: bool = False) -> int:
    """`visual-doctor`：视觉环境体检（配置/SDK/模型/权威包/参考图；不发收费调用）。"""
    cmd = [sys.executable, str(SKILL_DIR / "scripts" / "visual_doctor.py")]
    if lib is not None:
        cmd += ["--lib", str(lib)]
    if as_json:
        cmd.append("--json")
    return _run_script(cmd)


def do_visual_plan(lib: Path, style: str, assets: str | None = None) -> int:
    """`visual-plan`：规划/续建视觉计划并渲染中文阅读稿（工单02-Task7）。

    显式选择「只反推」时拒绝：不创建视觉计划（阶段 1 工单 1.2）。
    """
    sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
    from engine.project_settings import (
        MODE_BLOCKED_DATA_ONLY,
        ProjectSettingsError,
        explicit_visual_mode,
    )

    try:
        mode = explicit_visual_mode(lib)
    except ProjectSettingsError as e:
        print(f"❌ {e.code}：{e.detail}")
        return 1
    if mode == "data_only":
        print(f"❌ 未创建视觉计划（{MODE_BLOCKED_DATA_ONLY}）：本项目已选择「只反推资产、不生图」，"
              "视觉模块不建计划、不调用图片 API。")
        print("   需要视觉任务包：python scripts/maintenance.py configure-visual "
              "--lib <资产库> --mode prompt-only")
        print("   需要直接生成：  python scripts/maintenance.py configure-visual "
              "--lib <资产库> --mode direct-generate")
        return 1
    return _run_script(
        [sys.executable, str(SKILL_DIR / "scripts" / "render_visual_plan.py"),
         "--lib", str(lib), "--style", style] + (["--assets", assets] if assets else []))


# 用户入口用带连字符的人话取值，内部一律用下划线稳定码
VISUAL_MODE_CHOICES = {
    "data-only": "data_only",
    "prompt-only": "prompt_only",
    "direct-generate": "direct_generate",
}


def do_configure_visual(lib: Path, mode: str | None = None, parallel: int | None = None,
                        composition: str | None = None, review_subagents: str | None = None,
                        show: bool = False, as_json: bool = False) -> int:
    """`configure-visual`：设置「这次是否生图 / 并发 / 构图卡模式 / 审核子代理」。

    非交互：不传任何修改参数时只是沿用已存设置（不重新询问、不改写文件）；
    显式传参则覆盖并持久化。
    """
    sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
    from engine.project_settings import (
        ProjectSettings,
        ProjectSettingsError,
        load_project_settings,
        write_project_settings,
    )

    try:
        current = load_project_settings(lib)
    except ProjectSettingsError as e:
        print(f"❌ {e.code}：{e.detail}")
        return 1

    if show or (mode is None and parallel is None and composition is None
                and review_subagents is None):
        payload = {
            "visual_mode": current.visual_mode,
            "max_parallel": current.max_parallel,
            "composition_mode": current.composition_mode,
            "review_subagents": current.review_subagents,
            "schema_version": current.schema_version,
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print(f"🎛️ 项目设置（{lib / '.process' / '项目设置.toml'}）")
        print(f"   是否直接生成图片：{_MODE_LABELS[current.visual_mode]}")
        print(f"   同时生成几张：{current.max_parallel}（可选 1–4）")
        print(f"   构图卡：{'每批先看构图卡' if current.composition_mode == 'preview' else '直接自动生成'}")
        print(f"   审核子代理：{'已开启' if current.review_subagents else '关闭'}")
        if not show:
            print("   ℹ️ 已有设置，直接沿用（要改请显式传 --mode/--parallel/--composition"
                  "/--review-subagents）")
        return 0

    updated = ProjectSettings(
        schema_version=current.schema_version,
        visual_mode=VISUAL_MODE_CHOICES.get(mode, current.visual_mode) if mode else current.visual_mode,
        max_parallel=parallel if parallel is not None else current.max_parallel,
        composition_mode=composition if composition is not None else current.composition_mode,
        review_subagents=(review_subagents == "on") if review_subagents is not None
        else current.review_subagents,
    )
    try:
        path = write_project_settings(lib, updated)
    except ProjectSettingsError as e:
        print(f"❌ {e.code}：{e.detail}")
        return 1
    print(f"✅ 已保存项目设置：{path}")
    print(f"   是否直接生成图片：{_MODE_LABELS[updated.visual_mode]}｜并发 {updated.max_parallel}"
          f"｜构图卡 {updated.composition_mode}"
          f"｜审核子代理 {'开' if updated.review_subagents else '关'}")
    if updated.visual_mode == "data_only":
        print("   视觉模块将不创建计划、不调用图片 API；资料线不受影响。")
    return 0


# 用户可见模式说法的唯一来源在 project_settings（视觉 CLI 也读同一份）
_MODE_LABELS = MODE_LABELS


def _describe_from_state(state: str, stats: dict) -> tuple[list[str], str]:
    """由结构化状态派生已完成步骤与下一步（与 JSON 同源）。"""
    order = ["SOURCE_READY", "SEGMENTED", "REGISTERED", "EXTRACTED", "CATALOGED",
             "PROMPTS_READY", "VERIFIED", "DELIVERED"]
    labels = {
        "SOURCE_READY": "1 拆段", "SEGMENTED": "1.5 登记表", "REGISTERED": "2 段级提取",
        "EXTRACTED": "3 聚合裁决", "CATALOGED": "4 描述补全",
        "PROMPTS_READY": "5/6 提示词", "VERIFIED": "7 完整性检查", "DELIVERED": "8 完成报告",
    }
    next_map = {
        "WAITING_INPUT": "空库，将从步骤 1 开始",
        "SOURCE_READY": "步骤 1 拆段",
        "SEGMENTED": "步骤 1.5 全局登记表",
        "REGISTERED": "步骤 2 段级提取",
        "EXTRACTED": "步骤 3 聚合裁决",
        "CATALOGED": "步骤 5 基础提示词",
        "PROMPTS_READY": "步骤 7 完整性检查（maintenance.py verify）",
        "VERIFIED": "签发交付清单（重跑 maintenance.py verify）",
        "DELIVERED": "已交付（产物与交付清单一致）",
        "BLOCKED": "修复阻断项后重跑受影响步骤（见 evidence_path）",
        "NEEDS_REVIEW": "处理待确认/补充配置后重跑 verify",
        "NEEDS_CONFIG": "配置 OpenAI 兼容接口后重跑生成步骤",
    }
    if state in ("BLOCKED", "NEEDS_REVIEW", "NEEDS_CONFIG", "WAITING_INPUT"):
        done: list[str] = []
        if stats.get("segments"):
            done.append("1 拆段")
        if stats.get("registry"):
            done.append("1.5 登记表")
        if stats.get("final"):
            done.append("3 聚合裁决")
        return done, next_map[state]
    done = []
    for st in order:
        done.append(labels[st])
        if st == state:
            break
    return done, next_map.get(state, state)


def _has_llm_config() -> bool:
    """只读动作不受 NEEDS_CONFIG 阻断；生成类步骤由调用方判断。

    配置来源只认 engine.common（含 ASSET_ENV_PATH）；这里自己列候选文件会与运行时口径分叉。
    """
    from engine.common import env_values
    return bool(env_values()[0].get("OPENAI_API_KEY"))


def resolve_style_from_state(lib: Path) -> str | None:
    """从 .start-state.json 沿用画风（--style 未传时的防绕过兜底，ADR-0080）。"""
    state = lib / ".start-state.json"
    if not state.is_file():
        return None
    try:
        import json
        return (json.loads(state.read_text(encoding="utf-8")).get("style") or "").strip() or None
    except Exception:
        return None


def _write_verify_report(lib: Path, results: list[tuple[str, int]], status: str, extra: list[str]) -> None:
    """写验证报告（失败路径专用：绝不覆盖最后有效的交付清单）。"""
    proc_dir = lib / ".process"
    proc_dir.mkdir(parents=True, exist_ok=True)
    report = proc_dir / "验证报告.md"
    lines = ["# 验证报告（ADR-0084：失败不覆盖最后有效交付清单）", ""]
    for name, code in results:
        lines.append(f"- {name}: {'通过' if code == 0 else '未通过'}（rc={code}）")
    lines.append(f"- 状态判定: {status}")
    lines.extend(f"- {line}" for line in extra)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"📄 验证报告已写入: {report}（未覆盖历史有效交付清单）")


def do_verify(lib: Path, style: str | None = None) -> int:
    """验证收口（ADR-0080/0084 + E1 签发门）：一键跑全部机械检查，全绿签发交付清单，失败写验证报告。

    E1：签发前先核验启动证据（.start-state.json + 原始输入可读 + 画风一致）。
    证据不可核验一律非零退出且绝不生成交付清单——无状态文件不再"跳过画风检查后继续"。
    """
    from engine.pipeline_state import (
        DeliveryRefused, archive_delivery_manifest, build_delivery_manifest, evidence_detail,
        validate_start_evidence,
    )
    final = lib / "反推_最终.md"
    if not final.is_file():
        print(f"❌ 反推_最终.md 不存在: {final}（先跑到步骤 3）")
        return 1
    if not style:
        style = resolve_style_from_state(lib)
        if style:
            print(f"ℹ️ 未传 --style，已沿用启动状态文件画风: {style}")
    evidence_ok, reason, _src, _state_style = validate_start_evidence(lib, (style or "").strip())
    if not evidence_ok:
        detail = evidence_detail(reason, _state_style, style or "")
        print(f"❌ 拒绝签发（{reason}）：{detail}")
        _write_verify_report(lib, [], "BLOCKED", [f"签发门: {reason}（{detail}）"])
        return 1
    checks: list[tuple[str, list[str]]] = [
        ("check_schedule", [sys.executable, str(SKILL_DIR / "scripts" / "check_schedule.py"), str(final), "--lib", str(lib)]
         + (["--style", style] if style else [])),
        ("check_final", [sys.executable, str(SKILL_DIR / "scripts" / "check_final.py"), str(final)]),
    ]
    # ADR-0084 新增：资产索引与场景母版校验纳入 verify
    checks.append(("check_asset_index", [
        sys.executable, str(SKILL_DIR / "scripts" / "check_asset_index.py"),
        "--lib", str(lib),
    ]))
    costume_dir = lib / "服饰设计"
    if costume_dir.is_dir():
        for table in sorted(costume_dir.glob("*/角色服饰标准表.md")):
            std_lib = table.parent / "服饰标准库.md"
            if std_lib.is_file():
                checks.append((f"check_costumes[{table.parent.name}]",
                               [sys.executable, str(SKILL_DIR / "服饰设计" / "scripts" / "check_costumes.py"), str(std_lib), str(table)]))
    results = []
    for name, cmd in checks:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.stdout.strip():
            print(f"  [{name}] {r.stdout.strip()[:400]}")
        if r.stderr.strip():
            print(f"  [{name} stderr] {r.stderr.strip()[:400]}")
        results.append((name, r.returncode))
    # 工单 13：签发契约扩展到可核验编译器事实（数量守恒／总则指纹／依赖图／必需提示词／
    # 视觉审核／包内文件）。检查实现在 engine.delivery_checks，本函数只取结果，不直接依赖任何视觉入口。
    from engine import delivery_checks

    for name, rc, detail in delivery_checks.run_all(lib):
        print(f"  [{name}] {'✅' if rc == 0 else '❌'} {detail[:400]}")
        results.append((name, rc))
    status, rc = judge_status(results, style)
    if status == "NEEDS_REVIEW":
        print("  ⚠️ 画风检查未跑（无 --style 且无启动状态文件）——状态降级为 NEEDS_REVIEW，补 --style 后重跑 verify")
    print("📋 验证收口（交付契约「验证」「状态」栏可直接引用）")
    for name, code in results:
        print(f"  {name}: {'✅ 通过' if code == 0 else '❌ 未通过'}（rc={code}）")
    print(f"  状态判定: {status}")
    if rc == 0:
        s = collect_status(lib)
        counts = {"desc": s["desc"], "base": s["base"], "deriv": s["deriv"], "sched_rows": s["sched_rows"]}
        try:
            manifest = build_delivery_manifest(lib, style, results, counts)
        except DeliveryRefused as e:
            print(f"❌ 拒绝签发（{e.reason_code}）：{e.detail}")
            _write_verify_report(lib, results, "BLOCKED", [f"签发门: {e.reason_code}（{e.detail}）"])
            return 1
        try:
            archived = archive_delivery_manifest(lib)
        except OSError as e:
            print(f"⚠️ 历史清单归档失败（不影响本次签发）: {e}")
            archived = None
        (lib / "交付清单.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"📦 交付清单已签发: {lib / '交付清单.json'}")
        print(f"   画风: {style}｜源指纹: {manifest['source_fingerprint'][:21]}…｜检查: {len(results)} 项全绿")
        if archived:
            print(f"   上一份清单已归档为历史快照: {archived}（快照不作当前有效依据）")
        _clear_verify_report(lib)
    else:
        _write_verify_report(lib, results, status, [])
    return rc


def _clear_verify_report(lib: Path) -> None:
    """签发成功即清掉上一次失败的报告。

    文档口径是「`.process/验证报告.md` 存在 ⇒ 最近一次 verify 失败」（README/FAQ/错误处理矩阵/
    pipeline_state 的 BLOCKED 指引都按这个读法写）。成功路径若留着旧报告，就会出现
    「报告说 BLOCKED、交付清单说 DELIVERED」的同库矛盾（2026-09-21 两座已签发库实测都中招）。
    """
    report = lib / ".process" / "验证报告.md"
    if report.is_file():
        report.unlink()
        print("🗑️ 已清除上一次失败留下的验证报告（.process/验证报告.md 存在＝最近一次 verify 失败）")


def do_migrate(lib: Path) -> int:
    """老库迁移（ADR-0084）：只读识别旧结构，先备份再生成索引与母版框架。"""
    from aggregate import write_asset_index
    from engine.continuity import write_scene_master
    lib = Path(lib)
    if not lib.is_dir():
        print(f"❌ 资产库不存在: {lib}")
        return 1
    final = lib / "反推_最终.md"
    if not final.is_file():
        print(f"❌ 反推_最终.md 不存在: {final}（老库无可迁移内容）")
        return 1
    # 1. 先备份
    zip_path = do_backup(lib)
    # 2. 解析旧表：从 Markdown 四表重建 verdicts（缺证据标制作提案/待核）
    try:
        text = final.read_text(encoding="utf-8")
    except OSError as e:
        print(f"❌ 无法读取反推文件: {e}")
        return 1
    from generate_prompts import parse_asset_table
    verdicts: list[dict] = []
    summaries: list[dict] = []
    gid = 0
    for t in ("人物", "场景", "道具", "服装"):
        for cells in parse_asset_table(text, f"{t}资产" if t != "服装" else "服装妆造"):
            gid += 1
            name = cells[1].strip() if len(cells) > 1 else ""
            if not name:
                continue
            bind = cells[2].strip() if t == "服装" and len(cells) > 2 else ""
            verdicts.append({
                "组ID": f"M{gid:02d}", "类型": t, "主名": name,
                "等级": cells[-2].strip() if len(cells) >= 2 else "",
                "合并": "", "别名": cells[2].strip() if t == "人物" and len(cells) > 2 else "",
                "绑定": bind, "处理": "保留", "备注": "迁移：旧字段缺证据，标制作提案/待核",
            })
    # 章节摘要（尽力解析）
    in_use = False
    for line in text.splitlines():
        if line.startswith("## ") and "分集/章节" in line:
            in_use = True
            continue
        if in_use and (line.startswith("## ") or line.startswith("# ")):
            break
        if in_use and line.strip().startswith("|") and not line.strip().startswith("|---"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 6 and cells[0] != "章节":
                summaries.append({"章节": cells[0], "概要": cells[1], "人物": cells[2], "场景": cells[3], "道具": cells[4], "服装": cells[5]})
    groups_by_id = {v["组ID"]: {"组ID": v["组ID"], "成员": []} for v in verdicts}
    index_path = write_asset_index(lib, verdicts, groups_by_id, summaries, len(summaries),
                                   source_fingerprint="sha256:migrated", needs=[])
    import json as _json
    data = _json.loads(index_path.read_text(encoding="utf-8"))
    # 迁移语义：缺证据旧字段不得假定为剧本事实
    for a in data["assets"]:
        if not a.get("evidence"):
            a["fact_level"] = "制作提案"
            a["status"] = "NEEDS_REVIEW"
    index_path.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    master_path = write_scene_master(lib, data, summaries)
    print(f"✅ 迁移完成（已备份 {zip_path}，原反推表与提示词未覆盖）")
    print(f"   资产索引: {index_path} | 场景母版: {master_path}")
    print("   说明：缺证据旧字段已标 制作提案/待核；悬空服装绑定请人工在待确认中处理。")
    return 0


def judge_status(results: list[tuple[str, int]], style: str | None) -> tuple[str, int]:
    """验证状态判定（ADR-0080）：全过且有画风检查=DONE；全过但画风检查未跑=NEEDS_REVIEW；任一失败=BLOCKED。"""
    if any(rc != 0 for _, rc in results):
        return "BLOCKED", 1
    if not style:
        return "NEEDS_REVIEW", 1
    return "DONE", 0


def do_autofix(lib: Path, dry_run: bool = False, as_json: bool = False) -> int:
    """机械化修复（工单 T12）：幂等 + 留痕；修完打印复检结果。"""
    from engine import autofix
    report = autofix.run(lib, dry_run=dry_run)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        mode = "（dry-run，未落盘）" if dry_run else ""
        print(f"机械化修复{mode} · {lib}")
        print(f"  修复处数: {report['fixed_count']}")
        for it in report["items"]:
            print(f"  - {it['file']} [{it['rule']}] {it['before_hash'][:8]} → {it['after_hash'][:8]}")
        for u in report["unfixable"]:
            print(f"  ⚠️ 无法机械修复: {u['file']} {u['rules']}（保持原状，需人工）")
    if not dry_run:
        recheck = autofix.recheck(lib)
        print(f"  复检: {'✅ 0 项待修' if recheck['failures'] == 0 else '❌ 残留 ' + str(recheck['failures']) + ' 项: ' + '; '.join(recheck['detail'][:5])}")
        return 0 if recheck["failures"] == 0 else 1
    return 0


def do_doctor(lib: Path, as_json: bool = False) -> int:
    """统一体检（工单 T10）：每项带修法，零收费调用，密钥只报已配置/未配置。"""
    from engine import doctor
    checks = doctor.run_checks(lib)
    if as_json:
        print(json.dumps({"lib": str(lib), "checks": checks,
                          "ok": all(c["ok"] for c in checks)}, ensure_ascii=False, indent=2))
        return 0
    print(f"统一体检 · {lib}")
    for c in checks:
        mark = "✅" if c["ok"] else "❌"
        print(f"  {mark} {c['name']}: {c['detail']}")
        if not c["ok"]:
            print(f"      ↳ 修法: {c['fix']}")
    return 0 if all(c["ok"] for c in checks) else 1


def do_perf_report(lib: Path, as_json: bool = False) -> int:
    """耗时账本聚合报告（工单 T5）：按步骤出调用数/耗时/token/重试/失败。零收费调用。"""
    from engine import perf_ledger
    summary = perf_ledger.summarize(lib)
    if as_json:
        print(json.dumps({"lib": str(lib), "steps": summary}, ensure_ascii=False, indent=2))
        return 0
    if not summary:
        print(f"（{lib / '.process' / '耗时账本.jsonl'} 不存在或为空——还没有可聚合的账目）")
        return 0
    print(f"耗时账本报告 · {lib}")
    print(f"{'步骤':<14}{'调用':>6}{'总耗时ms':>12}{'均耗时ms':>10}{'prompt_tok':>12}{'completion_tok':>15}{'重试':>6}{'失败':>6}")
    for step, s in summary.items():
        print(f"{step:<14}{s['calls']:>6}{s['total_ms']:>12}{s['avg_ms']:>10}"
              f"{str(s['prompt_tokens']):>12}{str(s['completion_tokens']):>15}{s['retries']:>6}{s['failures']:>6}")
    by_role: dict = {}
    for s in summary.values():
        for role, rb in (s.get("by_role") or {}).items():
            agg = by_role.setdefault(role, {"calls": 0, "total_ms": 0})
            agg["calls"] += rb["calls"]
            agg["total_ms"] += rb["total_ms"]
    if len(by_role) > 1 or "mechanical" in by_role:
        print("  按角色（T13 分档）:")
        for role, agg in sorted(by_role.items()):
            print(f"    {role:<12}调用 {agg['calls']:>5}  总耗时 {agg['total_ms']:>8} ms")
    _warn_idle_mechanical_node(by_role)
    return 0


def _warn_idle_mechanical_node(by_role: dict) -> None:
    """配了 mechanical 节点却零调用就挑明，免得以为分档已经生效。

    当前架构里没有 mechanical 档的生产调用点——全部 7 个 LLM 调用点都是判断活，
    真正的机械修由 `engine/autofix.py` 本地完成、不经过模型。理由与守卫见
    `测试/unit/test_tiered_models.py::test_no_production_callsite_uses_mechanical_role`。
    """
    if (by_role.get("mechanical") or {}).get("calls"):
        return
    try:
        from engine import llm_pool
        idle = [n.name for n in llm_pool.load_nodes() if "mechanical" in (n.roles or ())]
    except Exception:
        return                      # 读不到配置不算错误，静默跳过（报告本身不受影响）
    if idle:
        print(f"  ⚠️  节点 {', '.join(idle)} 声明了 mechanical，但账本里零调用："
              "当前架构的机械修由 autofix 本地完成、不经过模型，这个节点不会产生收益。")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="资产库运维（status/init/backup/restore/verify/migrate/visual-plan/verify-visual/migrate-visual/visual-doctor）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_status = sub.add_parser("status", help="断点定位 + 产物统计")
    p_status.add_argument("--lib", required=True)
    p_status.add_argument("--json", action="store_true", help="机器可读 JSON 输出（ADR-0084）")
    p_init = sub.add_parser("init", help="手动初始化：原子写入 .start-state.json（E2）")
    p_init.add_argument("--lib", required=True)
    p_init.add_argument("--input-source", help="原始输入单文件（txt/md/docx/epub，必须真实存在）")
    p_init.add_argument("--source", action="append", default=[],
                        help="多文件项目源（可重复；与 --input-source 二选一）")
    p_init.add_argument("--source-order-file", help="多文件顺序清单：每行一个相对路径（相对清单所在目录）")
    p_init.add_argument("--genre", required=True, help="题材（如 古风/玄幻）")
    p_init.add_argument("--style", required=True, help="画风名/编号（如 A2 或 仙侠奇幻风）")
    p_init.add_argument("--output-root", help="产物根目录（默认 = --lib）")
    p_init.add_argument("--force", action="store_true", help="覆盖已存在且参数不同的状态文件")
    p_backup = sub.add_parser("backup", help="打包资产库为时间戳 zip")
    p_backup.add_argument("--lib", required=True)
    p_backup.add_argument("--out", help="备份输出目录（默认 <资产库>../资产库备份）")
    p_restore = sub.add_parser("restore", help="从备份恢复资产库")
    p_restore.add_argument("--zip", required=True)
    p_restore.add_argument("--lib", required=True)
    p_restore.add_argument("--force", action="store_true", help="目标非空时覆盖恢复")
    p_verify = sub.add_parser("verify", help="验证收口：一键跑全部机械检查，输出交付契约验证栏")
    p_verify.add_argument("--lib", required=True)
    p_verify.add_argument("--style", help="画风（未传时自动沿用 .start-state.json）")
    p_migrate = sub.add_parser("migrate", help="老库迁移：备份后生成资产索引与场景母版（ADR-0084）")
    p_migrate.add_argument("--lib", required=True)
    p_vmigrate = sub.add_parser("migrate-visual",
                                help="旧视觉产物非破坏迁移：备份→登记旧图→依资产索引新建计划")
    p_vmigrate.add_argument("--lib", required=True)
    p_vmigrate.add_argument("--style", required=True, help="目标风格中文正式名")
    p_vmigrate.add_argument("--no-backup", action="store_true", help="跳过备份（不推荐）")
    p_vverify = sub.add_parser("verify-visual",
                               help="视觉交付门（独立于资料交付；视觉未启用时视为通过）")
    p_vverify.add_argument("--lib", required=True)
    p_vverify.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    p_vplan = sub.add_parser("visual-plan",
                             help="规划/续建视觉计划并渲染阅读稿（可选下游模块，不影响资料签发）")
    p_vplan.add_argument("--lib", required=True)
    p_vplan.add_argument("--style", required=True, help="风格中文正式名（如 仙侠国风电影CG）")
    p_vplan.add_argument("--assets", help="只规划这些资产 ID（逗号分隔）")
    p_vdoctor = sub.add_parser("visual-doctor",
                               help="视觉环境体检：配置/SDK/模型/权威包/参考图（不发收费调用）")
    p_vdoctor.add_argument("--lib", help="资产库（用于参考图解析检查）")
    p_vdoctor.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    p_vconfig = sub.add_parser("configure-visual",
                               help="设置是否生图/并发/构图卡/审核子代理（非交互，可 --show）")
    p_vconfig.add_argument("--lib", required=True)
    p_vconfig.add_argument("--mode", choices=tuple(VISUAL_MODE_CHOICES),
                           help="data-only 只反推｜prompt-only 只准备提示词｜direct-generate 直接生成")
    p_vconfig.add_argument("--parallel", type=int, help="同时生成几张，1–4（默认 3）")
    p_vconfig.add_argument("--composition", choices=("auto", "preview"),
                           help="auto 直接自动生成｜preview 每批先看构图卡")
    p_vconfig.add_argument("--review-subagents", choices=("on", "off"),
                           help="是否启用两个审核子代理（默认 off）")
    p_vconfig.add_argument("--show", action="store_true", help="只显示当前设置")
    p_vconfig.add_argument("--json", action="store_true", help="配合 --show 输出机器可读 JSON")
    p_ledger = sub.add_parser("build-ledger",
                              help="由资产索引建唯一类型化总账与库级依赖图（编译链入口）")
    p_ledger.add_argument("--lib", required=True)
    p_ledger.add_argument("--json", action="store_true", help="stdout 只输出一个 JSON")
    p_compile = sub.add_parser("compile-prompts",
                               help="由总账确定性编译提示词（默认 shadow，不覆盖正式产物）")
    p_compile.add_argument("--lib", required=True)
    p_compile.add_argument("--shadow", action="store_true",
                           help="只写差异报告，不改写 提示词/（默认行为）")
    p_compile.add_argument("--apply", action="store_true",
                           help="把编译结果并入正式提示词（先备份；只改已存在节的正文）")
    p_compile.add_argument("--json", action="store_true", help="stdout 只输出一个 JSON")
    p_run = sub.add_parser("run-visual",
                           help="按当前产品结果派发图片生成（单批数量可调，视图规格不可调）")
    p_run.add_argument("--lib", required=True)
    p_run.add_argument("--batch", type=int, help="单批同时生成几张，1–4（缺省用项目设置）")
    p_perf = sub.add_parser("perf-report",
                            help="耗时账本聚合报告（按步骤：调用数/耗时/token/重试/失败；零收费）")
    p_perf.add_argument("--lib", required=True)
    p_perf.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    p_doctor = sub.add_parser("doctor",
                              help="统一体检：Python/依赖/.env/节点池/风格包/库可写（只读零收费）")
    p_doctor.add_argument("--lib", help="资产库（用于可写性检查）")
    p_doctor.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    p_autofix = sub.add_parser("autofix",
                               help="五类机械化修复（顿号/镜头体系/三视图/负面锁/层次雷同；幂等+留痕）")
    p_autofix.add_argument("--lib", required=True)
    p_autofix.add_argument("--dry-run", action="store_true", help="只报告不落盘")
    p_autofix.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    args = parser.parse_args()
    if args.cmd == "status":
        return cmd_status(Path(args.lib), as_json=getattr(args, "json", False))
    if args.cmd == "init":
        return do_init(Path(args.lib),
                       Path(args.input_source) if args.input_source else None,
                       args.genre, args.style,
                       force=args.force, output_root=args.output_root,
                       sources=[Path(s) for s in args.source],
                       source_order_file=Path(args.source_order_file) if args.source_order_file else None)
    if args.cmd == "backup":
        do_backup(Path(args.lib), Path(args.out) if args.out else None)
        return 0
    if args.cmd == "verify":
        return do_verify(Path(args.lib), args.style)
    if args.cmd == "migrate":
        return do_migrate(Path(args.lib))
    if args.cmd == "visual-plan":
        return do_visual_plan(Path(args.lib), args.style, args.assets)
    if args.cmd == "verify-visual":
        return do_verify_visual(Path(args.lib), as_json=getattr(args, "json", False))
    if args.cmd == "migrate-visual":
        return do_migrate_visual(Path(args.lib), args.style, backup=not args.no_backup)
    if args.cmd == "visual-doctor":
        return do_visual_doctor(Path(args.lib) if args.lib else None,
                                as_json=getattr(args, "json", False))
    if args.cmd == "configure-visual":
        return do_configure_visual(Path(args.lib), mode=args.mode, parallel=args.parallel,
                                   composition=args.composition,
                                   review_subagents=args.review_subagents,
                                   show=args.show, as_json=getattr(args, "json", False))
    if args.cmd == "build-ledger":
        return do_build_ledger(Path(args.lib), as_json=getattr(args, "json", False))
    if args.cmd == "compile-prompts":
        return do_compile_prompts(Path(args.lib), apply_official=bool(args.apply),
                                  as_json=getattr(args, "json", False))
    if args.cmd == "run-visual":
        return do_run_visual(Path(args.lib), batch=args.batch)
    if args.cmd == "perf-report":
        return do_perf_report(Path(args.lib), as_json=getattr(args, "json", False))
    if args.cmd == "doctor":
        return do_doctor(Path(args.lib) if args.lib else Path.cwd(), as_json=getattr(args, "json", False))
    if args.cmd == "autofix":
        return do_autofix(Path(args.lib), dry_run=bool(args.dry_run), as_json=getattr(args, "json", False))
    return do_restore(Path(args.zip), Path(args.lib), args.force)


if __name__ == "__main__":
    sys.exit(main())
