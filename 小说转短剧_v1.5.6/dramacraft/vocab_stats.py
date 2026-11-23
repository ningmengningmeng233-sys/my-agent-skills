# -*- coding: utf-8 -*-
"""词表体检统计器（v1.2.0 投产保障）。

从 state/vocab_hits.jsonl 汇总分析：
1. 高频命中 Top10（按频次倒序 + 最近样例）——提供「该不该加进禁词表/正则」的真实生产证据；
2. 零命中清单（词表全量减去账本命中集的差集）——提供「该不该精简词表」的候选名单。

铁律：只读统计并输出白话报告，绝不自动篡改或覆盖任何词表文件，长肉与裁员永远由人拍板。
"""
import argparse
import collections
import json
import os
from typing import Any, Dict, List, Set, Tuple

PATCH_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills_runtime", "dramacraft_patch"))
BANNED_WORDS_PATH = os.path.join(PATCH_DIR, "banned_words.json")
AI_TONE_PATTERNS_PATH = os.path.join(PATCH_DIR, "ai_tone_patterns.json")


def load_banned_words() -> List[str]:
    if os.path.exists(BANNED_WORDS_PATH):
        try:
            with open(BANNED_WORDS_PATH, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            pass
    return []


def load_ai_tone_patterns() -> List[str]:
    if os.path.exists(AI_TONE_PATTERNS_PATH):
        try:
            with open(AI_TONE_PATTERNS_PATH, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                return [item.get("name", "") for item in data if isinstance(item, dict) and "name" in item]
        except Exception:
            pass
    return []


def load_hits(path_or_dir: str) -> List[Dict[str, Any]]:
    """读取命中记录，可传入工作区目录或直接传入 vocab_hits.jsonl 路径"""
    target = path_or_dir
    if os.path.isdir(target):
        # 优先查找 state/vocab_hits.jsonl，其次根目录 vocab_hits.jsonl
        cand1 = os.path.join(target, "state", "vocab_hits.jsonl")
        cand2 = os.path.join(target, "vocab_hits.jsonl")
        target = cand1 if os.path.exists(cand1) else cand2

    hits = []
    if not os.path.exists(target):
        return hits

    try:
        with open(target, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    hits.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return hits


def generate_vocab_report(workspace_dir: str) -> str:
    """生成白话词表分析报告"""
    hits = load_hits(workspace_dir)
    all_banned_words = load_banned_words()
    all_ai_patterns = load_ai_tone_patterns()

    # 统计频次
    # key: (category, name) -> count, last_sample, last_ep
    counter = collections.Counter()
    last_samples: Dict[Tuple[str, str], Dict[str, Any]] = {}
    hit_words: Set[str] = set()
    hit_patterns: Set[str] = set()

    for h in hits:
        gate = h.get("gate", "")
        if gate == "literature-sickness":
            term = h.get("term", "")
            if term:
                counter[("文学病禁词", term)] += 1
                hit_words.add(term)
                last_samples[("文学病禁词", term)] = h
        elif gate == "ai-tone-removal":
            pat = h.get("pattern", "")
            if pat:
                counter[("AI腔翻案模式", pat)] += 1
                hit_patterns.add(pat)
                last_samples[("AI腔翻案模式", pat)] = h

    # 1. 组装 Top 10
    top_rows = []
    for rank, ((cat, name), count) in enumerate(counter.most_common(10), 1):
        sample_info = last_samples.get((cat, name), {})
        ep = sample_info.get("ep", "-")
        snippet = sample_info.get("snippet", "-")
        top_rows.append(f"| {rank} | {cat} | {name} | {count} 次 | 第 {ep} 集: 「{snippet}」 |")

    # 2. 零命中差集
    zero_words = [w for w in all_banned_words if w not in hit_words]
    zero_patterns = [p for p in all_ai_patterns if p not in hit_patterns]

    lines = [
        "# 词表命中分析报告",
        "",
        "本报告基于生产过程中的实际质量门拦截日志自动提炼，只提供决策证据，不自动改动词表。",
        "",
        "## 一、高频命中 Top 10（模型最常触犯的典型顽疾）",
        "",
        "| 排名 | 类型 | 命中词 / 规则模式 | 拦截频次 | 最近违规样例 |",
        "|:---:|:---:|:---|:---:|:---|",
    ]
    if top_rows:
        lines.extend(top_rows)
    else:
        lines.append("| - | - | （暂无任何命中拦截记录） | 0 | - |")

    lines.extend([
        "",
        "## 二、零命中清单（当前词表中从未被拦截的规则，供精简参考）",
        "",
        f"### 1. 未命中文学病词（共 {len(zero_words)} / {len(all_banned_words)} 个条目）",
    ])
    if zero_words:
        # 分行显示，每行若干个
        lines.append("`" + "`, `".join(zero_words[:50]) + ("` 等" if len(zero_words) > 50 else "`"))
    else:
        lines.append("（全部词条均有命中）")

    lines.extend([
        "",
        f"### 2. 未命中 AI 腔模式（共 {len(zero_patterns)} / {len(all_ai_patterns)} 个模式）",
    ])
    if zero_patterns:
        lines.append("`" + "`, `".join(zero_patterns) + "`")
    else:
        lines.append("（全部模式均有命中）")

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="DramaCraft 词表体检与命中统计器")
    parser.add_argument("workspace", nargs="?", default="workspace", help="工作区目录或 vocab_hits.jsonl 路径")
    args = parser.parse_args()

    report = generate_vocab_report(args.workspace)
    print(report)


if __name__ == "__main__":
    main()
