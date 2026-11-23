#!/usr/bin/env python3
"""
日系小说本地化 — 段落分类脚本 v1.0

对批文件（或整书）的段落做【推进】/【氛围】二分类标注，改写前预处理。
分类规则（见 references/混合文体规则.md）：
- 【推进】: 改变情节状态（有对白/动作/事件结果）
- 【氛围】: 不改变情节状态（无对白密度 + 感官词密度 + 句均>25字 + 时间锚点）

输出：段前加 `<!-- 分类:推进 -->` 或 `<!-- 分类:氛围 -->` 注释标记，
写回同目录 `<原名>.classified.md`（不改原文件）。

用法:
  python classify_paragraphs.py "<批文件路径>" [--out <输出路径>] [--min-chars 25]

依赖: 无（纯标准库）
"""
import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 感官词（光线/气味/温度/声音/触感）
SENSE_WORDS = re.compile(
    r"光|影|明|暗|风|雨|雪|雾|香|臭|味|温|凉|热|寒|声|音|响|静|湿|干|色|空|気|匂|静けさ|夕暮れ|夜明け",
)
# 对白标记（引号/对话动词）
DIALOGUE_MARKERS = re.compile(r"「|」|『|』|“|”|言った|言う|叫んだ|答えた|说|说道|喊|答|話した")
# 动作/事件标记（只保留强动作，避免"風/来/行"等弱词误伤氛围段）
ACTION_MARKERS = re.compile(
    r"走|跑|跳|推|拉|抓|打|拿|放|站起|坐|蹲|转身|抬头|低头|握紧|攥|冲|扑|闪|躲|"
    r"殺|死|戦|打ち|切った|掴|叩|蹴|斬|击|挥|撞|摔|扑倒",
)
# 情节状态词（事件结果/转折）
EVENT_MARKERS = re.compile(
    r"けど|しかし|だが|そして|それから|だから|結果|結局|ついに|やっと|突然|"
    r"但是|然而|突然|终于|结果|没想到|就在这时",
)


def classify_paragraph(para: str) -> str:
    """返回 '推进' 或 '氛围'"""
    para = para.strip()
    if not para:
        return "推进"  # 空段不算氛围

    n_chars = len(para)
    n_sentences = len(re.findall(r"[。！？!?．]", para)) or 1
    avg_len = n_chars / n_sentences

    # 特征1: 对白/动作/事件密度（推进信号）
    has_dialogue = bool(DIALOGUE_MARKERS.search(para))
    has_action = bool(ACTION_MARKERS.search(para))
    has_event = bool(EVENT_MARKERS.search(para))

    # 特征2: 感官词密度（氛围信号）
    sense_count = len(SENSE_WORDS.findall(para))
    sense_density = sense_count / max(1, n_chars / 100)  # 每百字感官词数

    # 特征3: 无对白密度（连续3+句无对话无动作）
    # 简化：整段无对话且无动作视为长无对白——由下方 not has_dialogue 分支直接承担

    # 判定（与混合文体规则一致：事件推进度是唯一裁判）
    # 日文逗号（、）是子句分隔，主句按 。！？ 计；avg_len 阈值相应降低
    # 先看强氛围信号：无对白 + 高感官密度 → 氛围（即使偶有动作词）
    if not has_dialogue and sense_density >= 1.5 and avg_len >= 15:
        return "氛围"
    if not has_dialogue and avg_len >= 25:
        return "氛围"
    # 再推进信号
    if has_dialogue or has_action or has_event:
        return "推进"
    return "推进"


def process_file(path: str, out_path: str):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        text = f.read()

    # 分离 frontmatter
    frontmatter = ""
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            frontmatter = text[: end + 4] + "\n"
            body = text[end + 4:]

    paragraphs = re.split(r"(\n\s*\n)", body)
    stats = {"推进": 0, "氛围": 0}

    out_parts = [frontmatter]
    if len(paragraphs) == 1 and "\n" in body:
        # 管道联动兜底：拆批产物段落间无空行（原文如此）→ 空行分段得 1 大段，
        # 整批会被判"1 段氛围 100%"（失真）。自动降级为按非空行分段。
        for line in (l for l in body.splitlines() if l.strip()):
            cls = classify_paragraph(line)
            stats[cls] += 1
            out_parts.append(f"<!-- 分类:{cls} -->\n{line}\n")
    else:
        for i in range(0, len(paragraphs), 2):
            para = paragraphs[i]
            sep = paragraphs[i + 1] if i + 1 < len(paragraphs) else ""
            if para.strip():
                cls = classify_paragraph(para)
                stats[cls] += 1
                out_parts.append(f"<!-- 分类:{cls} -->\n{para}{sep}")
            else:
                out_parts.append(para + sep)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(out_parts))

    total = stats["推进"] + stats["氛围"]
    ratio = stats["氛围"] / total * 100 if total else 0
    print(f"{os.path.basename(path)}: 共 {total} 段 | 推进 {stats['推进']} | 氛围 {stats['氛围']} | 氛围占比 {ratio:.1f}%")
    if ratio > 20:
        print(f"  ⚠️ 氛围占比 {ratio:.1f}% 超过 20% 红线，需检查（规则见混合文体规则.md）")


def main():
    ap = argparse.ArgumentParser(description="段落分类预处理")
    ap.add_argument("files", nargs="+", help="批文件路径（可多个，并行处理）")
    ap.add_argument("--out", help="输出目录（默认与源文件同目录，文件名加 .classified）")
    ap.add_argument("--workers", type=int, default=8, help="并行线程数（默认 8）")
    args = ap.parse_args()

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def worker(fpath):
        if not os.path.exists(fpath):
            return f"错误: 文件不存在 {fpath}"
        if args.out:
            os.makedirs(args.out, exist_ok=True)
            out = os.path.join(args.out, os.path.basename(fpath).replace(".md", ".classified.md"))
        else:
            out = fpath.replace(".md", ".classified.md")
        return process_file(fpath, out)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(worker, f) for f in args.files]
        for fut in as_completed(futures):
            res = fut.result()
            if res:  # worker 返回 None 时不打印多余行
                print(res)


if __name__ == "__main__":
    main()
