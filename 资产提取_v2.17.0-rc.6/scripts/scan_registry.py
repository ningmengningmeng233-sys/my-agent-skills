#!/usr/bin/env python3
"""Global asset registry generator: parallel candidates + hierarchical merging."""
import argparse
import hashlib
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
sys.path.insert(0, str(SKILL_DIR))

from engine.common import read_text, parse_frontmatter, number_lines, split_template, range_label
from engine.prompt_rules import PLACEHOLDER_NAMES as PLACEHOLDER_ALIAS
from engine.llm import call_llm, log, WORKERS
from engine.index_db import IndexDB, RULESET_VERSION, file_hash, task_node_fingerprint
from engine import source_facts as _source_facts
if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ASSET_LIB_DIR = Path(os.environ.get("ASSET_LIB_DIR", "").strip() or SKILL_DIR / "资产库")
TMPL_CANDIDATE = SKILL_DIR / "templates" / "候选主名表模板.md"
TMPL_MERGE = SKILL_DIR / "templates" / "登记表聚合模板.md"

# 别名列的空值写法与「表内空值」唯一集合同源（engine.prompt_rules.PLACEHOLDER_NAMES）


def segment_task_fingerprint(seg_path: str | Path) -> str:
    """登记任务节点指纹 = 段内容 + **规则摘要** + **候选主名表模板** + 模型配置。

    模板必须进指纹：登记用 `候选主名表模板.md` 组请求，只按段内容判断点时，改模板后
    候选表会以「已完成（断点跳过）」沿用旧产物（与 `extract_segments` 同一处坑）。
    规则摘要（`RULESET_VERSION`）同理参与（R09-D07）。
    """
    return task_node_fingerprint(seg_path, rule_version=RULESET_VERSION,
                                 template_version=file_hash(TMPL_CANDIDATE) or "")


def candidate_one(seg_path: str, out_dir: Path, force: bool, db: IndexDB) -> tuple[str | None, str]:
    text = read_text(seg_path)
    meta, body = parse_frontmatter(text)
    novel = meta.get("小说名称", "").strip("《》")
    author = meta.get("作者", "")
    seg_name = Path(seg_path).stem
    out_path = out_dir / f"{seg_name}-候选表.md"
    seg_hash = file_hash(seg_path)
    node_fp = segment_task_fingerprint(seg_path)

    row = db.get(seg_path)
    if row and row[2] == "failed" and row[3] >= 3 and not force:
        log(f"  ⏭️  {seg_name}: 已连续失败 {row[3]} 次（跳过；--force 强制重试）")
        return None, "failed"

    if not force and db.should_skip(seg_path, out_path, seg_hash, node_fp):
        return str(out_path), "skipped"

    if out_path.exists() and not force:
        # AE-01: 能走到这里说明断点指纹未命中（指纹不匹配/无记录/内容已变），
        # 旧候选表已不可信——强制失效后重扫，禁止盲目复用陈旧候选表。
        try:
            out_path.unlink()
        except OSError:
            pass
        log(f"  🔄 {seg_name}: 任务指纹失效，旧候选表已作废并重新扫描")

    db.mark_pending(seg_path, out_path, seg_hash)
    try:
        system, user_tmpl = split_template(TMPL_CANDIDATE)
    except ValueError as e:
        log(f"  ❌ {seg_name}: {e}")
        db.mark_failed(seg_path, str(e))
        return None, "failed"

    user_prompt = user_tmpl.format(
        novel=novel,
        author=author,
        chapter_range=meta.get("章节范围", ""),
        numbered=number_lines(body),
    )
    content, usage = call_llm(system, user_prompt, min_len=50, step="全局登记", role="judgement")
    out_path.write_text(content + "\n", encoding="utf-8")
    db.mark_done(seg_path, out_path, usage.get("prompt_tokens", 0),
                 usage.get("completion_tokens", 0), node_fp=node_fp)
    log(f"  ✅ 候选表 {out_path.name}（{len(content)} 字符）")
    return str(out_path), "done"


def clean_aliases(text: str) -> str:
    """Clear placeholder text from alias column in candidate/registry tables."""
    out = []
    for line in text.splitlines():
        if line.startswith("|") and not line.startswith("|---"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2 and cells[0] not in ("候选主名", "主名", "资产ID"):
                if cells[1] in PLACEHOLDER_ALIAS:
                    cells[1] = ""
                line = "| " + " | ".join(cells) + " |"
        out.append(line)
    return "\n".join(out)


def _merge_pair_hash(a_path: Path, b_path: Path) -> str:
    """Deterministic hash over the two input tables of a merge pair."""
    ha = file_hash(a_path) or ""
    hb = file_hash(b_path) or ""
    return hashlib.sha256(f"{ha}|{hb}".encode("utf-8")).hexdigest()[:16]


def merge_pair(a_path: Path, b_path: Path, out_path: Path, force: bool) -> tuple[str | None, str]:
    pair_hash = _merge_pair_hash(a_path, b_path)
    hash_path = out_path.parent / (out_path.name + ".hash")
    if not force and out_path.exists() and out_path.stat().st_size > 50:
        if hash_path.exists() and hash_path.read_text(encoding="utf-8").strip() == pair_hash:
            return str(out_path), "skipped"
        if not hash_path.exists():
            log(f"  ℹ️  {out_path.name}: 旧合并产物无哈希记录（保守跳过；--force 可全量重跑）")
            return str(out_path), "skipped"
    system, user_tmpl = split_template(TMPL_MERGE)
    user_prompt = user_tmpl.format(
        novel="",
        author="",
        a_range=range_label(a_path),
        a_table=clean_aliases(read_text(a_path)),
        b_range=range_label(b_path),
        b_table=clean_aliases(read_text(b_path)),
    )
    content, usage = call_llm(system, user_prompt, min_len=50, step="对账并入", role="judgement")
    out_path.write_text(content + "\n", encoding="utf-8")
    hash_path.write_text(pair_hash, encoding="utf-8")
    log(f"  🔀 合并 {a_path.name} + {b_path.name} → {out_path.name}（{len(content)} 字符）")
    return str(out_path), "done"


def merge_round(
    round_no: int,
    files: list[Path],
    merge_dir: Path,
    force: bool,
    workers: int,
) -> tuple[list[Path], int]:
    """One round of pairwise merges. Returns (next round files, carry count)."""
    pairs: list[tuple[Path, Path | None]] = [
        (files[i], files[i + 1]) for i in range(0, len(files) - 1, 2)
    ]
    if len(files) % 2 == 1:
        pairs.append((files[-1], None))
    next_files: list[Path | None] = [None] * len(pairs)
    carry_count = 0

    def run(idx: int, a: Path, b: Path | None) -> tuple[int, Path | None, str, bool]:
        if b is None:
            return idx, a, "carry-single", False
        out_path = merge_dir / f"merge_r{round_no}_{idx:03d}.md"
        try:
            out, st = merge_pair(a, b, out_path, force)
            return idx, Path(out) if out else None, st, False
        except Exception as e:
            carry_count_local = True
            log(f"  ❌ 合并 {a.name} + {b.name} 失败（carry 到下一轮）: {e}")
            return idx, a, "failed", carry_count_local

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(run, idx, a, b) for idx, (a, b) in enumerate(pairs)]
        for fut in as_completed(futs):
            idx, path, _st, carried = fut.result()
            next_files[idx] = path
            if carried:
                carry_count += 1
    return [f for f in next_files if f], carry_count


def include_claims(lib: Path, group: str, report_path: Path) -> int:
    """Merge verified [新增] claims from reconcile report into the registry."""
    registry_path = lib / f"{group}-登记表.md"
    if not report_path.exists():
        print(f"❌ 对账报告不存在: {report_path}（先跑 aggregate.py）")
        return 1
    if not registry_path.exists():
        print(f"❌ 登记表不存在: {registry_path}（先跑登记表生成）")
        return 1

    text = report_path.read_text(encoding="utf-8")
    existing_names: set[str] = set()
    for line in registry_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("|") and not line.startswith("|---"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells and cells[0] not in ("候选主名", "主名", "资产ID"):
                existing_names.add(cells[0])

    added = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"- \[新增\]申报: 「(.+?)」（(.+?)，(.+?)）出处校验通过", line)
        if not m:
            continue
        name, typ, chs = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        if name in existing_names:
            continue
        first_ch = chs.split("、")[0] if chs else ""
        added.append(f"| {name} | | {first_ch} | {typ}资产（[新增]申报并入） | {typ} |")

    if not added:
        print("ℹ️  无新增可并入（全部已在登记表，或无出处通过的申报）")
        return 0

    existing = registry_path.read_text(encoding="utf-8")
    if existing and not existing.endswith("\n"):
        existing += "\n"
    registry_path.write_text(existing + "\n".join(added) + "\n", encoding="utf-8")
    print(f"✅ 已并入 {len(added)} 条 [新增] 申报 → {registry_path}")
    return 0


def process_group(group: str, seg_files: list[str], lib: Path, force: bool, workers: int) -> bool:
    """Candidate pass → hierarchical merge → registry file."""
    cand_dir = lib / "登记表" / group / "候选表"
    merge_dir = lib / "登记表" / group / "合并"
    cand_dir.mkdir(parents=True, exist_ok=True)
    merge_dir.mkdir(parents=True, exist_ok=True)

    print(f"📚 {group}: 候选遍 {len(seg_files)} 段 → 分层聚合 → 登记表")
    t0 = time.time()

    cand_files = []
    with IndexDB(lib, table="registry_tasks") as db:
        if not db.ready:
            print(f"❌ 断点库初始化失败，无法继续处理 {group}")
            return False
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(candidate_one, p, cand_dir, force, db): p for p in seg_files}
            for fut in as_completed(futs):
                p = futs[fut]
                try:
                    out, _st = fut.result()
                    if out:
                        cand_files.append(Path(out))
                except Exception as e:
                    db.mark_failed(p, str(e))
                    log(f"  ❌ {Path(p).name} 候选失败: {e}")

    if not cand_files:
        print("  ⚠️ 无候选表产出")
        return False

    files = sorted(cand_files)
    round_no = 0
    total_carries = 0
    while len(files) > 1:
        round_no += 1
        print(f"  🔀 合并轮 {round_no}: {len(files)} 张表 → {len(files) // 2 + len(files) % 2} 张")
        files, carries = merge_round(round_no, files, merge_dir, force, workers)
        total_carries += carries
        files = sorted(files)
        if len(files) >= len(cand_files) and round_no > 1:
            print("  ⚠️ 合并轮未收敛，中止")
            return False

    if len(files) != 1:
        print("  ⚠️ 合并未收敛到单表")
        return False

    registry_path = lib / f"{group}-登记表.md"
    content = clean_aliases(read_text(files[0]))
    carry_note = ""
    if total_carries > 0:
        carry_note = (
            f"> ⚠️ 合并未完全：{total_carries} 对候选表合并失败被原样带入（carry），"
            f"本表部分候选未经全局合并——锚点可靠性降级，请重跑（--force）或人工核对。\n"
        )
        print(f"  ⚠️ 合并 carry {total_carries} 对——登记表含未合并候选，锚点可靠性降级（重跑 --force 修复）")
    header = (
        f"# 全局资产登记表\n\n"
        f"> 生成: scan_registry.py（并行候选 + 分层聚合, ADR-0003）| 段级提取的锚点\n"
        f"{carry_note}"
        f"> 主名 = 原文词汇（主名规则）；段级提取必须引用本表主名；疑似新资产标 [新增] 严格申报。\n\n"
    )
    registry_path.write_text(header + content, encoding="utf-8")
    rows = sum(
        1
        for line in content.splitlines()
        if line.startswith("|") and not line.startswith("|---") and "候选主名" not in line
    )
    print(f"✅ 登记表: {registry_path}（{rows} 条资产主名, 耗时 {time.time()-t0:.0f}s）")
    return total_carries == 0


def _collect_segment_facts(lib: Path, seg_dirs: list[str]) -> int:
    """工单 1：扫描各段正文的确定性源事实并落 .process/source-facts.jsonl（附加产物）。"""
    facts: list[dict] = []
    for d in seg_dirs:
        seg_dir = Path(d)
        if not seg_dir.is_dir():
            continue
        for f in sorted(seg_dir.iterdir()):
            if f.suffix != ".md" or f.name.startswith(".") or "候选表" in f.name or "资产记录" in f.name:
                continue
            _meta, body = parse_frontmatter(read_text(f))
            for fact in _source_facts.scan_deterministic_facts(
                body, source_file=f.name, segment_id=f.stem
            ):
                facts.append(fact)
    return _source_facts.dump_source_facts_jsonl(
        facts, lib / ".process" / "source-facts.jsonl"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="全局资产登记表生成器（并行候选 + 分层聚合）")
    parser.add_argument("segments", nargs="*", help="原文段目录（资产库/原文段/<小说>-<作者>）")
    parser.add_argument("--lib", default=str(ASSET_LIB_DIR), help="资产库根目录")
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--force", action="store_true", help="覆盖已存在产物（重跑）")
    parser.add_argument("--include-claims", metavar="对账报告.md", help="闭环：把对账报告'出处校验通过'的 [新增] 申报并入登记表")
    args = parser.parse_args()

    if args.include_claims:
        group = None
        first_line = Path(args.include_claims).read_text(encoding="utf-8").splitlines()[0]
        m = re.search(r"（(.+)）", first_line)
        if m:
            group = m.group(1).strip()
        if not group:
            print("❌ 无法从对账报告推断小说名-作者（首行需为 # 登记表对账报告（<小说>-<作者>））")
            return 1
        return include_claims(Path(args.lib), group, Path(args.include_claims))

    if not os.environ.get("OPENAI_API_KEY"):
        print("❌ 未设置 OPENAI_API_KEY（请复制 .env.example 为 .env 并填入）")
        return 1

    lib = Path(args.lib)
    all_ok = True
    for d in args.segments:
        seg_dir = Path(d)
        if not seg_dir.is_dir():
            print(f"❌ 目录不存在: {d}")
            all_ok = False
            continue
        group = str(seg_dir.relative_to(lib / "原文段"))
        seg_files = sorted(
            str(f)
            for f in seg_dir.iterdir()
            if f.suffix == ".md" and not f.name.startswith(".") and "候选表" not in f.name and "资产记录" not in f.name
        )
        if not seg_files:
            print(f"⚠️ {group}: 无段文件")
            continue
        ok = process_group(group, seg_files, lib, args.force, args.workers)
        all_ok = all_ok and ok

    # 工单 1：附加落机器可读源事实（失败不阻断旧登记表链路）
    try:
        n = _collect_segment_facts(lib, args.segments)
        print(f"✅ 源事实: .process/source-facts.jsonl（{n} 条）")
    except Exception as e:  # noqa: BLE001 — 附加产物，任何异常都不影响主流程
        print(f"⚠️ source-facts 落盘失败（已忽略）: {e}")

    print("\n✅ 完成" if all_ok else "\n⚠️ 部分失败（重跑同一命令续传）")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
