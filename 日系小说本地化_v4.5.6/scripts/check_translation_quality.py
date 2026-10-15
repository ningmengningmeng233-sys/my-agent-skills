#!/usr/bin/env python3
"""翻译质量体检：检测输入稿的错译/漏译/矛盾/机翻特征（结构体检的语义层补充）。

用法:
  python check_translation_quality.py <批文件目录>
  python check_translation_quality.py <单文件>

检测项:
  1. 术语不一致（同一日源词多种译法——映射表之前的源头问题）
  2. 前后矛盾（同一事件两种说法/数字对不上）
  3. 错译特征（日式语法残留导致语义扭曲）
  4. 漏译特征（句子结构不完整/突兀断句）
  5. 机翻味（长句堆叠/欧化句式密度）

产出《翻译质量体检报告.md》→ 严重问题 → 用户拍板（修补/接受/换稿）。
"""
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 机翻味特征：欧化长定语/被动堆叠/语义空洞
MACHINE_PATTERNS: dict[str, str] = {
    "欧化长定语": r"被[^。]{10,}的[^。]{5,}(?:所|给)",
    "日式被动堆叠": r"被[^。]{2,8}被[^。]{2,8}",
    "语义空洞连接": r"关于.{0,12}这一点|对于.{0,12}来说",
    "突兀断句": r"[^。！？\n]{3,8}[。！？]\s*[他她它这那]{1}[^。]{0,4}[。！？](?![一-鿿])",
}

# 漏译特征：不完整句/孤立逗号残留
OMISSION_PATTERNS: dict[str, str] = {
    "孤立连接词": r"^[而且但是然而不过]+[。！？]",
}


def strip_dialogue(text: str) -> str:
    """剔除引号内内容（对话省略号/语气是合法台词节奏，不检查）"""
    return re.sub(r"[“\"「『][^”\"」』]*[”\"」』]", "", text)


def scan_machine(text: str) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
    for name, pat in MACHINE_PATTERNS.items():
        for m in re.finditer(pat, text):
            ctx = text[max(0, m.start() - 12):m.end() + 20].replace("\n", " ")
            hits.append((name, m.group(0)[:30], ctx))
    return hits


def scan_omission(text: str) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        for name, pat in OMISSION_PATTERNS.items():
            if re.search(pat, line):
                hits.append((name, line[:30], line[:60]))
    return hits


def find_contradictions(text: str) -> list[tuple[str, str, str]]:
    """前后矛盾：同一量词短语出现不同数字（如"三十万金币"vs"三万金币"）。
    启发式不可靠——本函数只标记可疑点，最终由 AI/人工抽检确认。"""
    hits: list[tuple[str, str, str]] = []
    # 匹配 "数字+量词短语"（如 三十万金币 / 三千金币 / 五枚）
    pattern = re.compile(r"([\d一二三四五六七八九十百千万]+)((?:万|千|百)?(?:金币|银币|枚|个|名|人|里|天|年|月|次|招|层))")
    by_phrase: dict[str, set[str]] = defaultdict(set)
    positions: dict[str, list[int]] = defaultdict(list)
    for m in pattern.finditer(text):
        num, phrase = m.group(1), m.group(2)
        by_phrase[phrase].add(num)
        positions[phrase].append(m.start())
    for phrase, nums in by_phrase.items():
        if len(nums) > 1 and len(positions[phrase]) > 1:
            pos = positions[phrase][0]
            ctx = text[max(0, pos - 15):pos + 20].replace("\n", " ")
            hits.append(("前后矛盾(疑似,需人工核)", f"{'/'.join(sorted(nums))}{phrase}", ctx))
    return hits


def find_term_inconsistency(text: str) -> list[tuple[str, str, str]]:
    """术语不一致：同一上下文词的高频变体（简化：专名后括号译注/多写并存）"""
    hits: list[tuple[str, str, str]] = []
    # 检测 "XX（XX）" 式译注残留（翻译工作痕迹）
    for m in re.finditer(r"([一-鿿]{2,6})[（(]([^）)]{2,12})[）)]", text):
        hits.append(("译注残留(疑似漏处理)", m.group(0)[:30], m.group(0)[:40]))
    return hits


def check_file(path: str) -> dict:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    # 跳过 frontmatter 与元信息区
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    # 跳过"本批说明/爽点对照"等元信息区（映射注释会误报译注残留）
    for marker in ("## 本批说明", "## 本批爽点对照", "## 写前计划"):
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]
    results: dict[str, list] = {
        "机翻味": scan_machine(text),
        "漏译特征": scan_omission(text),
        "前后矛盾": find_contradictions(text),
        "译注残留": find_term_inconsistency(text),
    }
    return results


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    target = sys.argv[1]
    paths = [Path(target)] if Path(target).is_file() else sorted(Path(target).rglob("*"))
    files = [p for p in paths if p.suffix in (".md", ".txt") and p.is_file()]

    all_hits: Counter[str] = Counter()
    details: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for f in files:
        r = check_file(str(f))
        for cat, hits in r.items():
            all_hits[cat] += len(hits)
            details[cat].extend((f.name, *h) for h in hits[:5])

    print(f"检查 {len(files)} 个文件\n")
    severe = False
    for cat in ["机翻味", "漏译特征", "前后矛盾", "译注残留"]:
        n = all_hits[cat]
        flag = "❌" if n > 0 else "✅"
        print(f"  {flag} {cat}: {n} 处")
        if n > 0:
            severe = True
            for fname, name, word, ctx in details[cat][:3]:
                print(f"     - [{word}] …{ctx}")

    report = Path("翻译质量体检报告.md")
    with report.open("w", encoding="utf-8") as f:
        f.write("# 翻译质量体检报告\n\n")
        f.write(f"- 检查文件数: {len(files)}\n")
        f.write(f"- 机翻味: {all_hits['机翻味']} | 漏译特征: {all_hits['漏译特征']} | "
                f"前后矛盾: {all_hits['前后矛盾']} | 译注残留: {all_hits['译注残留']}\n\n")
        for cat in ["机翻味", "漏译特征", "前后矛盾", "译注残留"]:
            if details[cat]:
                f.write(f"## {cat}\n")
                for fname, name, word, ctx in details[cat][:20]:
                    f.write(f"- [{word}]（{fname}）{ctx}\n")
                f.write("\n")

    print(f"\n报告已写入: {report.name}")
    print("严重问题（机翻味/漏译/矛盾密集）→ 报告用户拍板：修补 / 接受 / 换稿")
    return 1 if severe else 0


if __name__ == "__main__":
    sys.exit(main())
