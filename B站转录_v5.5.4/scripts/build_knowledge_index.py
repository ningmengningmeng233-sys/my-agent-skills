#!/usr/bin/env python3
"""
转录文档知识索引生成器（B+C 方案）
  B: 规则引擎初筛 — 技术词典命中 + 高频英文术语词频，零成本抽取候选知识点
  C: 按视频为分析单元，11 路并行调 DeepSeek 生成结构化知识索引
输出: <输出目录>/<BV>-知识索引.md + 知识索引总览.md
用法:
  python build_knowledge_index.py            # 全部视频（默认 BV_IDS）
  python build_knowledge_index.py --limit 1  # 只处理前 N 个（测试用）
  python build_knowledge_index.py --bvid BVxxxx --out "D:\\路径"  # 单视频+自定义输出目录
"""
import argparse
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(SKILL_DIR, ".env"))
except Exception:
    pass

import requests
from transcript_db import TranscriptDB

API_URL = os.environ.get("SUMMARY_API_URL", "https://api.openai.com/v1/chat/completions")
API_KEY = os.environ.get("OPENAI_API_KEY", "")
API_MODEL = os.environ.get("SUMMARY_API_MODEL", "gpt-4o-mini")
MAX_CHARS = 26000  # 单视频全文截断（约2.5万token，deepseek-chat 64K上下文内）

# ---- 本批 AI绘画/ComfyUI 主题视频的技术词典（规则初筛用） ----
TECH_TERMS = [
    "ComfyUI", "LoRA", "Stable Diffusion", "SDXL", "SD1.5", "Flux", "FP8", "FP16", "BF16",
    "INT8", "GGUF", "ConvRot", "SEEDVR2", "Embedding", "Dreambooth", "ControlNet", "TensorRT",
    "CLIP", "VAE", "UNet", "Checkpoint", "ckpt", "MiniMax", "H3", "海螺", "量化", "蒸馏",
    "显存", "VRAM", "超分", "放大", "加速", "注意力", "采样器", "调度器", "工作流", "节点",
    "云端", "部署", "整合包", "插件", "安装", "升级", "模型下载", "炼丹", "训练集", "标签",
    "正则化", "学习率", "epoch", "batch", "分辨率", "Lanczos", "文生图", "图生图", "多图",
    "4K", "视频", "修复", "高清", "精度", "性能", "速度", "显存占用", "内存", "显卡", "N卡",
]

STOP_WORDS = set("的 了 是 我 你 他 她 它 们 这 那 在 有 和 也 就 都 而 及 与 或 一个 这个 那个 什么 怎么 可以 需要 进行 我们 你们 大家 时候 现在 然后 但是 因为 所以 如果 还是 这样 那样 一下 一些 这些 那些 一个 不是 没有 比较 非常 应该 可能 已经 通过 使用 直接 下载 安装 打开 点击 选择 设置 看到 知道 觉得 里面 上面 下面 一边 方面 方式 方法 东西 视频 教程 同学 朋友".split())

_print_lock = Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def rule_extract(text, title):
    """规则初筛：词典命中 + 高频英文术语"""
    haystack = (text + "\n" + title).lower()
    dict_hits = sorted({t for t in TECH_TERMS if t.lower() in haystack})

    words = re.findall(r"[a-zA-Z][a-zA-Z0-9._\-]{2,}", text)
    freq = Counter(w.lower() for w in words)
    freq_terms = [w for w, c in freq.most_common(30) if c >= 5 and w not in dict_hits]

    return dict_hits, freq_terms


def call_llm(system_prompt, user_prompt, max_tokens=2200, retries=3):
    payload = {
        "model": API_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
    }
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.post(
                API_URL, json=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
                timeout=120,
            )
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices", [])
            if not choices or "message" not in choices[0]:
                raise ValueError(f"API 返回格式异常: {data}")
            return choices[0]["message"]["content"].strip()
        except Exception as e:
            last_err = e
            time.sleep(3 * (attempt + 1))
    raise last_err


SYSTEM_PROMPT = (
    "你是视频教程知识索引专家。根据给定的视频转录文本，产出结构化知识索引（Markdown）。\n"
    "要求：\n"
    "1. 全部用中文输出，简洁精炼\n"
    "2. 严格按以下格式输出（不要加额外说明）：\n"
    "---\n"
    "## 知识点\n"
    "- 名称 | 类别(工具/概念/操作/参数/技巧) | 一句话说明\n"
    "（覆盖该视频全部重要知识点，宁多勿漏，每条说明不超过25字）\n"
    "---\n"
    "## 关键流程/步骤\n"
    "- 步骤1\n- 步骤2\n（如视频含操作流程则按顺序列出，否则写：无）\n"
    "---\n"
    "## 核心术语\n"
    "- 术语1、术语2……（逗号分隔，仅列本视频实际出现的）"
)


def build_one(bv, title, author, upload_date, duration, text):
    dict_hits, freq_terms = rule_extract(text, title)
    log(f"  [{bv}] 规则初筛: 词典命中 {len(dict_hits)} 个, 高频词 {len(freq_terms)} 个")

    truncated = text[:MAX_CHARS]
    user_prompt = (
        f"视频标题：{title}\n作者：{author}\n发布时间：{upload_date}\n时长：{duration}\n"
        f"BV号：{bv}\n\n"
        f"规则初筛候选术语（请核对哪些确实出现在本视频中，并补充遗漏的）：\n"
        f"{', '.join(dict_hits + freq_terms) if dict_hits or freq_terms else '（无）'}\n\n"
        f"转录全文（可能截断）：\n{truncated}"
    )
    index = call_llm(SYSTEM_PROMPT, user_prompt)
    return index


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bvid", help="只处理单个视频")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个（测试）")
    parser.add_argument("--out", default=r"D:\桌面\Obsidian入门启动包\b站视频转录，ai部分\索引",
                        help="输出目录")
    parser.add_argument("--workers", type=int, default=11)
    args = parser.parse_args()

    if not API_KEY:
        print("❌ 未设置 OPENAI_API_KEY（.env）")
        return 1

    BV_IDS = [
        "BV16e411e7Zx", "BV1TK411v7Jw", "BV1VUM1zdEyh", "BV1f8un61ET8",
        "BV1MF3K6oEwT", "BV1CDNy6yETK", "BV1Qx7T6JEwi", "BV1vMEX6pEdx",
        "BV1mgEc6FEiF", "BV1oBGa6SEty", "BV1Wyuc6eET1",
    ]
    if args.bvid:
        BV_IDS = [args.bvid]
    if args.limit:
        BV_IDS = BV_IDS[: args.limit]

    with TranscriptDB() as db:
        records = []
        for bv in BV_IDS:
            rec = db.get_by_bvid(bv)
            if rec:
                records.append(rec)
            else:
                log(f"⚠️ DB 中无 {bv}")

    if not records:
        print("❌ 没有可处理的视频")
        return 1

    os.makedirs(args.out, exist_ok=True)
    print(f"📚 待处理 {len(records)} 个视频 → 输出: {args.out}")
    log(f"🔄 规则初筛 + 并行 LLM 分析（{args.workers} workers, 模型: {API_MODEL}）...")

    results = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(build_one, r["bvid"], r["title"], r["author"],
                      r.get("upload_date", ""), r.get("duration", ""), r.get("transcript_text", "")): r["bvid"]
            for r in records
        }
        for fut in as_completed(futures):
            bv = futures[fut]
            try:
                index = fut.result()
                path = os.path.join(args.out, f"{bv}-知识索引.md")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(index + "\n")
                results[bv] = index
                log(f"  ✅ {bv} 索引已生成（{len(index)} 字）")
            except Exception as e:
                log(f"  ❌ {bv} 失败: {e}")

    elapsed = time.time() - t0
    ok = len(results)
    print(f"\n{'='*50}")
    print(f"📊 完成: 成功 {ok}/{len(records)}, 耗时 {elapsed:.0f}s")

    # 总览导航
    overview_path = os.path.join(args.out, "知识索引总览.md")
    with open(overview_path, "w", encoding="utf-8") as f:
        f.write("# 📚 B站转录视频 · 知识索引总览\n\n")
        f.write(f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M')} ｜ 成功 {ok}/{len(records)} 个视频\n\n")
        for r in records:
            bv = r["bvid"]
            status = "✅" if bv in results else "❌"
            f.write(f"- {status} [{r['title'][:40]}]({bv}-知识索引.md) ｜ {r['author']} ｜ {bv}\n")
    print(f"📄 总览: {overview_path}")
    return 0 if ok == len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
