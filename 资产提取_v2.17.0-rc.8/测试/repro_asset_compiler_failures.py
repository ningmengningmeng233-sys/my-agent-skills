# -*- coding: utf-8 -*-
"""工单 0 复现器：用现有 v2.16 链路跑脱敏夹具，输出漏斗计数并按 v2.17 标准判定覆盖。

漏斗口径：登记数（预聚合候选组）/ 聚合数（正式人物·场景·道具）/ 基础提示词数 / 图片计划数。

v2.17 覆盖标准：每一个正式 人物/场景/道具 都必须有基础提示词。凡因 P3（单次出现等
生产优先级推断）被跳过、且没有"明确非资产/用户取消"理由的正式资产，一律计为静默丢失。

退出码：
  基线（默认）：报告生成成功即视为运行成功；覆盖率不达标 → 退出 1（冻结故障的正证据）。
  --expect-fixed：覆盖率 100%（丢失=0）才退出 0，否则退出 1。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
FIXTURE_DIR = os.path.join(HERE, "fixtures", "asset_compiler_regression")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from mock_api import start_mock_server  # noqa: E402

SOURCE = os.path.join(FIXTURE_DIR, "source.txt")
NOVEL = "回归夹具"
AUTHOR = "脱敏"
GROUP = f"{NOVEL}-{AUTHOR}"


def run_stage(script, args, lib, url):
    env = {
        **os.environ,
        "ASSET_LIB_DIR": lib,
        "OPENAI_API_KEY": "repro-local-key",
        "SUMMARY_API_MODEL": "repro-model",
        "SUMMARY_API_URL": url,
        "ASSET_WORKERS": "8",
    }
    r = subprocess.run([sys.executable, os.path.join("scripts", script)] + args,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=REPO_ROOT, env=env)
    return r.returncode, r.stdout, r.stderr


def parse_formal_assets(final_md):
    """从反推_最终.md 四张表提取正式 人物/场景/道具（列 0=资产ID，列 1=名称）。

    仅在「人物/场景/道具资产提取表」三节内计数；遇到其它 `##` 标题即复位 section，
    避免把「合并资产总表 / 待确认附录」里以别名占第二列、复用同一资产 ID 的行误算成正式资产。
    """
    formal = []
    section = None
    sec_map = {"人物": "人物资产提取表", "场景": "场景资产提取表", "道具": "道具资产提取表"}
    for line in final_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            section = None
            for typ, title in sec_map.items():
                if title in stripped:
                    section = typ
            continue
        if not stripped.startswith("|"):
            continue
        if section is None:
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2 or cells[0] in ("资产ID", "") or set(cells[0]) <= {"-"}:
            continue
        if cells[0].startswith("CH_") or cells[0].startswith("SC_") or cells[0].startswith("PR_"):
            formal.append({"type": section, "asset_id": cells[0], "name": cells[1]})
    return formal


def parse_base_prompted(final_dir_lib):
    """基础提示词分节标题（## NNN. 名称）与跳过清单条目。"""
    prompted = set()
    skipped = {}
    base_dir = os.path.join(final_dir_lib, "提示词", "基础")
    if os.path.isdir(base_dir):
        for fn in os.listdir(base_dir):
            if not fn.endswith(".md"):
                continue
            text = open(os.path.join(base_dir, fn), encoding="utf-8").read()
            if fn == "跳过清单.md":
                for m in re.finditer(r"\|\s*([A-Z]{2}_[^|]+?)\s*\|([^|]*)\|", text):
                    skipped[m.group(1).strip()] = m.group(2).strip()
                for m in re.finditer(r"([A-Z]{2}_[^\s，。|]+)", text):
                    skipped.setdefault(m.group(1).strip(), "跳过")
            else:
                for m in re.finditer(r"^##\s*\d{3}\.\s*(.+)$", text, re.M):
                    prompted.add(m.group(1).strip())
    return prompted, skipped


def count_visual_plan(lib):
    plan = os.path.join(lib, "视觉资产", "计划.toml")
    if not os.path.isfile(plan):
        return 0
    text = open(plan, encoding="utf-8").read()
    return len(re.findall(r"^\[\[task", text, re.M))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="报告 JSON 输出路径")
    ap.add_argument("--expect-fixed", action="store_true")
    args = ap.parse_args()

    root = tempfile.mkdtemp(prefix="asset_repro_")
    lib = os.path.join(root, "资产库")
    os.makedirs(lib, exist_ok=True)
    srv, url = start_mock_server()
    try:
        stages = [
            ("split_segments.py", [SOURCE, "--novel", NOVEL, "--author", AUTHOR,
                                   "--lib", lib, "--overlap", "1"]),
            ("scan_registry.py", [os.path.join(lib, "原文段", GROUP), "--lib", lib]),
            ("extract_segments.py", [os.path.join(lib, "原文段", GROUP), "--lib", lib]),
            ("aggregate.py", [os.path.join(lib, "段产物", GROUP),
                              os.path.join(lib, "原文段", GROUP), "--lib", lib]),
            ("generate_prompts.py", ["--base", os.path.join(lib, "反推_最终.md"),
                                     "--genre", "古风", "--lib", lib,
                                     "--style", "仙侠奇幻风", "--yes", "--view-mode", "full"]),
        ]
        stage_rc = {}
        for script, a in stages:
            rc, out, err = run_stage(script, a, lib, url)
            stage_rc[script] = rc
            if rc != 0:
                print(f"[repro] 阶段失败 {script} rc={rc}\n{out[-400:]}\n{err[-400:]}",
                      file=sys.stderr)

        final_path = os.path.join(lib, "反推_最终.md")
        if not os.path.isfile(final_path):
            report = {"error": "反推_最终.md 未生成", "stage_rc": stage_rc, "lib": lib}
            _write_report(report, args.out)
            print("[repro] 流水线未能产出反推_最终.md", file=sys.stderr)
            return 2
        final_md = open(final_path, encoding="utf-8").read()

        formal = parse_formal_assets(final_md)
        prompted, skipped = parse_base_prompted(lib)
        registry_candidates = _count_candidates(lib)
        visual_plans = count_visual_plan(lib)

        # v2.17 覆盖判定：正式人物/场景/道具若无基础提示词 → 静默丢失
        missing = []
        for a in formal:
            if a["name"] in prompted or a["asset_id"] in prompted:
                continue
            # 跳过清单里"明确非资产/用户取消"可豁免；P3/优先级类理由不豁免
            reason = skipped.get(a["asset_id"], "") or _skip_reason(skipped, a["asset_id"])
            legit = any(k in reason for k in ("非资产", "用户取消", "取消"))
            if not legit:
                missing.append({
                    "type": a["type"], "asset_id": a["asset_id"], "name": a["name"],
                    "skip_reason": reason or "无提示词且不在跳过清单",
                })

        total_formal = len(formal)
        coverage = 1.0 if total_formal == 0 else (total_formal - len(missing)) / total_formal
        report = {
            "schema_version": 1,
            "fixture": "asset_compiler_regression",
            "stage_rc": stage_rc,
            "counts": {
                "registry_candidates": registry_candidates,
                "aggregated_formal": total_formal,
                "base_prompts": len(prompted),
                "image_plan_tasks": visual_plans,
            },
            "coverage": round(coverage, 4),
            "silent_loss": missing,
            "lib": lib,
        }
        _write_report(report, args.out)

        print("=" * 60)
        print("资产编译器漏斗（现有 v2.16 链路）")
        print("=" * 60)
        print(f"  登记候选组 : {registry_candidates}")
        print(f"  聚合正式   : {total_formal}  （人物/场景/道具）")
        print(f"  基础提示词 : {len(prompted)}")
        print(f"  图片计划   : {visual_plans}")
        print(f"  覆盖率     : {coverage:.1%}   静默丢失 {len(missing)} 项")
        for m in missing:
            print(f"    - [{m['type']}] {m['asset_id']}  {m['name']}  理由: {m['skip_reason']}")
        print(f"  报告: {args.out or '(未写文件)'}")

        if args.expect_fixed:
            return 0 if not missing else 1
        # 基线：证明存在静默丢失即退出 1（红证据）
        return 1 if missing else 0
    finally:
        srv.shutdown()
        shutil.rmtree(root, ignore_errors=True)


def _skip_reason(skipped, asset_id):
    for k, v in skipped.items():
        if k == asset_id or asset_id in k or k in asset_id:
            return v
    return ""


def _count_candidates(lib):
    idx = os.path.join(lib, ".process", "资产索引.json")
    if os.path.isfile(idx):
        try:
            data = json.load(open(idx, encoding="utf-8"))
            return len(data.get("assets", data.get("资产", [])))
        except (OSError, ValueError):
            pass
    reg = os.path.join(lib, f"{GROUP}-登记表.md")
    if os.path.isfile(reg):
        return len(re.findall(r"^\|\s*G\d+", open(reg, encoding="utf-8").read(), re.M))
    return 0


def _write_report(report, out_path):
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        os.replace(tmp, out_path)


if __name__ == "__main__":
    sys.exit(main())
