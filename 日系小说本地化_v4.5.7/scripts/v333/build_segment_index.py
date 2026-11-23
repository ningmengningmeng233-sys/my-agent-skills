#!/usr/bin/env python3
"""
章节索引直调生成器 v3.32（ADR-0001）

每个段文件 → 一个线程 → 直调 LLM API 生成章节索引。
- 32 路并行（ThreadPoolExecutor），429 自动重试退避
- SQLite 断点层（index_db.py）：done 跳过、失败写原因、中断重跑零浪费
- --skip-existing：目标索引文件已存在则跳过（文件即状态兜底）
- 行号精度：段文件逐行加行号前缀传入；生成后自动跑行号校准
- prompt 模板：templates/直调任务模板.md（单源真相）

v3.32 变更：
- 并发默认 16 → 32（DeepSeek 并发上限 2500，429 风险≈0）
- max_tokens 12000 → 20000（堵输出截断）
- 流式输出（stream=True + SSE 解析，跳过 keep-alive 注释行）
- token 消耗统计（usage 记入断点库，结束打印汇总）
- 段文件内容 hash 检测（追更改写段文件 → 自动重跑，防行号漂移）

配置（技能目录 .env，见 .env.example）:
  OPENAI_API_KEY / SUMMARY_API_URL / SUMMARY_API_MODEL / INDEX_LIB_DIR

用法:
  python build_segment_index.py "<段文件路径>" ... [--lib <索引库根目录>] [--workers 32] [--skip-existing] [--no-fix]
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # scripts/v333/ → skill 根
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 本目录（v333/），供 index_db 导入

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(SKILL_DIR, ".env"))
except Exception:
    pass

import requests
from index_db import IndexDB, file_hash

API_URL = os.environ.get("SUMMARY_API_URL", "https://api.openai.com/v1/chat/completions")
API_KEY = os.environ.get("OPENAI_API_KEY", "")
API_MODEL = os.environ.get("SUMMARY_API_MODEL", "gpt-4o-mini")
INDEX_LIB_DIR = os.environ.get("INDEX_LIB_DIR", "").strip() or os.path.join(SKILL_DIR, "索引库")

TEMPLATE_FILE = os.path.join(SKILL_DIR, "templates", "直调任务模板.md")
CHAPTER_TMPL = os.path.join(SKILL_DIR, "templates", "章节索引模板.md")
VOCAB_FILE = os.path.join(SKILL_DIR, "templates", "包含元素标签词汇表.md")
OUT_DIR = os.path.join(INDEX_LIB_DIR, "章节索引")

_print_lock = Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def parse_frontmatter(text):
    meta = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[3:end]
            body = text[end + 4 :]
            for line in fm.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"')
    return meta, body


CHAPTER_RE = re.compile(
    r"^第[一二三四五六七八九十百零0-9]+[章节回卷话集][^\n]{0,40}"
    r"|^(?:Chapter|Book|Volume|Episode|Part)\s+\d+[^\n]{0,40}"
    r"|^\d+[\.\、．]\s[^\n]{0,40}"
    r"|^(?:序章|楔子|终章|番外|间章|尾声|后记)[^\n]{0,40}",
    re.M,
)


def extract_chapters(body):
    return [m.group(0).strip() for m in CHAPTER_RE.finditer(body)]


def number_lines(text):
    lines = text.splitlines()
    return "\n".join(f"{i+1:05d}: {line}" for i, line in enumerate(lines))


def call_llm(system_prompt, user_prompt, max_tokens=20000, retries=3):
    """流式直调 LLM。返回 (content, usage)；429 退避 10/20/30s，错误退避 5/10/15s。"""
    payload = {
        "model": API_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.4,
        "stream": True,
    }
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.post(
                API_URL, json=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
                timeout=(30, 600), stream=True,
            )
            if resp.status_code == 429:
                resp.close()
                wait = 10 * (attempt + 1)
                log(f"    ⏳ 429 限流，等待 {wait}s 重试...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            parts, usage = [], {}
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue  # 请求保活空行
                line = raw.strip()
                if not line.startswith("data:"):
                    continue  # 跳过 SSE keep-alive 注释（: keep-alive）
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for ch in chunk.get("choices", []):
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        parts.append(delta["content"])
                if chunk.get("usage"):
                    usage = chunk["usage"]
            content = "".join(parts).strip()
            if not content:
                raise ValueError("流式响应为空")
            return content, usage
        except Exception as e:
            last_err = e
            wait = 5 * (attempt + 1)
            log(f"    ⚠️ 调用失败({e})，{wait}s 后重试 ({attempt+1}/{retries})")
            time.sleep(wait)
    raise last_err


def build_one(seg_path, out_dir, skip_existing, do_fix, db):
    text = read_text(seg_path)
    meta, body = parse_frontmatter(text)
    novel = meta.get("小说名称", "").strip("《》")
    author = meta.get("作者", "")
    seg_name = os.path.basename(seg_path).replace(".md", "")
    out_path = os.path.join(out_dir, seg_name + "-idx.md")
    seg_hash = file_hash(seg_path)

    # 断点：DB done + 文件存在 + 内容 hash 一致 → 跳过（追更改写段文件会自动重跑）
    if db.should_skip(seg_path, out_path, seg_hash):
        log(f"  ⏭️  {seg_name}: 已完成（断点跳过）")
        return out_path, "skipped"
    if skip_existing and os.path.exists(out_path):
        log(f"  ⏭️  {seg_name}: 索引文件已存在（--skip-existing）")
        return out_path, "skipped"

    db.mark_pending(seg_path, out_path, seg_hash)
    chapters = extract_chapters(body)
    numbered = number_lines(text)

    try:
        template = read_text(TEMPLATE_FILE)
        chapter_tmpl = read_text(CHAPTER_TMPL)
        vocab = read_text(VOCAB_FILE)
    except FileNotFoundError as e:
        log(f"  ❌ {seg_name}: 缺少模板文件 {e}")
        db.mark_failed(seg_path, f"missing template: {e}")
        return None, "failed"

    log(f"  [{seg_name}] 开始: {novel} | {meta.get('章节范围','')} | {len(chapters)} 章 | {len(text)} 字符")

    # 从模板文件构造 prompt（单源真相）
    parts = template.split("## 系统提示\n", 1)
    if len(parts) != 2:
        db.mark_failed(seg_path, "直调任务模板格式错误")
        return None, "failed"
    system_prompt = parts[1].split("\n## 用户输入模板\n", 1)[0].strip()
    user_tmpl = parts[1].split("\n## 用户输入模板\n", 1)[1].strip()

    user_prompt = user_tmpl.format(
        seg_path=seg_path,
        novel=novel,
        author=author,
        chapter_range=meta.get("章节范围", ""),
        chapter_count=len(chapters),
        chapters="\n".join(chapters),
        template=chapter_tmpl,
        vocab=vocab,
        numbered=numbered,
    )

    content, usage = call_llm(system_prompt, user_prompt)
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content + "\n")

    # 自动行号校准 + 一致性检查（失败不阻塞）
    if do_fix:
        try:
            from verify_and_fix_lines import process_file, check_consistency
            process_file(out_path)
            check_consistency(out_path)
        except Exception as e:
            log(f"    ⚠️ 行号校准/一致性检查异常: {e}")

    db.mark_done(seg_path, out_path, prompt_tokens, completion_tokens, seg_hash)
    log(f"  ✅ {os.path.basename(out_path)} 已生成（{len(content)} 字符 | prompt {prompt_tokens} | completion {completion_tokens}）")
    return out_path, "done"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("segments", nargs="+", help="段文件路径（可多个，也可传目录）")
    parser.add_argument("--lib", default=INDEX_LIB_DIR, help="索引库根目录（默认 INDEX_LIB_DIR/.env 或技能目录/索引库）")
    parser.add_argument("--out", help="章节索引输出目录（默认 <索引库>/章节索引）")
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--skip-existing", action="store_true", help="目标索引文件已存在则跳过")
    parser.add_argument("--no-fix", action="store_true", help="跳过行号校准")
    args = parser.parse_args()

    if not API_KEY:
        print("❌ 未设置 OPENAI_API_KEY（请复制 .env.example 为 .env 并填入）")
        return 1

    # 展开目录参数为段文件列表
    seg_files = []
    for s in args.segments:
        if os.path.isdir(s):
            seg_files.extend(
                os.path.join(s, f)
                for f in sorted(os.listdir(s))
                if f.endswith(".md") and not f.endswith("-idx.md")
            )
        else:
            seg_files.append(s)

    out_dir = args.out or os.path.join(args.lib, "章节索引")
    os.makedirs(out_dir, exist_ok=True)
    for d in ("小说原文", "小说索引", "章节索引"):
        os.makedirs(os.path.join(args.lib, d), exist_ok=True)

    print(f"📚 待处理 {len(seg_files)} 个段 → 输出: {out_dir}")
    print(f"🔄 直调模式（{args.workers} workers, 模型: {API_MODEL}, 断点: SQLite, 流式: 开）...")

    t0 = time.time()
    ok = skipped = fail = 0
    with IndexDB(args.lib) as db:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {
                ex.submit(build_one, p, out_dir, args.skip_existing, not args.no_fix, db): p
                for p in seg_files
            }
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
                    log(f"  ❌ {os.path.basename(p)} 失败: {e}")
                    try:
                        with IndexDB(args.lib) as db2:
                            db2.mark_failed(p, str(e))
                    except Exception:
                        pass
        _, (sum_prompt, sum_comp) = db.stats()

    elapsed = time.time() - t0
    print(f"\n{'='*50}")
    print(f"📊 完成: 生成 {ok}, 跳过 {skipped}, 失败 {fail}, 耗时 {elapsed:.0f}s")
    if ok:
        print(f"💰 token 统计: prompt 共 {sum_prompt:,} / completion 共 {sum_comp:,}（均值 prompt {sum_prompt//max(ok,1):,}、completion {sum_comp//max(ok,1):,} 每段）")
    if fail:
        print("ℹ️  失败段已记录到索引库断点库，重跑同一命令即可续传（重试已计数）")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
