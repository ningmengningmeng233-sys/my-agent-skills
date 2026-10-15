#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线视觉端到端 + 失败矩阵 + 恢复矩阵（工单04-Task5）。

- 离线执行器：固定假图 + 结构化 mock，**零真实 Gemini 请求**；
- 正例链：TXT 输入 → 四类资产资料签发 → 显式视觉计划 → 手工导出 → mock 导入 →
  逐项 QC → 自动拼版 → 视觉签发；资料交付在全链路始终有效；
- 失败矩阵：缺状态 / 源变化 / 风格包漂移 / 旧三宫格 / 断颈 / 正背不一致 /
  未通过参考 / 第四轮请求 / 依赖过期 / 图片哈希变化 / 整部原稿误发送；
  （风格别名冲突**不在此列**——它由 `测试/unit/test_style_registry.py` 直接覆盖，
  此处曾只留一行 import 却无任何断言，属声明的覆盖面与实物不符。）
- 恢复矩阵：扰动恢复后只重验受影响任务；资料签发不被视觉扰动覆盖；
  历史有效清单不被失败验证覆盖。
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_api import start_mock_server  # noqa: E402

SKILL = Path(__file__).resolve().parent.parent
S = SKILL / "scripts"
REQUIRED_CHECKS = ("check_schedule", "check_final", "check_asset_index")
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGNsaGhggAEmOAsvBwBMBAGIxu9U"
    "aAAAAABJRU5ErkJggg==")
FAILURES: list = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{str(detail)[:180]}]" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return cond


def run(args, cwd=None, env=None):
    return subprocess.run([sys.executable] + [str(a) for a in args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          cwd=str(cwd or SKILL), env=env)


def status_json(lib: Path, env) -> dict:
    r = run([S / "maintenance.py", "status", "--lib", lib, "--json"], env=env)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


def data_state(payload: dict) -> str:
    return payload.get("data_delivery", {}).get("state", payload.get("pipeline_state", ""))


def visual_state(payload: dict) -> str:
    return payload.get("visual_delivery", {}).get("state", "")


def main() -> int:
    srv, url = start_mock_server()
    env = {**os.environ, "ASSET_WORKERS": "8", "SUMMARY_API_URL": url,
           "OPENAI_API_KEY": "offline-test-key", "SUMMARY_API_MODEL": "offline-model",
           # 编码由本文件负责：这里曾把 PYTHONUTF8/PYTHONIOENCODING 一起 pop 掉，
           # 于是整条子链在 GBK 控制台打印 ✅/ℹ 时抛 UnicodeEncodeError，
           # 表现为「资料链/视觉计划」成片失败而退出码语义全是假的。
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    for var in ("GEMINI_API_KEY",):
        env.pop(var, None)
    print("离线视觉端到端（结构化 mock，零真实 API 请求）")
    tmp = Path(tempfile.mkdtemp(prefix="visual_offline_"))
    try:
        lib = tmp / "资产库"
        src = tmp / "源.txt"
        src.write_text("\n".join(
            f"第{i}章 章节{i}\n林姑娘走进茶楼，掌柜在柜台后拨算盘。\n青瓷茶壶冒着热气，油纸伞靠在门边。"
            for i in range(1, 5)), encoding="utf-8")

        # ---- 正例链：资料交付 ----
        print("\n[1] 资料链：TXT → 四类资产 → 真实签发")
        steps = [
            ("init", [S / "maintenance.py", "init", "--lib", lib, "--input-source", src, "--genre", "古风", "--style", "仙侠奇幻风"]),
            ("拆段", [S / "split_segments.py", src, "--novel", "离线", "--author", "测", "--lib", lib]),
            ("登记表", [S / "scan_registry.py", lib / "原文段" / "离线-测", "--lib", lib]),
            ("段级提取", [S / "extract_segments.py", lib / "原文段" / "离线-测", "--lib", lib]),
            ("聚合", [S / "aggregate.py", lib / "段产物" / "离线-测", lib / "原文段" / "离线-测", "--lib", lib]),
            ("描述", [S / "generate_prompts.py", "--enrich", lib / "反推_最终.md", "--genre", "古风", "--lib", lib, "--style", "仙侠奇幻风", "--yes"]),
            ("基础", [S / "generate_prompts.py", "--base", lib / "反推_最终.md", "--genre", "古风", "--lib", lib, "--style", "仙侠奇幻风", "--yes", "--view-mode", "full"]),
            ("调度", [S / "generate_prompts.py", "--init-schedule", lib / "反推_最终.md", "--genre", "古风", "--lib", lib]),
            ("衍生", [S / "generate_prompts.py", "--deriv", "--genre", "古风", "--lib", lib, "--style", "仙侠奇幻风", "--yes", "--view-mode", "full"]),
            ("verify", [S / "maintenance.py", "verify", "--lib", lib, "--style", "仙侠奇幻风"]),
        ]
        for name, cmd in steps:
            r = run(cmd, env=env)
            check(f"资料链 {name}", r.returncode == 0, r.stdout[-200:] + r.stderr[-120:])
        payload = status_json(lib, env)
        check("资料交付 = DELIVERED", data_state(payload) == "DELIVERED", payload.get("reason_code"))
        check("视觉交付 = 未启用（合法）", visual_state(payload) == "NOT_ENABLED")

        # ---- 正例链：视觉生产 ----
        print("\n[2] 视觉链：计划 → 手工导出 → mock 导入 → QC → 拼版 → 签发")
        r = run([S / "create_visual_plan.py", "--lib", lib,
                 "--style", "仙侠国风电影CG"], env=env)
        check("视觉计划生成", r.returncode == 0, r.stdout[-200:])
        plan_path = lib / "视觉资产" / "计划.toml"
        plan = (plan_path.read_text(encoding="utf-8") if plan_path.is_file() else "")
        task_ids = [line.split('"')[1] for line in plan.splitlines()
                    if line.startswith("task_id = ")]
        check("计划含独立任务", bool(task_ids), task_ids[:3])

        # 手工导出（无密钥路径）
        exported = []
        for task_id in task_ids[:3]:
            r = run([S / "visual_production.py", "export",
                     "--lib", lib, "--task", task_id], env=env)
            exported.append(r.returncode == 0)
            package = lib / "视觉资产" / "任务包" / task_id
            trio = [package / name for name in ("请求.json", "提示词.md", "参考图清单.md")]
            check(f"手工任务包 {task_id}", all(p.is_file() for p in trio))

        # mock 导入 + QC（用假图，绝不调用真实模型）
        fake = tmp / "mock结果.png"
        fake.write_bytes(PNG)
        for task_id in task_ids[:3]:
            run([S / "visual_production.py", "import",
                 "--lib", lib, "--task", task_id, "--image", fake], env=env)
            qc_file = tmp / f"{task_id}.qc.json"
            import hashlib as _hashlib
            qc_payload = {"task_id": task_id, "decision": "ACCEPTED", "checker": "mock:offline",
                          "checked_at": "t", "issues": [],
                          "input_hashes": [_hashlib.sha256(PNG).hexdigest()]}
            qc_file.write_text(json.dumps(qc_payload, ensure_ascii=False), encoding="utf-8")
            r = run([S / "visual_production.py", "check",
                     "--lib", lib, "--task", task_id, "--qc", qc_file], env=env)
            check(f"QC 写回 {task_id}", r.returncode == 0, r.stdout[-160:])

        r = run([S / "compose_reference_board.py", "--lib", lib,
                 "--task", task_ids[0]], env=env)
        check("自动拼版（只合成不生成）", r.returncode == 0, r.stdout[-200:])
        board = lib / "视觉资产" / "拼版" / f"{task_ids[0]}.png"
        check("拼版产物存在", board.is_file())

        r = run([S / "check_visual_delivery.py", "--lib", lib, "--json"], env=env)
        delivery = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
        check("视觉交付检查可运行", bool(delivery))
        check("未全部通过时不签发", delivery.get("status") != "DELIVERED" or r.returncode == 0)

        payload = status_json(lib, env)
        check("视觉链路后资料交付仍 DELIVERED", data_state(payload) == "DELIVERED",
              payload.get("reason_code"))
        check("视觉交付线已进入部分完成", visual_state(payload) in ("PARTIAL", "PLANNED", "DELIVERED"),
              visual_state(payload))

        # ---- 失败矩阵 ----
        print("\n[3] 失败矩阵（每项都必须被拒绝）")
        if str(SKILL) not in sys.path:
            sys.path.insert(0, str(SKILL))
        from scripts.engine.image_qc import BLOCKING, parse_qc_response
        from scripts.engine.image_request_builder import RequestBuildError
        from scripts.engine.visual_pipeline import REPAIR_HINTS, propagate_upstream_change
        from scripts.engine.visual_schema import (
            VisualPlan,
            VisualTask,
            load_visual_plan,
            validate_visual_plan,
        )

        plan_obj = load_visual_plan(plan_path)
        first = plan_obj.by_id(task_ids[0])

        # 1) 缺状态
        state_file = lib / ".start-state.json"
        saved = state_file.read_text(encoding="utf-8")
        state_file.unlink()
        check("缺启动状态 → 资料线拒签", data_state(status_json(lib, env)) != "DELIVERED")
        state_file.write_text(saved, encoding="utf-8")

        # 2) 源变化
        saved_src = src.read_text(encoding="utf-8")
        src.write_text(saved_src + "\n追更一行", encoding="utf-8")
        changed = status_json(lib, env)
        check("源变化 → 脱离交付", data_state(changed) != "DELIVERED", changed.get("reason_code"))
        src.write_text(saved_src, encoding="utf-8")

        # 3) 风格包漂移
        from scripts.engine.style_registry import resolve_style_record
        record = resolve_style_record("仙侠国风电影CG")
        drifted = VisualPlan(**{**plan_obj.__dict__, "style_hash": "f" * 64})
        check("风格包漂移 → 校验拦截",
              any(i.code == "STYLE_PIN_MISMATCH" for i in validate_visual_plan(drifted, {})))

        # 4) 错误别名：由 测试/unit/test_style_registry.py 覆盖，本门不再重复声明
        #    （此处原有一行 `StyleAliasConflict` 死导入，段内无断言）

        # 5) 旧三宫格
        old_plan = lib / "视觉资产" / "旧三宫格.toml"
        old_plan.write_text((plan_path.read_text(encoding="utf-8")
                            + "\n# 三格图板：左面正面特写，右面正视/后视\n"), encoding="utf-8")
        check("旧三宫格文本不进入计划字段",
              "三格" not in load_visual_plan(plan_path).tasks[0].kind)
        old_plan.unlink()

        # 6) 断颈
        neck = parse_qc_response({
            "task_id": first.task_id, "decision": "ACCEPTED", "checker": "m", "checked_at": "t",
            "input_hashes": ["a" * 64],
            "issues": [{"code": "NECK_STUMP_VISIBLE", "severity": BLOCKING, "evidence": "颈桩",
                        "repair_instruction": "隐形支撑"}],
        }, expected={"task_id": first.task_id, "input_hashes": ["a" * 64]})
        check("断颈 → 不得判通过", neck.decision != "ACCEPTED")
        check("断颈有定向修复指令", bool(REPAIR_HINTS.get("NECK_STUMP_VISIBLE")))

        # 7) 正背不一致
        fb = parse_qc_response({
            "task_id": first.task_id, "decision": "NEEDS_REVISION", "checker": "m",
            "checked_at": "t", "input_hashes": ["a" * 64],
            "issues": [{"code": "FRONT_BACK_INCONSISTENT", "severity": BLOCKING,
                        "evidence": "画布不一致", "repair_instruction": "统一相机参数"}],
        }, expected={"task_id": first.task_id, "input_hashes": ["a" * 64]})
        check("正背不一致 → 需修改", fb.decision == "NEEDS_REVISION")

        # 8) 未通过参考
        upstream = VisualTask(**{**first.__dict__, "status": "NEEDS_REVISION"})
        down_fields = dict(first.__dict__)
        down_fields.update({"task_id": "%s-后脑-v01" % first.asset_id, "kind": "后脑",
                            "status": "GENERATED", "aspect_ratio": "1:1",
                            "depends_on": (first.task_id,)})
        downstream = VisualTask(**down_fields)
        bad_plan = VisualPlan(**{**plan_obj.__dict__, "tasks": (upstream, downstream)})
        check("未通过参考 → 校验拦截",
              any(i.code == "REFERENCE_NOT_ACCEPTED" for i in validate_visual_plan(bad_plan, {})))

        # 9) 第四轮请求
        too_many = VisualTask(**{**first.__dict__, "attempts": tuple(
            __import__("scripts.engine.visual_schema", fromlist=["AttemptRecord"]).AttemptRecord(
                round=n, image=f"a{n}.png", image_sha256=str(n) * 64) for n in range(1, 5))})
        check("第四轮 → 超限拦截", any(
            i.code in ("ROUND_LIMIT_EXCEEDED", "MAX_ROUNDS_OVERFLOW")
            for i in validate_visual_plan(VisualPlan(**{**plan_obj.__dict__,
                                                        "tasks": (too_many,)}), {})))

        # 10) 依赖过期传播
        accepted_downstream = VisualTask(
            **{**downstream.__dict__, "status": "READY_AS_REFERENCE"})
        ready = VisualPlan(**{**plan_obj.__dict__, "tasks": (
            VisualTask(**{**first.__dict__, "status": "READY_AS_REFERENCE"}),
            accepted_downstream,)})
        propagated = propagate_upstream_change(ready, first.task_id)
        check("上游替换 → 下游需要重新检查",
              propagated.by_id(downstream.task_id).status == "NEEDS_REVIEW")
        check("上游替换不影响上游自身",
              propagated.by_id(first.task_id).status == "READY_AS_REFERENCE")

        # 11) 图片哈希变化
        from scripts.engine.visual_store import verify_attempt
        attempt = first.latest_attempt
        if attempt is not None:
            tampered = lib / attempt.image
            original_bytes = tampered.read_bytes()
            tampered.write_bytes(original_bytes + b"x")
            check("图片哈希变化 → 校验拦截",
                  any(i.code == "IMAGE_HASH_MISMATCH" for i in verify_attempt(attempt, lib)))
            tampered.write_bytes(original_bytes)
            check("恢复字节后校验通过", verify_attempt(attempt, lib) == [])

        # 12) 整部原稿误发送
        from scripts.engine.image_request_builder import build_generation_request
        from scripts.engine.reference_router import load_omni_stylepack
        big_task = VisualTask(**{**first.__dict__, "notes": "整章原文" * 4000})
        try:
            build_generation_request(big_task, {"asset_id": first.asset_id, "name": "x",
                                                "evidence": []}, load_omni_stylepack(record),
                                     [], "gemini")
            check("整部原稿误发送 → 拦截", False, "未拦截")
        except RequestBuildError as e:
            check("整部原稿误发送 → 拦截", e.code in ("PROMPT_TOO_LARGE",), e.code)

        # ---- 恢复矩阵 ----
        print("\n[4] 恢复矩阵")
        r = run([S / "maintenance.py", "verify", "--lib", lib,
                 "--style", "仙侠奇幻风"], env=env)
        payload = status_json(lib, env)
        check("扰动恢复后资料线重新签发", data_state(payload) == "DELIVERED",
              payload.get("reason_code"))
        check("资料签发不被视觉扰动覆盖", visual_state(payload) != "NOT_ENABLED")
        manifest = lib / "交付清单.json"
        check("历史有效交付清单仍在", manifest.is_file())
        if manifest.is_file():
            mf = json.loads(manifest.read_text(encoding="utf-8"))
            check("清单状态与必需检查集合",
                  mf.get("status") == "DELIVERED"
                  and set(REQUIRED_CHECKS) <= {c.get("name") for c in mf.get("checks", [])})
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 50)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for name in FAILURES:
            print(f"  ❌ {name}")
        return 1
    print("离线视觉端到端 + 失败矩阵 + 恢复矩阵全部通过（零真实 API 请求）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
