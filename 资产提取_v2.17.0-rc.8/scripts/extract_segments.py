#!/usr/bin/env python3
"""Segment-level asset extraction direct-call engine with SQLite checkpointing."""
import argparse
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
sys.path.insert(0, str(SKILL_DIR))

from engine.common import read_text, parse_frontmatter, number_lines
from engine import segment_format
from engine.llm_pool import append_event
# 备注标签解析单一事实源在 engine.extract_tags（此处再导出，保持旧导入路径兼容）
from engine.extract_tags import parse_metadata_tags_from_remark  # noqa: F401  (兼容导出)
from engine.llm import call_llm, log, WORKERS
from engine.index_db import IndexDB, RULESET_VERSION, file_hash, task_node_fingerprint
if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ASSET_LIB_DIR = Path(os.environ.get("ASSET_LIB_DIR", "").strip() or SKILL_DIR / "资产库")
TEMPLATE_FILE = SKILL_DIR / "templates" / "段级提取模板.md"
REGISTRY_BUDGET = 12000


def segment_task_fingerprint(seg_path: str | Path) -> str:
    """提取任务的节点指纹＝源文哈希 + **规则摘要** + **段级提取模板哈希** + 模型配置摘要。

    模板必须参与：它决定提示词与标签契约（`[状态:…]`、`[连接:…]` 的写法都在里面）。
    不参与就会出现"改了模板、产物却以『已完成（断点跳过）』的身份继续被当作正确"——
    实测：模板里 `[状态:受损]` 这个错误示例修好后重跑，提取被整段跳过，
    索引里那条 `STATE_OF→受损` 照样把 verify 卡在 `BLOCKED`。

    规则摘要（`RULESET_VERSION`）同理参与：规则改动静默失效是同一处坑（R09-D07）。
    """
    return task_node_fingerprint(seg_path, rule_version=RULESET_VERSION,
                                 template_version=file_hash(TEMPLATE_FILE) or "")


def build_one(
    seg_path: str,
    out_dir: Path,
    skip_existing: bool,
    db: IndexDB,
    force_retry: bool = False,
) -> tuple[str | None, str]:
    text = read_text(seg_path)
    meta, body = parse_frontmatter(text)
    novel = meta.get("小说名称", "").strip("《》")
    author = meta.get("作者", "")
    seg = Path(seg_path)
    seg_name = seg.stem
    out_path = out_dir / f"{seg_name}-资产记录.md"
    seg_hash = file_hash(seg_path)

    row = db.get(seg_path)
    if row and row[2] == "failed" and row[3] >= 3 and not force_retry:
        log(f"  ⏭️  {seg_name}: 已连续失败 {row[3]} 次（跳过；--force-retry 强制重试）")
        return None, "failed"

    node_fp = segment_task_fingerprint(seg_path)
    if db.should_skip(seg_path, out_path, seg_hash, node_fp):
        log(f"  ⏭️  {seg_name}: 已完成（断点跳过）")
        return str(out_path), "skipped"
    if skip_existing and out_path.exists() and out_path.stat().st_size > 100:
        log(f"  ⏭️  {seg_name}: 产物文件已存在（--skip-existing）")
        return str(out_path), "skipped"

    db.mark_pending(seg_path, out_path, seg_hash)
    log(f"  [{seg_name}] 开始: {novel} | {meta.get('章节范围', '')} | {len(text)} 字符")

    try:
        template = read_text(TEMPLATE_FILE)
    except FileNotFoundError as e:
        log(f"  ❌ {seg_name}: 缺少模板文件 {e}")
        db.mark_failed(seg_path, f"missing template: {e}")
        return None, "failed"

    parts = template.split("## 系统提示\n", 1)
    if len(parts) != 2:
        db.mark_failed(seg_path, "段级提取模板格式错误")
        return None, "failed"
    system_prompt = parts[1].split("\n## 用户输入模板\n", 1)[0].strip()
    user_tmpl = parts[1].split("\n## 用户输入模板\n", 1)[1].strip()

    registry_table = ""
    lib_root = out_dir.parent.parent
    registry_path = lib_root / f"{novel}-{author}-登记表.md"
    if registry_path.exists():
        rows = []
        for line in read_text(registry_path).splitlines():
            if line.startswith("|") and not line.startswith("|---"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if len(cells) >= 5 and cells[0] not in ("候选主名", "主名", "资产ID"):
                    rows.append(("| " + " | ".join(cells[:5]) + " |", cells[0]))
        selected, dropped_names = [], []
        total = 0
        for row, name in rows:
            sep = 1 if selected else 0
            if total + len(row) + sep <= REGISTRY_BUDGET:
                selected.append(row)
                total += len(row) + sep
            else:
                dropped_names.append(name)
        registry_table = "\n".join(selected)
        if rows:
            log(f"  📌 锚点注入: {registry_path.name}（{len(rows)} 条主名，注入 {len(selected)} 条 / 预算 {REGISTRY_BUDGET} 字符")
            if dropped_names:
                tail = ", ".join(dropped_names[:10]) + (" 等" if len(dropped_names) > 10 else "")
                log(f"  ✂️  登记表锚点截断（预算不足）: {tail}（共 {len(dropped_names)} 条）")

    user_prompt = user_tmpl.format(
        novel=novel,
        author=author,
        chapter_range=meta.get("章节范围", ""),
        registry_table=registry_table,
        numbered=number_lines(body),
    )

    content, usage = call_llm(system_prompt, user_prompt, min_len=100, step="段级提取", role="judgement")
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    # 段级格式拦截（工单 T8）：用与聚合同一份解析器就地自检；不合格带纠正提示重试
    # 一次，仍不合格以稳定码点名段 ID，不让坏格式流向聚合阶段。
    lib_root2 = out_dir.parent.parent
    try:
        segment_format.parse_summary_rows(content)
    except segment_format.SegmentFormatError as e:
        append_event(lib_root2, "segment_format_retry", seg_name, e.code)
        log(f"  ⚠️ {seg_name}: 段产物格式不符（{e.code}），带纠正提示重试一次")
        correction = (
            "\n\n【格式纠正】你上一轮的输出中有表格行不符合要求。"
            "章节摘要表每行必须是「| 第X章…| 概要 | 人物 | 场景 | 道具 | 服装 |」（章节列必须以「第」开头）；"
            "资产出现记录表每行必须是「| 类型 | 名称 | 别名 | 出现章节 | 摘句 | 行号 | 备注 |」。"
            "请重新输出完整内容，不要输出这三张表之外的表格行。"
        )
        content, usage = call_llm(system_prompt, user_prompt + correction, min_len=100, step="段级提取", role="judgement")
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        try:
            segment_format.parse_summary_rows(content)
        except segment_format.SegmentFormatError as e2:
            append_event(lib_root2, "segment_format_invalid", seg_name, e2.code)
            db.mark_failed(seg_path, f"SEGMENT_FORMAT_INVALID: {e2.code}")
            raise segment_format.SegmentFormatError(f"{seg_name}: {e2.code}") from e2

    out_path.write_text(content + "\n", encoding="utf-8")

    db.mark_done(seg_path, out_path, prompt_tokens, completion_tokens, seg_hash, node_fp=node_fp)
    log(f"  ✅ {out_path.name} 已生成（{len(content)} 字符 | prompt {prompt_tokens} | completion {completion_tokens}）")
    return str(out_path), "done"


def main() -> int:
    parser = argparse.ArgumentParser(description="段级资产提取直调引擎")
    parser.add_argument("segments", nargs="+", help="段文件路径（可多个，也可传目录）")
    parser.add_argument("--lib", default=str(ASSET_LIB_DIR), help="资产库根目录")
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--skip-existing", action="store_true", help="产物文件已存在则跳过")
    parser.add_argument("--force-retry", action="store_true", help="强制重试连续失败 ≥3 次的段")
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("❌ 未设置 OPENAI_API_KEY（请复制 .env.example 为 .env 并填入）")
        return 1

    asset_lib = Path(args.lib)
    seg_files = []
    for s in args.segments:
        p = Path(s)
        if p.is_dir():
            seg_files.extend(
                str(f)
                for f in sorted(p.iterdir())
                if f.suffix == ".md" and "资产记录" not in f.name and not f.name.startswith(".")
            )
        else:
            seg_files.append(s)
    if not seg_files:
        print("❌ 未找到段文件（请先运行 split_segments.py 拆段）")
        return 1

    groups: defaultdict[str, list[str]] = defaultdict(list)
    for p in seg_files:
        rel = Path(p).parent.relative_to(asset_lib / "原文段")
        groups[str(rel)].append(p)

    (asset_lib / "段产物").mkdir(parents=True, exist_ok=True)
    total_ok = total_skip = total_fail = 0
    for group, files in groups.items():
        out_dir = asset_lib / "段产物" / group
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"📚 {group}: 待处理 {len(files)} 个段 → 输出: {out_dir}")
        print(f"🔄 直调模式（{args.workers} workers, 断点: SQLite, 流式: 开）...")
        t0 = time.time()
        ok = skipped = fail = 0
        with IndexDB(args.lib) as db:
            if not db.ready:
                print(f"❌ 断点库初始化失败，无法继续处理 {group}")
                return 1
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futures = {ex.submit(build_one, p, out_dir, args.skip_existing, db, args.force_retry): p for p in files}
                for fut in as_completed(futures):
                    p = futures[fut]
                    try:
                        _, status = fut.result()
                        if status == "done":
                            ok += 1
                        elif status == "skipped":
                            skipped += 1
                        else:
                            fail += 1
                    except Exception as e:
                        fail += 1
                        log(f"  ❌ {Path(p).name} 失败: {e}")
                        db.mark_failed(p, str(e))
            _, (sum_prompt, sum_comp) = db.stats()
        elapsed = time.time() - t0
        print(f"\n{'='*50}")
        print(f"📊 {group}: 生成 {ok}, 跳过 {skipped}, 失败 {fail}, 耗时 {elapsed:.0f}s")
        if ok:
            print(f"💰 token: prompt 共 {sum_prompt:,} / completion 共 {sum_comp:,}")
        if fail:
            print("ℹ️  失败段已记录断点库，重跑同一命令即可续传")
        total_ok += ok
        total_skip += skipped
        total_fail += fail

    print(f"\n✅ 全部完成: 生成 {total_ok}, 跳过 {total_skip}, 失败 {total_fail}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
