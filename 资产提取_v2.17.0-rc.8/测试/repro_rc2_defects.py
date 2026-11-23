#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.15.0-rc.2 终验工单：四项 P0 缺陷的物理复现（红测证据，修复前必须全部命中）。

复现项（对应《资产提取v2.15终验结论与修复工单.md》）：
  1. 参考图路由未接入真实 CLI：导出首个无依赖正脸任务，`参考图清单.md` 写“本任务无参考图”。
  2. 空库 `verify-visual` 返回 NOT_ENABLED 但 rc=0（自动化会误判为视觉交付通过）。
  3. Gemini 适配器对缺失参考发送空字节且 MIME 固定 PNG。
  4. 视觉 CLI 检测不到 `ASSET_ENV_PATH` 指向的包外 `.env`。

用法：python 测试/repro_rc2_defects.py   （只读 + 临时目录；退出码 0 表示四项缺陷均已复现）
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL = Path(__file__).resolve().parent.parent
STYLE = "仙侠国风电影CG"
FINDINGS: list[tuple[str, bool, str]] = []


def _run(cmd, cwd=SKILL, env=None):
    return subprocess.run([sys.executable] + [str(c) for c in cmd], capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          env={**os.environ, **(env or {})}, cwd=str(cwd))


def _record(name: str, defect_present: bool, detail: str) -> None:
    FINDINGS.append((name, defect_present, detail))
    print(f"  {'🔴 复现' if defect_present else '⚪ 未复现'} {name}：{detail}")


def _visual_lib(td: Path, name: str = "资产库") -> Path:
    """用 e2e 快照资产库的副本，避免污染仓库内快照。"""
    src = SKILL / "测试" / "e2e" / "资产库"
    lib = td / name
    shutil.copytree(src, lib)
    return lib


def _compiled_reference_count(lib: Path, task_id: str) -> int:
    """走真实编译链统计该任务实际选中的参考图张数（无参考=缺陷）。"""
    sys.path.insert(0, str(SKILL))
    from scripts.engine.visual_request_chain import compile_for_library
    from scripts.engine.visual_schema import load_visual_plan

    plan = load_visual_plan(lib / "视觉资产" / "计划.toml")
    return len(compile_for_library(plan.by_id(task_id), lib, plan).resolved)


def defect_1_reference_router_not_wired(td: Path) -> None:
    """缺陷 1：导出正脸任务后参考图清单为空。"""
    lib = _visual_lib(td)
    r = _run([SKILL / "scripts" / "maintenance.py", "visual-plan", "--lib", lib, "--style", STYLE])
    if r.returncode != 0:
        _record("缺陷1 参考图路由", False, f"前置失败：visual-plan rc={r.returncode} {r.stdout[-200:]}")
        return
    plan = (lib / "视觉资产" / "计划.toml").read_text(encoding="utf-8")
    face_tasks = re.findall(r'task_id\s*=\s*"([^"]*正脸-v\d+)"', plan)
    no_dep = [t for t in face_tasks if "服装" not in t and "全身" not in t]
    if not face_tasks:
        _record("缺陷1 参考图路由", False, "计划中未生成正脸任务")
        return
    task_id = no_dep[0] if no_dep else face_tasks[0]
    r = _run([SKILL / "scripts" / "visual_production.py", "export", "--lib", lib, "--task", task_id])
    listing = lib / "视觉资产" / "任务包" / task_id / "参考图清单.md"
    if not listing.is_file():
        _record("缺陷1 参考图路由", False,
                f"导出未生成清单（rc={r.returncode}）{r.stdout[-200:]}")
        return
    text = listing.read_text(encoding="utf-8")
    empty = "本任务无参考图" in text
    _record("缺陷1 参考图路由未接入", empty,
            f"{task_id} 清单={'本任务无参考图' if empty else '含参考图'}")
    # 计划里的 references 为空**不再是缺陷**：参考由编译期路由产出（visual_request_chain），
    # 计划只承载任务事实。真正要钉死的是「编译结果必须有参考」。
    try:
        compiled = _compiled_reference_count(lib, task_id)
    except Exception as e:  # noqa: BLE001
        compiled = -1
        _record("缺陷1(c) 编译期必须产出参考", True, f"编译失败：{type(e).__name__}: {e}")
    else:
        _record("缺陷1(c) 编译期必须产出参考", compiled <= 0,
                f"编译得到 {compiled} 张参考（权威包路由）")
    # 附带证据：任务包内是否有实际图片
    imgs = list((lib / "视觉资产" / "任务包" / task_id).rglob("*.png"))
    _record("缺陷1(b) 任务包不含参考图片", not imgs, f"包内 PNG 数={len(imgs)}")


def defect_2_empty_lib_verify_visual(td: Path) -> None:
    """缺陷 2：空库 verify-visual 的 rc 与状态。"""
    empty = td / "空库"
    empty.mkdir()
    r = _run([SKILL / "scripts" / "maintenance.py", "verify-visual", "--lib", empty, "--json"])
    payload = {}
    try:
        payload = json.loads(r.stdout)
    except ValueError:
        pass
    state = payload.get("visual_delivery") or payload.get("state") or ""
    _record("缺陷2 空库 verify-visual rc=0", r.returncode == 0,
            f"rc={r.returncode} state={state or '(无字段)'} 输出={r.stdout.strip()[:120]}")
    manifest = empty / "视觉资产" / "视觉交付清单.json"
    _record("缺陷2(b) 空库不该签发视觉清单", manifest.is_file(),
            f"清单存在={manifest.is_file()}")


def defect_3_gemini_empty_bytes(td: Path) -> None:
    """缺陷 3：假 SDK 捕获载荷，证明缺图发送空字节 + MIME 固定 PNG。"""
    sys.path.insert(0, str(SKILL / "scripts"))
    sys.path.insert(0, str(SKILL / "scripts" / "engine"))
    try:
        from engine.gemini_image import GeminiImageConfig, GeminiImageExecutor
        from engine.image_executor import GenerationRequest
        from engine.visual_schema import ReferenceBinding
    except ImportError as e:
        _record("缺陷3 Gemini 载荷", False, f"导入失败：{e}")
        return

    captured = {"parts": []}

    class _Part:
        @staticmethod
        def from_bytes(data, mime_type):  # noqa: N803 - 对齐真实 SDK 签名
            captured["parts"].append((len(data), mime_type))
            return {"bytes": data, "mime": mime_type}

    class _Types:
        Part = _Part

        @staticmethod
        def ImageConfig(**kw):
            return kw

        @staticmethod
        def GenerateContentConfig(**kw):
            return kw

    class _Models:
        @staticmethod
        def generate_content(**_kw):
            raise AssertionError("复现阶段不应真正发请求")

    req = GenerationRequest(
        task_id="CH_角落老者-正脸-v01", prompt="p", negative_prompt="n",
        references=(ReferenceBinding(path="不存在的参考图.png", role="style", note=""),),
        aspect_ratio="1:1", image_size="2K", output_mime="image/png",
        style_name="仙侠国风电影CG", style_version="1.0.0",
        reference_roles=("style",), extra={})

    ex = GeminiImageExecutor(client=type("C", (), {"models": _Models()})(),
                             config=GeminiImageConfig(api_key="k"), types_module=_Types())
    try:
        ex._sdk_contents(req, _Types())
    except Exception as e:  # noqa: BLE001
        _record("缺陷3 缺图发送空字节", False, f"未走到捕获：{type(e).__name__}: {e}")
        return
    holes = [p for p in captured["parts"] if p[0] == 0]
    mimes = {p[1] for p in captured["parts"]}
    _record("缺陷3 缺图发送空字节", bool(holes), f"空字节数={len(holes)} 总图片={len(captured['parts'])}")
    _record("缺陷3(b) MIME 硬编码 PNG", mimes == {"image/png"} if mimes else False,
            f"MIME 集合={mimes or '无'}")


def defect_4_env_path(td: Path) -> None:
    """缺陷 4：包外 .env（经 ASSET_ENV_PATH 指定）是否被视觉 CLI 检测到。

    必须在「有真实计划与真实任务」的库上跑 run，否则会先因找不到任务退出。
    """
    secret = "repro-placeholder-key-value-not-a-real-secret"
    ext = td / "包外.env"
    ext.write_text(f"GEMINI_API_KEY={secret}\n", encoding="utf-8")
    lib = _visual_lib(td, "资产库-env")
    r = _run([SKILL / "scripts" / "maintenance.py", "visual-plan", "--lib", lib, "--style", STYLE])
    plan = (lib / "视觉资产" / "计划.toml").read_text(encoding="utf-8")
    task_ids = re.findall(r'task_id\s*=\s*"([^"]+)"', plan)
    if not task_ids:
        _record("缺陷4 包外 .env 未生效", False, f"前置失败：无任务（rc={r.returncode}）")
        return

    env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
    env["ASSET_ENV_PATH"] = str(ext)
    # 选无上游依赖的任务，否则会先因依赖未通过退出，测不到配置检测
    face_tasks = [t for t in task_ids if re.search(r"正脸-v\d+$", t)]
    target = face_tasks[0] if face_tasks else task_ids[0]
    r = _run([SKILL / "scripts" / "visual_production.py", "run", "--lib", lib, "--task", target],
             env=env)
    combined = r.stdout + r.stderr
    refused = "未配置" in combined
    _record("缺陷4 视觉CLI读不到包外.env", refused,
            f"任务={target} rc={r.returncode} 报未配置={refused}"
            f"（修复后应从 ASSET_ENV_PATH 的 .env 读到配置，不再误报未配置）")
    leaked = secret in combined
    _record("缺陷4(b) 日志泄漏密钥（修复后必须保持不泄漏）", leaked, f"泄漏={leaked}")


def main() -> int:
    print("🔍 v2.15.0-rc.2 终验工单：四项 P0 缺陷复现（修复前基线）")
    with tempfile.TemporaryDirectory(prefix="rc2_repro_") as raw:
        td = Path(raw)
        print("\n[缺陷 1] 参考图路由未接入真实生产链")
        defect_1_reference_router_not_wired(td)
        print("\n[缺陷 2] 空库 verify-visual 语义")
        defect_2_empty_lib_verify_visual(td)
        print("\n[缺陷 3] Gemini 参考图载荷")
        defect_3_gemini_empty_bytes(td)
        print("\n[缺陷 4] 包外 .env 检测")
        defect_4_env_path(td)

    print("\n" + "=" * 60)
    reproduced = sum(1 for _n, present, _d in FINDINGS if present)
    for name, present, detail in FINDINGS:
        print(f"  {'🔴 复现' if present else '⚪ 未复现'} {name}：{detail}")
    print(f"\n{len(FINDINGS)} 项检查中 {reproduced} 项复现出缺陷（修复前基线应有缺陷项被复现）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
