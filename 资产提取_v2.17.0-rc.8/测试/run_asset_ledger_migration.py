#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 12 回归：旧库迁移 → 回滚 → 再迁移。

用临时脱敏库跑完整三步，逐项校验计划的两条完成条件：
  1. 迁移、回滚和再次迁移均**幂等**；
  2. `scan` 阶段旧交付文件哈希**完全不变**。
另验证 apply 不覆盖旧 Markdown、rollback 逐文件哈希一致、shadow 不动原库。

退出码：全部通过 0，任一不通过 1。
"""
import json
import sys
import tempfile
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "engine"))

from engine import ledger_migration as mig  # noqa: E402

FAILURES: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}{('：' + detail) if detail and not ok else ''}")
    if not ok:
        FAILURES.append(name)


def build_legacy_library(root: Path) -> Path:
    lib = root / "资产库"
    (lib / ".process").mkdir(parents=True)
    (lib / "提示词" / "基础").mkdir(parents=True)
    (lib / "连续性").mkdir(parents=True)
    (lib / "视觉资产").mkdir(parents=True)
    index = {"schema_version": 1, "source_fingerprint": "sha256:fixture", "assets": [
        {"asset_id": "CH_甲", "stable_key": "CHR:00001:1", "type": "人物", "name": "甲",
         "priority": "P0", "fact_level": "剧本事实", "status": "CONFIRMED", "aliases": [],
         "evidence": [{"line": "00001", "quote": "甲登场"}]},
        {"asset_id": "SC_乙", "stable_key": "SCN:00002:1", "type": "场景", "name": "乙",
         "priority": "P1", "fact_level": "剧本事实", "status": "CONFIRMED", "aliases": [],
         "evidence": [{"line": "00002", "quote": "乙处空无一人"}]},
        {"asset_id": "PR_丙", "stable_key": "PRP:00003:1", "type": "道具", "name": "丙",
         "priority": "P2", "fact_level": "强推断", "status": "CONFIRMED", "aliases": [],
         "evidence": []},
    ]}
    (lib / ".process" / "资产索引.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    (lib / "反推_最终.md").write_text(
        "# 反推\n\n## 人物资产提取表\n| 资产ID | 人物姓名 | 别名/称呼 | 出现位置 | 重要等级 | 备注 |\n"
        "|---|---|---|---|---|---|\n| CH_甲 | 甲 |  | 第1章 | S |  |\n", encoding="utf-8")
    (lib / "提示词" / "基础" / "基础提示词.md").write_text("# 基础\n## 甲\n正文\n", encoding="utf-8")
    (lib / "连续性" / "场景母版.md").write_text(
        "# 场景空间母版\n\n## SC_乙 乙\n| 字段 | 内容 |\n|---|---|\n| 固定锚点 | 人工补的屏风 |\n",
        encoding="utf-8")
    (lib / "视觉资产" / "正式版本.json").write_text(
        json.dumps({"schema_version": 1, "releases": [
            {"task_id": "CH_甲-正脸", "decision": "ACCEPTED", "image_sha256": "deadbeef"}]},
            ensure_ascii=False), encoding="utf-8")
    return lib


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ledger_mig_") as tmp:
        root = Path(tmp)
        lib = build_legacy_library(root)
        before = mig.library_hashes(lib)

        print("🔍 scan（只读）")
        report = mig.scan(lib)
        check("scan 旧交付文件哈希完全不变", mig.library_hashes(lib) == before)
        check("scan 未写任何文件", report["wrote"] == [])
        check("scan 提案计数正确", report["proposal"]["total"] == 3,
              str(report["proposal"]))
        check("无证据资产标 legacy proposal",
              [a["asset_id"] for a in report["ambiguities"]] == ["PR_丙"])
        check("无证据资产未伪造摘句",
              all(not a.get("quote") for a in report["ambiguities"]))

        print("📦 apply（备份 + 写总账，不覆盖旧 Markdown）")
        prompts = lib / "提示词" / "基础" / "基础提示词.md"
        prompts_before = prompts.read_bytes()
        four = lib / "反推_最终.md"
        four_before = four.read_bytes()
        first = mig.apply(lib)
        check("已备份", Path(first["backup"]).is_file())
        check("已写总账", (lib / ".process" / "asset-ledger.json").is_file())
        check("已产对照文件", (lib / mig.COMPARE_NAME).is_file())
        check("旧 Markdown 未被覆盖", four.read_bytes() == four_before)
        check("旧提示词未被覆盖", prompts.read_bytes() == prompts_before)
        ledger = json.loads((lib / ".process" / "asset-ledger.json").read_text(encoding="utf-8"))
        keys = {e["asset_id"]: e["stable_key"] for e in ledger["entries"]}
        check("stable key 保留", keys["CH_甲"] == "CHR:00001:1")
        check("人工场景母版字段保留",
              "人工补的屏风" in (lib / "连续性" / "场景母版.md").read_text(encoding="utf-8"))
        releases = json.loads((lib / "视觉资产" / "正式版本.json").read_text(encoding="utf-8"))
        check("已通过视觉审核保留", releases["releases"][0]["decision"] == "ACCEPTED")
        flagged = next(e for e in ledger["entries"] if e["asset_id"] == "PR_丙")
        check("无证据资产阻断正式签发",
              flagged.get("migration_status") == mig.LEGACY_PROPOSAL and flagged.get("blocking"))

        print("🔁 再次 apply（幂等）")
        second = mig.apply(lib)
        check("第二次 apply 幂等", second["idempotent"] is True)

        print("↩️ rollback")
        (lib / "反推_最终.md").write_text("被改坏了", encoding="utf-8")
        result = mig.rollback(lib)
        check("回滚后逐文件哈希一致", result["mismatch"] == [], str(result["mismatch"]))
        check("回滚回到迁移前状态", mig.library_hashes(lib) == before)

        print("🔁 回滚后再 apply")
        third = mig.apply(lib)
        check("回滚后 apply 不是幂等（总账已不在）", third["idempotent"] is False)
        check("再迁移内容与首次一致",
              (lib / ".process" / "asset-ledger.json").read_bytes()
              == json.dumps(json.loads((lib / ".process" / "asset-ledger.json").read_text(encoding="utf-8")),
                            ensure_ascii=False, indent=2).encode("utf-8"))

        print("🌗 shadow（不改动原库）")
        snapshot = mig.library_hashes(lib)
        shadow = mig.shadow(lib, work_root=root / "影子")
        check("shadow 不改动原库", mig.library_hashes(lib) == snapshot)
        saving = shadow["expected_saving"]
        check("shadow 给出预计调用节省", saving["old_text_calls"] >= saving["new_text_calls"],
              str(saving))

    print("")
    if FAILURES:
        print(f"❌ 迁移回归失败 {len(FAILURES)} 项：{'、'.join(FAILURES)}")
        return 1
    print("✅ 旧库迁移 / 回滚 / 影子运行回归全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
