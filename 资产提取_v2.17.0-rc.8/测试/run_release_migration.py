#!/usr/bin/env python3
"""v2.14.0 发布级迁移/追更回归（F4）：备份→迁移→恢复、追更→重签发、五类负例。

只操作临时库；夹具 测试/fixtures/v212_minimal 只读且入库（不依赖被 gitignore 的测试/e2e）。
覆盖：
- F4.1 复制夹具→migrate：先出 zip 备份；旧四表/旧提示词/段原文哈希不变；
  索引与母版新增；手工母版字段保留；待确认关系落 unresolved 不自动确认；
  缺证据旧字段标 制作提案（不假扮剧本事实）。
- F4.1 restore 到另一个空目录：旧文件哈希逐一致。
- F4.2 追更：真实 e2e 链路签发后追加一章→清单失效→按文档重跑受影响步骤→
  旧 stable_key 不漂移、新资产获得新 key→verify 重签发→status DELIVERED。
- F4.2 五类负例（离线、固定 mock 数据）：别名歧义/悬空关系/假摘句/单向 CONNECTS_TO/锚点反转，
  各自独立报错，不污染正例。
失败即非零退出并打印失败项。
"""
import hashlib
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
FIXTURE = SKILL / "测试" / "fixtures" / "v212_minimal" / "资产库"
# F1.1 同约定：不注入编码保护伞
for _enc_var in ("PYTHONUTF8", "PYTHONIOENCODING"):
    os.environ.pop(_enc_var, None)
ENV = {**os.environ}

# 追更步骤与 e2e_flow 同源 mock（单一来源：测试/mock_api.py）
sys.path.insert(0, str(SKILL / "测试"))
from mock_api import start_mock_server  # noqa: E402

_MOCK_SRV, _MOCK_URL = start_mock_server()
ENV["OPENAI_API_KEY"] = "flow-test-key"
ENV["SUMMARY_API_MODEL"] = "flow-test-model"
ENV["SUMMARY_API_URL"] = _MOCK_URL
ENV["ASSET_WORKERS"] = "8"


def _run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable] + [str(c) for c in cmd],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=dict(ENV), cwd=str(SKILL), **kw)


def _assert(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {name}" + (f"（{detail}）" if detail and not cond else ""))
    if not cond:
        if detail:
            print(f"     {str(detail)[:400]}")
        sys.exit(1)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _tree_hashes(root: Path, rels: list[str]) -> dict[str, str]:
    return {rel: _sha(root / rel) for rel in rels}


OLD_FILES = [
    "反推_最终.md",
    "原文段/旧剧-旧作者/段01.md",
    "描述/人物描述.md",
    "提示词/基础/人物提示词.md",
    "提示词/衍生/人物提示词.md",
    "提示词/调度清单.md",
]
MANUAL_ANCHOR = "手工确认：柜台在大堂东侧（人工）"


def part_migrate_and_restore() -> None:
    print("🔍 F4.1 迁移：备份→索引/母版→旧文件哈希不变→手工字段保留→待确认不自动确认")
    with tempfile.TemporaryDirectory(prefix="v214_migrate_") as td:
        lib = Path(td) / "资产库"
        shutil.copytree(FIXTURE, lib)
        before = _tree_hashes(lib, OLD_FILES)

        r = _run(["scripts/maintenance.py", "migrate", "--lib", str(lib)])
        _assert("migrate 退出 0", r.returncode == 0, r.stdout[-300:] + r.stderr[-200:])

        backups = list((lib.parent / "资产库备份").glob("资产库备份-*.zip"))
        _assert("先生成 zip 备份", len(backups) == 1 and backups[0].stat().st_size > 0)

        after = _tree_hashes(lib, OLD_FILES)
        _assert("旧四表/旧提示词/段原文哈希不变", before == after,
                str({k: (before[k][:8], after[k][:8]) for k in before if before[k] != after[k]}))

        idx_path = lib / ".process" / "资产索引.json"
        master_path = lib / "连续性" / "场景母版.md"
        _assert("索引与母版已生成", idx_path.is_file() and master_path.is_file())
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        by_id = {a["asset_id"]: a for a in idx["assets"]}
        _assert("四类资产各入索引",
                {"CH_沈掌柜", "SC_同福客栈", "PR_黄铜算盘", "CO_沈掌柜常服"} <= set(by_id))
        _assert("缺证据旧字段标制作提案且不假扮剧本事实",
                all(by_id[a]["fact_level"] == "制作提案" and by_id[a]["status"] == "NEEDS_REVIEW"
                    for a in ("CH_沈掌柜", "SC_同福客栈", "PR_黄铜算盘") if a in by_id),
                json.dumps({a: by_id[a]["fact_level"] for a in by_id}, ensure_ascii=False))
        _assert("有效绑定仍生成 WEARS（服装→人物）",
                any(r["kind"] == "WEARS" and r["target"] == "CH_沈掌柜"
                    for r in by_id.get("CO_沈掌柜常服", {}).get("relations", [])))
        _assert("待确认关系落 unresolved 不自动确认",
                any(u["asset_id"] == "CO_神秘客披风" and u["kind"] == "WEARS"
                    for u in idx.get("unresolved_relations", [])),
                str(idx.get("unresolved_relations")))
        _assert("无 STATE_OF 自环（R4b）",
                not [r for a in idx["assets"] for r in a["relations"]
                     if r["kind"] == "STATE_OF" and r["target"] == a["asset_id"]])
        _assert("手工母版字段保留", MANUAL_ANCHOR in master_path.read_text(encoding="utf-8"))

        # 恢复到另一个空目录，旧文件哈希逐一致
        rest_dir = Path(td) / "restored" / "资产库"
        rest_dir.parent.mkdir(parents=True)
        r = _run(["scripts/maintenance.py", "restore", "--zip", str(backups[0]), "--lib", str(rest_dir)])
        _assert("restore 退出 0", r.returncode == 0, r.stdout[-200:])
        restored = _tree_hashes(rest_dir, OLD_FILES)
        _assert("恢复后旧文件哈希逐一致", restored == before,
                str({k for k in before if before[k] != restored.get(k)}))


def part_continuation() -> None:
    print("🔍 F4.2 追更：真实签发→追加一章→清单失效→重跑受影响步骤→旧键不漂移→重签发")
    flow = _run(["测试/e2e_flow.py"])
    _assert("e2e 真实链路全绿", flow.returncode == 0, flow.stdout[-300:])
    m = re.search(r"产物目录: (.+)", flow.stdout)
    lib = Path(m.group(1).strip())

    idx_path = lib / ".process" / "资产索引.json"
    old_keys = {a["asset_id"]: a["stable_key"] for a in json.loads(idx_path.read_text(encoding="utf-8"))["assets"]}

    st = json.loads((lib / ".start-state.json").read_text(encoding="utf-8"))
    src = Path(st["input_source"])
    src.write_text(src.read_text(encoding="utf-8") + "\n第13章 章节13\n林姑娘走进客栈，掌柜在柜台后拨算盘。\n灯笼挂在门边，角落老者抚琴。\n", encoding="utf-8")

    def _state_not_delivered():
        r = _run(["scripts/maintenance.py", "status", "--lib", str(lib), "--json"])
        return json.loads(r.stdout).get("pipeline_state")

    _assert("追加一章后清单立即失效", _state_not_delivered() != "DELIVERED")

    seg_dir = lib / "原文段" / "全流程测试-流测"
    prod_dir = lib / "段产物" / "全流程测试-流测"
    steps = [
        # ADR-0082：段边界变化会被过期段拦截；文档化追更路径为 --append 增量拆段
        ["scripts/split_segments.py", str(src), "--novel", "全流程测试", "--author", "流测", "--lib", str(lib), "--append"],
        ["scripts/scan_registry.py", str(seg_dir), "--lib", str(lib)],
        ["scripts/extract_segments.py", str(seg_dir), "--lib", str(lib)],
        ["scripts/aggregate.py", str(prod_dir), str(seg_dir), "--lib", str(lib)],
        ["scripts/scan_registry.py", "--include-claims", str(lib / ".process" / "登记表对账报告.md"), "--lib", str(lib)],
        ["scripts/generate_prompts.py", "--enrich", str(lib / "反推_最终.md"), "--genre", "古风", "--lib", str(lib), "--style", "仙侠奇幻风", "--yes"],
        ["scripts/generate_prompts.py", "--base", str(lib / "反推_最终.md"), "--genre", "古风", "--lib", str(lib), "--style", "仙侠奇幻风", "--yes", "--view-mode", "full"],
        ["scripts/generate_prompts.py", "--init-schedule", str(lib / "反推_最终.md"), "--genre", "古风", "--lib", str(lib)],
        ["scripts/generate_prompts.py", "--deriv", "--genre", "古风", "--lib", str(lib), "--style", "仙侠奇幻风", "--yes", "--view-mode", "full"],
    ]
    for i, cmd in enumerate(steps):
        r = _run(cmd)
        _assert(f"追更步骤 {i + 1}/{len(steps)} 退出 0", r.returncode == 0,
                (r.stdout[-200:] + r.stderr[-120:]))

    new_idx = {a["asset_id"]: a["stable_key"] for a in json.loads(idx_path.read_text(encoding="utf-8"))["assets"]}
    drifted = {k: (v, new_idx.get(k)) for k, v in old_keys.items() if new_idx.get(k) != v}
    _assert("旧资产 stable_key 不漂移", not drifted, str(drifted))
    fresh_assets = set(new_idx) - set(old_keys)
    _assert("新资产获得新键（客栈/灯）", {"SC_林姑娘走进客栈", "PR_灯"} <= fresh_assets,
            f"新增: {sorted(fresh_assets)}")

    r = _run(["scripts/maintenance.py", "verify", "--lib", str(lib), "--style", "仙侠奇幻风"])
    _assert("追更后重验退出 0", r.returncode == 0, r.stdout[-300:])
    r = _run(["scripts/maintenance.py", "status", "--lib", str(lib), "--json"])
    payload = json.loads(r.stdout)
    _assert("追更后 status 回到 DELIVERED", payload.get("pipeline_state") == "DELIVERED",
            f"{payload.get('pipeline_state')}/{payload.get('reason_code')}")


def part_negatives() -> None:
    print("🔍 F4.2 五类负例（离线固定数据，各自独立报错）")
    sys.path.insert(0, str(SKILL / "scripts"))
    sys.path.insert(0, str(SKILL / "scripts" / "engine"))
    from aggregate import build_asset_index, write_asset_index
    from engine.continuity import check_connects_consistency, check_master_drift

    def mk(name, q, ln):
        return [{"组ID": "G01", "类型": "人物", "主名": name, "等级": "B", "合并": "",
                 "别名": "", "绑定": "", "处理": "保留", "备注": ""}], \
               {"G01": {"组ID": "G01", "成员": [
                   {"段文件": "段01.md", "出现章节": "第1章", "摘句": q, "行号": ln, "别名": ""}]}}

    # 1. 别名歧义
    verdicts = [
        {"组ID": "G01", "类型": "人物", "主名": "林姑娘", "等级": "B", "合并": "", "别名": "", "绑定": "", "处理": "保留", "备注": ""},
        {"组ID": "G02", "类型": "场景", "主名": "东街茶楼", "等级": "A", "合并": "", "别名": "茶楼", "绑定": "", "处理": "保留", "备注": ""},
        {"组ID": "G03", "类型": "场景", "主名": "西街茶楼", "等级": "B", "合并": "", "别名": "茶楼", "绑定": "", "处理": "保留", "备注": ""},
    ]
    groups = {"G01": {"组ID": "G01", "成员": [{"段文件": "段01.md", "出现章节": "第1章", "摘句": "林姑娘进城", "行号": "00002", "别名": ""}]},
              "G02": {"组ID": "G02", "成员": [{"段文件": "段01.md", "出现章节": "第1章", "摘句": "东街茶楼临河", "行号": "00003", "别名": ""}]},
              "G03": {"组ID": "G03", "成员": [{"段文件": "段02.md", "出现章节": "第2章", "摘句": "西街茶楼靠市", "行号": "00006", "别名": ""}]}}
    summaries = [{"章节": "第1章", "概要": "进城", "人物": "林姑娘", "场景": "茶楼", "道具": "", "服装": ""}]
    idx = build_asset_index(verdicts, groups, summaries, 2, source_fingerprint="x")
    _assert("负例1 别名歧义→待审", any("歧义" in u["reason"] for u in idx["unresolved_relations"]))

    # 2/3. 悬空关系与假摘句：写盘后 check_asset_index 必须非零
    with tempfile.TemporaryDirectory(prefix="v214_neg_") as td:
        lib = Path(td) / "资产库"
        seg_text = "张三走进茶楼，桌上有一把油纸伞。"
        (lib / "原文段" / "样例-作者").mkdir(parents=True)
        (lib / "原文段" / "样例-作者" / "段01.md").write_text(seg_text, encoding="utf-8")
        verdicts = [
            {"组ID": "G01", "类型": "服装", "主名": "黑斗篷", "等级": "B", "合并": "", "别名": "", "绑定": "不存在的人", "处理": "保留", "备注": ""},
            {"组ID": "G02", "类型": "人物", "主名": "张三", "等级": "B", "合并": "", "别名": "", "绑定": "", "处理": "保留", "备注": ""},
        ]
        groups = {
            "G01": {"组ID": "G01", "成员": [{"段文件": "段01.md", "出现章节": "第1章", "摘句": "一袭黑斗篷", "行号": "00002", "别名": ""}]},
            "G02": {"组ID": "G02", "成员": [{"段文件": "段01.md", "出现章节": "第1章", "摘句": "这句话并不在段原文里", "行号": "00003", "别名": ""}]},
        }
        write_asset_index(lib, verdicts, groups, [], 1, source_fingerprint="x")
        r = _run(["scripts/check_asset_index.py", "--lib", str(lib)])
        out = r.stdout
        _assert("负例2 悬空关系→索引门非零", r.returncode != 0 and ("未解析关系" in out or "不存在" in out), out[-200:])
        _assert("负例3 假摘句→索引门非零", r.returncode != 0 and "假摘句" in out, out[-200:])

    # 4. 单向 CONNECTS_TO
    one_way = {"schema_version": 1, "source_fingerprint": "x", "assets": [
        {"asset_id": "SC_走廊", "stable_key": "SCN:00001:1", "type": "场景", "name": "走廊",
         "fact_level": "强推断", "priority": "P1", "evidence": [], "dependencies": [],
         "relations": [{"kind": "CONNECTS_TO", "target": "SC_302房间"}], "status": "CONFIRMED"},
        {"asset_id": "SC_302房间", "stable_key": "SCN:00002:1", "type": "场景", "name": "302房间",
         "fact_level": "强推断", "priority": "P1", "evidence": [], "dependencies": [],
         "relations": [], "status": "CONFIRMED"},
    ]}
    _assert("负例4 单向 CONNECTS_TO→报错", check_connects_consistency(one_way))

    # 5. 场景锚点反转（母版与索引空间事实冲突）
    anchored = {"schema_version": 1, "source_fingerprint": "x", "assets": [
        {"asset_id": "SC_走廊", "stable_key": "SCN:00001:1", "type": "场景", "name": "走廊",
         "fact_level": "剧本事实", "priority": "P1",
         "evidence": [{"segment": "段01.md", "chapter": "第1章", "line": "00001", "quote": "门内有台灯"}],
         "dependencies": [], "relations": [], "status": "CONFIRMED",
         "space_facts": {"anchor": [{"value": "门内有台灯", "segment": "段01.md",
                                     "chapter": "第1章", "line": "00001", "quote": "门内有台灯"}]}},
    ]}
    master_text = "# 场景母版\n\n## SC_走廊\n| 字段 | 内容 |\n|---|---|\n| 固定锚点 | 窗在南侧、无台灯 |\n"
    _assert("负例5 锚点反转→报错", check_master_drift(anchored, master_text))


def main() -> int:
    print("🔍 v2.14.0 发布级迁移/追更回归")
    part_migrate_and_restore()
    part_continuation()
    part_negatives()
    print("✅ 迁移/追更/负例回归全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
