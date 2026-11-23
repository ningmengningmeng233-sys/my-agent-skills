#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉环境体检（工单 P0-3）：无收费地检查配置、SDK、模型、权威包与参考图。

设计口径：
- **默认不发起任何收费生成调用**，只读配置与本地文件；
- 密钥只报告「已配置 / 未配置」，**绝不输出密钥值**；
- 网络不可用或 SDK 不支持只读查询时返回 `NETWORK_UNVERIFIED`，**不得冒充通过**；
- 权威包按 manifest 逐文件校验；缺图/漂移一律如实报出。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))

from engine.style_registry import resolve_style_record  # noqa: E402
from engine import visual_request_chain as _chain  # noqa: E402

CONFIGURED = "已配置"
NOT_CONFIGURED = "未配置"
NETWORK_UNVERIFIED = "NETWORK_UNVERIFIED"
SDK_MISSING = "SDK_MISSING"
SDK_PRESENT = "SDK_PRESENT"


def _sdk_state() -> str:
    """只检查 SDK 是否可导入，不做任何网络调用。"""
    try:
        import google.genai  # noqa: F401
    except ImportError:
        return SDK_MISSING
    return SDK_PRESENT


def _pack_report(style_name: str = "仙侠国风电影CG") -> dict:
    record = resolve_style_record(style_name)
    if record is None:
        return {"present": False, "style": style_name, "integrity_ok": False,
                "issues": [f"风格 {style_name!r} 不在登记表中"], "root": "", "file_count": 0}
    from engine.style_registry import pack_root

    root = Path(pack_root(record.pack_slug, record.pack_version))
    if not root.is_dir():
        return {"present": False, "style": style_name, "integrity_ok": False,
                "issues": [f"权威风格包不存在：{record.pack_slug}@{record.pack_version}"],
                "root": str(root), "file_count": 0}
    issues = _chain.stylepack_integrity_issues(root, require_images=True)
    from engine.stylepack_verify import iter_pack_files

    return {
        "present": True,
        "style": record.name,
        "version": record.pack_version,
        "root": str(root),
        "file_count": sum(1 for _ in iter_pack_files(root)),
        "integrity_ok": not issues,
        "issues": issues,
    }


def _config_report() -> dict:
    """只报告是否已配置；绝不读出或输出密钥值。"""
    from engine.common import load_env

    env_path = None
    try:
        env_path = load_env()
    except Exception:  # noqa: BLE001 - 体检不得因配置源异常中断
        env_path = None
    from engine.image_executor import executor_is_configured

    configured = executor_is_configured()
    return {
        "api_key": CONFIGURED if configured else NOT_CONFIGURED,
        "env_source": Path(env_path).name if env_path else "",
    }


def _model_report() -> dict:
    from engine.gemini_image import DEFAULT_IMAGE_MODEL, DEFAULT_QC_MODEL

    return {
        "generation": {"model": DEFAULT_IMAGE_MODEL},
        "qc": {"model": DEFAULT_QC_MODEL},
    }


def _reference_report(lib: Path | None) -> dict:
    """参考图解析能力：权威包能否给出可发送参考（不生成、不收费）。"""
    report: dict = {"stylepack_root": _pack_report()["root"], "resolvable": False, "detail": ""}
    if lib is None:
        report["detail"] = "未提供资产库：跳过参考图解析检查"
        return report
    plan_path = Path(lib) / "视觉资产" / "计划.toml"
    if not plan_path.is_file():
        report["detail"] = "尚无视觉计划：跳过参考图解析检查"
        return report
    try:
        from engine.visual_schema import load_visual_plan
        from engine.visual_request_chain import compile_for_library

        plan = load_visual_plan(plan_path)
        target = next((t for t in plan.sorted_tasks() if not t.depends_on), None)
        if target is None:
            report["detail"] = "计划中没有无依赖任务：跳过参考图解析检查"
            return report
        compiled = compile_for_library(target, Path(lib), plan)
        report["resolvable"] = True
        report["sample_task"] = target.task_id
        report["reference_count"] = len(compiled.resolved)
        report["omitted_count"] = len(compiled.omitted)
        report["prompt_chars"] = len(compiled.request.prompt)
        report["references"] = [r.to_manifest() for r in compiled.resolved]
    except Exception as e:  # noqa: BLE001 - 体检需报告原因而非崩溃
        report["detail"] = f"{type(e).__name__}: {getattr(e, 'code', '')} {e}"
    return report


def _model_availability(configured: bool) -> dict:
    """模型可用性预检：只在已配置时尝试只读查询；不可用即 NETWORK_UNVERIFIED。"""
    if not configured:
        return {"status": NOT_CONFIGURED, "detail": "未配置密钥，未做模型可用性查询"}
    try:
        from google import genai  # noqa: F401  # 延迟导入：无 SDK 时不影响体检
    except ImportError:
        return {"status": SDK_MISSING, "detail": "未安装 Google SDK，无法预检模型"}
    return {"status": NETWORK_UNVERIFIED,
            "detail": "未发起网络查询（默认不产生任何费用与外部请求）"}


def run_doctor(lib: Path | str | None = None) -> dict:
    """执行体检并返回结构化报告（密钥只报状态，绝不含密钥值）。"""
    pack = _pack_report()
    config = _config_report()
    models = _model_report()
    configured = config["api_key"] == CONFIGURED
    report = {
        "authoritative_pack": pack,
        "api_key": config["api_key"],
        "env_source": config["env_source"],
        "sdk": _sdk_state(),
        "generation": models["generation"],
        "qc": models["qc"],
        "model_availability": _model_availability(configured),
        "references": _reference_report(Path(lib) if lib else None),
        # 明确声明：本命令不发收费调用
        "billable_calls": 0,
    }
    report["ok"] = bool(pack["present"] and pack["integrity_ok"])
    return report


def _render(report: dict) -> str:
    pack = report["authoritative_pack"]
    lines = [
        "🩺 视觉环境体检（不发收费调用）",
        "",
        f"权威风格包：{'存在' if pack['present'] else '缺失'}"
        f"（{pack.get('style', '')}@{pack.get('version', '')}，{pack.get('file_count', 0)} 文件）",
        f"  完整性：{'通过' if pack['integrity_ok'] else '失败'}",
    ]
    for issue in pack.get("issues", []):
        lines.append(f"    - {issue}")
    lines += [
        f"密钥：{report['api_key']}" + (f"（来源 {report['env_source']}）" if report.get('env_source') else ""),
        f"Google SDK：{report['sdk']}",
        f"生成模型：{report['generation']['model']}",
        f"质检模型：{report['qc']['model']}",
        f"模型可用性：{report['model_availability']['status']}"
        f"（{report['model_availability']['detail']}）",
    ]
    refs = report["references"]
    if refs.get("resolvable"):
        lines.append(f"参考图解析：通过（样例 {refs.get('sample_task', '')}，"
                     f"选中 {refs.get('reference_count', 0)} 张，"
                     f"省略 {refs.get('omitted_count', 0)} 张，"
                     f"提示词 {refs.get('prompt_chars', 0)} 字）")
    else:
        lines.append(f"参考图解析：{refs.get('detail', '未检查')}")
    lines += ["", f"结论：{'通过' if report['ok'] else '需修复'}", ""]
    return "\n".join(lines)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="视觉环境体检（不发收费调用）")
    parser.add_argument("--lib", help="资产库（用于参考图解析检查）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    report = run_doctor(Path(args.lib) if args.lib else None)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_render(report))
    return 0 if report["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
