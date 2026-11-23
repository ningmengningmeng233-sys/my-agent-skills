#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三种正式输入格式（TXT / DOCX / EPUB）的 fresh 全流程冒烟（规格 2.2 / 4）。

每个格式各自建一个全新资产库，跑真实命令链：
init → split_segments → scan_registry → extract_segments → aggregate
→ 描述/基础/衍生 → verify 真实签发 → status --json。

统一断言：
  1. 四类顶层资产四表齐全，索引类型不外溢四类（规格 2.1）
  2. 原文定位：DOCX/EPUB 走规范化层，段 frontmatter 带原始来源定位与来源清单，
     且来源清单哈希与真实原文件一致；TXT 保持既有直读路径（不产生中间产物）
  3. 资料签发：交付清单 DELIVERED + 必需检查全过；status --json 双交付状态线成立

只用 mock API，不发起任何真实网络请求；失败即非零退出并打印失败检查名。
"""
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
FIXTURES = SKILL / "测试" / "fixtures" / "sources"
REQUIRED_CHECKS = ("check_schedule", "check_final", "check_asset_index")
FOUR_TABLES = ("人物资产提取表", "场景资产提取表", "道具资产提取表", "服装妆造资产提取表")
FOUR_TYPES = {"人物", "场景", "道具", "服装"}
STYLE = "仙侠奇幻风"
FAILURES: list = []


def h(text: str) -> None:
    print(f"\n=== {text} ===")


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{detail[:200]}]" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    return cond


def run(args: list, lib: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable] + [str(a) for a in args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(SKILL),
        env={**env, "ASSET_LIB_DIR": str(lib)},
    )


def run_step(label: str, args: list, lib: Path, env: dict, stdout_ok=None) -> None:
    r = run(args, lib, env)
    ok = r.returncode == 0 and (stdout_ok(r.stdout) if stdout_ok else True)
    if not check(label, ok, (r.stdout[-300:] + r.stderr[-200:])):
        pass


def make_txt_source(root: Path) -> Path:
    src = root / "冒烟源.txt"
    lines = []
    for i in range(1, 7):
        lines.append(f"第{i}章 章节{i}")
        lines.append("角落老者抚琴，东街茶楼里炉火正旺。")
        lines.append("青瓷茶壶冒着热气，油纸伞靠在门边。")
    src.write_text("\n".join(lines), encoding="utf-8")
    return src


def assert_provenance(label: str, lib: Path, source: Path, normalized: bool) -> None:
    norm_dir = lib / ".process" / "normalized_sources"
    seg_dir = lib / "原文段" / "格式冒烟-测"
    segs = sorted(seg_dir.glob("*.md")) if seg_dir.is_dir() else []
    if not check(f"{label}: 段产物存在", bool(segs)):
        return
    bodies = {p.name: p.read_text(encoding="utf-8") for p in segs}
    if not normalized:
        check(f"{label}: 单 TXT 不产生规范化中间产物", not norm_dir.exists())
        check(f"{label}: 段 frontmatter 保留旧「源文件」字段",
              any(f"源文件: {source.name}" in b for b in bodies.values()))
        check(f"{label}: 旧路径不写原始来源定位",
              all("原始来源定位" not in b for b in bodies.values()))
        return

    md = norm_dir / "规范化原文.md"
    mp = norm_dir / "来源清单.json"
    check(f"{label}: 规范化原文与来源清单已生成", md.is_file() and mp.is_file())
    if not (md.is_file() and mp.is_file()):
        return
    check(f"{label}: 每个段都记录原始来源定位与来源清单",
          all("原始来源定位" in b and "来源清单" in b for b in bodies.values()))
    manifest = json.loads(mp.read_text(encoding="utf-8"))
    check(f"{label}: 来源清单记录了顺序来源", manifest.get("order_source") in ("natural", "explicit"),
          str(manifest.get("order_source")))
    import hashlib
    meta = manifest["sources"][0]
    check(f"{label}: 来源清单哈希与真实原文件一致",
          meta["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest())
    check(f"{label}: 来源清单指向真实原文件",
          Path(meta["source_path"]).resolve() == source.resolve(), meta.get("source_path", ""))
    state = json.loads((lib / ".start-state.json").read_text(encoding="utf-8"))
    check(f"{label}: 启动状态记录来源清单路径与哈希",
          state.get("source_manifest_path") and len(state.get("source_manifest_hash", "")) == 64)


def assert_four_classes(label: str, lib: Path) -> None:
    final = lib / "反推_最终.md"
    text = final.read_text(encoding="utf-8") if final.is_file() else ""
    missing = [t for t in FOUR_TABLES if t not in text]
    check(f"{label}: 四类顶层资产四表齐全", not missing, f"缺 {missing}")
    idx_path = lib / ".process" / "资产索引.json"
    if not check(f"{label}: 资产索引存在", idx_path.is_file()):
        return
    assets = json.loads(idx_path.read_text(encoding="utf-8")).get("assets", [])
    check(f"{label}: 索引非空", bool(assets))
    types = {a.get("type") for a in assets}
    check(f"{label}: 索引类型不外溢四类", types <= FOUR_TYPES, f"实际 {sorted(types)}")


def assert_delivery(label: str, lib: Path, env: dict) -> None:
    mf_path = lib / "交付清单.json"
    if not check(f"{label}: 交付清单已签发", mf_path.is_file()):
        return
    mf = json.loads(mf_path.read_text(encoding="utf-8"))
    names = {c.get("name") for c in mf.get("checks", []) if isinstance(c, dict)}
    check(f"{label}: 交付清单 DELIVERED 且必需检查全过",
          mf.get("status") == "DELIVERED" and set(REQUIRED_CHECKS) <= names
          and all(c.get("rc") == 0 for c in mf.get("checks", [])),
          f"status={mf.get('status')} checks={sorted(n for n in names if n)}")
    r = run([SKILL / "scripts" / "maintenance.py", "status", "--lib", lib, "--json"], lib, env)
    try:
        payload = json.loads(r.stdout)
    except ValueError:
        check(f"{label}: status --json 可解析", False, r.stdout[-200:])
        return
    check(f"{label}: 资料交付线 = DELIVERED",
          payload.get("pipeline_state") == "DELIVERED"
          and payload.get("reason_code") == "DELIVERY_VALID"
          and payload.get("data_delivery", {}).get("state") == "DELIVERED",
          f"{payload.get('pipeline_state')}/{payload.get('reason_code')}")
    check(f"{label}: 视觉交付线独立且为未启用",
          payload.get("visual_delivery", {}).get("state") == "NOT_ENABLED",
          str(payload.get("visual_delivery")))


def run_project(label: str, source: Path, normalized: bool, env: dict, tmp: Path) -> None:
    h(f"{label} fresh 项目（源: {source.name}）")
    lib = tmp / f"资产库-{label}"
    novel, author = "格式冒烟", "测"
    seg_dir = lib / "原文段" / f"{novel}-{author}"
    prod_dir = lib / "段产物" / f"{novel}-{author}"

    run_step("init（记录启动证据）",
             [SKILL / "scripts" / "maintenance.py", "init", "--lib", lib,
              "--input-source", source, "--genre", "古风", "--style", STYLE], lib, env)
    run_step("拆段 split_segments",
             [SKILL / "scripts" / "split_segments.py", source,
              "--novel", novel, "--author", author, "--lib", lib], lib, env)
    run_step("登记表 scan_registry",
             [SKILL / "scripts" / "scan_registry.py", seg_dir, "--lib", lib], lib, env)
    run_step("段级提取 extract_segments",
             [SKILL / "scripts" / "extract_segments.py", seg_dir, "--lib", lib], lib, env)
    run_step("聚合裁决 aggregate",
             [SKILL / "scripts" / "aggregate.py", prod_dir, seg_dir, "--lib", lib], lib, env)
    final = lib / "反推_最终.md"
    run_step("描述补全 --enrich",
             [SKILL / "scripts" / "generate_prompts.py", "--enrich", final,
              "--genre", "古风", "--lib", lib, "--style", STYLE, "--yes"], lib, env,
             stdout_ok=lambda s: "失败 0" in s)
    run_step("基础提示词 --base",
             [SKILL / "scripts" / "generate_prompts.py", "--base", final,
              "--genre", "古风", "--lib", lib, "--style", STYLE, "--yes", "--view-mode", "full"],
             lib, env, stdout_ok=lambda s: "失败 0" in s)
    run_step("调度清单 --init-schedule",
             [SKILL / "scripts" / "generate_prompts.py", "--init-schedule", final,
              "--genre", "古风", "--lib", lib], lib, env)
    run_step("衍生提示词 --deriv",
             [SKILL / "scripts" / "generate_prompts.py", "--deriv",
              "--genre", "古风", "--lib", lib, "--style", STYLE, "--yes", "--view-mode", "full"],
             lib, env, stdout_ok=lambda s: "失败 0" in s)
    run_step("verify 真实签发",
             [SKILL / "scripts" / "maintenance.py", "verify", "--lib", lib, "--style", STYLE],
             lib, env)

    assert_four_classes(label, lib)
    assert_provenance(label, lib, source, normalized)
    assert_delivery(label, lib, env)
    if label == "DOCX":
        segs = sorted(seg_dir.glob("*.md"))
        check("DOCX: 按场次切分（剧本场次标记生效）",
              bool(segs) and any("第1场" in p.name for p in segs),
              "、".join(p.name for p in segs))


def main() -> int:
    srv, url = start_mock_server()
    env = {**os.environ, "ASSET_WORKERS": "8", "SUMMARY_API_URL": url,
           "OPENAI_API_KEY": "smoke-test-key", "SUMMARY_API_MODEL": "smoke-test-model",
           # 与 run_visual_offline 同一类缺陷：这里曾把编码变量 pop 掉，子链在 GBK 控制台
           # 打印 ✅/ℹ 时抛 UnicodeEncodeError，被读成「资料链各步失败」。密钥仍照旧剥。
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    print("正式输入格式 fresh 冒烟（mock API，无真实网络请求）")
    print(f"mock API: {url}")
    tmp = Path(tempfile.mkdtemp(prefix="asset_formats_"))
    try:
        txt_src = make_txt_source(tmp)
        run_project("TXT", txt_src, False, env, tmp)
        run_project("DOCX", FIXTURES / "最小剧本.docx", True, env, tmp)
        run_project("EPUB", FIXTURES / "最小小说.epub", True, env, tmp)
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 50)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for name in FAILURES:
            print(f"  ❌ {name}")
        return 1
    print("TXT / DOCX / EPUB 三种正式输入格式 fresh 全流程冒烟全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
