# -*- coding: utf-8 -*-
"""
verify_anchors.py — 锚点验证脚本（防编造锚点句）
蒸馏完成的风格卡在入库前，对每句锚点原句做原文验证：
按卡 frontmatter 的书名定位原文目录，逐文件做包含检查（去空白后子串匹配）。
逐字命中=通过；子串部分命中=部分；完全查无=编造风险（删除该锚点或卡返工）。
验证结果写入卡 frontmatter 的 `锚点验证` 字段；未通过验证的卡不得入库。

用法:
  python verify_anchors.py <卡文件> <索引库原文目录>
  python verify_anchors.py --all <卡库目录> <索引库原文目录>   # 批量验证
"""
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def load_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def get_book_name(card_text):
    """从 frontmatter 提取书名，匹配原文目录（去掉书名号）"""
    m = re.search(r"^书名:\s*(.+)$", card_text, re.M)
    if not m:
        return None
    return m.group(1).strip().strip("《》")

def extract_anchors(card_text):
    """提取卡正文中锚点区的引号句。
    兼容 4 种锚点区标题：`**锚点原句**：` / `**锚点句**：` / `**锚点**：` / `- 锚点：`；
    每行可含多句（「雷，灭！」「我问你，说够了么！」或引号+｜分隔）。"""
    anchors = []
    in_anchor = False
    for line in card_text.splitlines():
        line = line.strip()
        # 锚点区入口：行含"锚点"且以 - 或 ** 开头（排除"> 说明"行）
        if ("锚点" in line) and (line.startswith("-") or line.startswith("**")):
            in_anchor = True
            # 标题行可能内联锚点句（如 `- **锚点**：「雷，灭！」`）
            anchors.extend(re.findall(r'[""“”「」](.+?)[""“”「」]', line))
            continue
        if in_anchor:
            if line.startswith("-") or re.match(r"^\d+\.", line):
                # 引号句（兼容「」、“”、""），可能一行多句
                anchors.extend(re.findall(r'[""“”「」](.+?)[""“”「」]', line))
            elif line.startswith("###") or line.startswith("##"):
                in_anchor = False
    return anchors

SCENE_CARD_TAG = "卡类型: 场景专卡"


def is_scene_card(card_text):
    """场景专卡判定（frontmatter 标记）"""
    return SCENE_CARD_TAG in card_text


def _source_of(line):
    """锚点行尾出处（《书》第X章）→ 书名；无出处返回 None"""
    m = re.search(r"[（(]《([^》]+)》", line)
    return m.group(1) if m else None


def _sample_books(card_text):
    """场景专卡取样书单（frontmatter 取样书单区的《书名》列表）"""
    m = re.search(r"取样书单:\s*\n((?:  - [^\n]*\n?)*)", card_text)
    return re.findall(r"《([^》]+)》", m.group(1)) if m else []


def _has_ref(line):
    """锚点行是否带出处标注（（第X章）/（《书》第X章）/（序X·第Y章））——区分锚点句与规则引号"""
    return bool(re.search(
        r"[（(]\s*(?:《[^》]+》)?\s*(?:序[\d一二三四五六七八九十百千]+[·.、]\s*)?第?[\d一二三四五六七八九十百千]+[章幕部卷]",
        line))


def extract_anchors_with_sources(card_text):
    """场景专卡锚点提取：返回 [(锚点, 出处书|None)]。
    只提取「锚点」区内的引号行（区分锚点句与规则正文引号）；
    出处优先取行内（《书》第X章），否则取所在分书样本小节标题的书（### 《书》）。"""
    out = []
    cur_book = None
    in_anchor = False
    for line in card_text.splitlines():
        line = line.strip()
        m = re.match(r"^#{2,3}\s*《([^》]+)》", line)
        if m:
            cur_book = m.group(1)
        if line.startswith("###") or line.startswith("##"):
            in_anchor = False
        if ("锚点" in line) and (line.startswith("-") or line.startswith("**")):
            in_anchor = True
            continue
        if in_anchor and _has_ref(line):
            for a in re.findall(r'[""“”「」](.+?)[""“”「」]', line):
                out.append((a, _source_of(line) or cur_book))
    return out


def find_book_dir(orig_root, book_name):
    """按书名找原文子目录（提取核心词后包含匹配，目录名排序保证确定性）"""
    if not os.path.isdir(orig_root):
        return None
    # 提取核心词：去掉《》、去掉括号后缀（如"（仙界篇语料）"）
    core = re.sub(r"[《》（）()]", "", book_name or "")
    core = re.sub(r"[（(].*?[)）]", "", core).strip()
    dirs = sorted(os.listdir(orig_root))
    for d in dirs:
        if core and core in d:
            return os.path.join(orig_root, d)
    # 兜底：书名前 2 字匹配（如"凡人修仙传"→匹配"凡人修仙传-忘语"或"凡人修仙传仙界篇"）
    if len(core) >= 2:
        for d in dirs:
            if d.startswith(core[:2]):
                return os.path.join(orig_root, d)
    return None

LOOSE_PUNCT = r"\s，。！？；：、（）【】《》\"'“”‘’「」…——·~"

def _loose(s):
    return re.sub(LOOSE_PUNCT, "", s)

def verify_in_dir(anchor, book_dir):
    """在目录的全部文件中检查锚点：逐字命中 / 宽松命中(去标点) / 部分命中 / 查无"""
    a = re.sub(r"\s+", "", anchor)
    if not a:
        return "查无(空)"
    hits = 0
    files = 0
    for root, _, files_ in os.walk(book_dir):
        for fn in files_:
            if not (fn.endswith(".md") or fn.endswith(".txt")):
                continue
            files += 1
            try:
                text = re.sub(r"\s+", "", load_text(os.path.join(root, fn)))
            except Exception:
                continue
            if a in text:
                hits += 1
    if hits > 0:
        return "通过"
    # 宽松档：去空白+去标点后全句匹配（容忍跨行/引号/标点字形差异，2026-08-17 增）
    a_loose = _loose(a)
    if len(a_loose) >= 6:
        for root, _, files_ in os.walk(book_dir):
            for fn in files_:
                if not (fn.endswith(".md") or fn.endswith(".txt")):
                    continue
                try:
                    text_loose = _loose(re.sub(r"\s+", "", load_text(os.path.join(root, fn))))
                except Exception:
                    continue
                if a_loose in text_loose:
                    return "通过(宽松)"
    # 部分匹配：锚点拆成 ≤8 字子串，任一子串出现 = 部分
    subs = [a[i:i+8] for i in range(0, max(1, len(a)-8), 8)]
    sub_hits = 0
    for root, _, files_ in os.walk(book_dir):
        for fn in files_:
            if not (fn.endswith(".md") or fn.endswith(".txt")):
                continue
            try:
                text = re.sub(r"\s+", "", load_text(os.path.join(root, fn)))
            except Exception:
                continue
            for s in subs:
                if s in text:
                    sub_hits += 1
            if sub_hits >= max(1, len(subs) * 2 // 3):
                break
    if sub_hits >= max(1, len(subs) * 2 // 3):
        return "部分命中"
    return "查无"

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "--all":
        card_dir, orig_root = sys.argv[2], sys.argv[3]
        total = valid = partial = missing = 0
        for fn in sorted(os.listdir(card_dir)):
            if not (fn.endswith("-风格卡.md") or fn.endswith("-场景卡.md")):
                continue
            path = os.path.join(card_dir, fn)
            text = load_text(path)
            if is_scene_card(text):
                # 场景专卡：锚点带出处，按出处书定位；无出处锚点扫取样书单全部书（任一命中=通过）
                book = None
                anchors_src = extract_anchors_with_sources(text)
                sample = _sample_books(text)
                results = []
                for a, src in anchors_src:
                    if src:
                        bdir = find_book_dir(orig_root, src)
                        results.append(verify_in_dir(a, bdir) if bdir else "查无(未找到原文目录)")
                        continue
                    # 无出处：扫取样书单
                    verdict = "查无"
                    for b in sample:
                        bdir = find_book_dir(orig_root, b)
                        if bdir and verify_in_dir(a, bdir) == "通过":
                            verdict = "通过"
                            break
                    results.append(verdict)
                anchors = [a for a, _ in anchors_src]
            else:
                book = get_book_name(text)
                book_dir = find_book_dir(orig_root, book)
                anchors = extract_anchors(text)
                if not anchors:
                    print(f"  {fn}: 无锚点（弱卡）")
                    continue
                results = [verify_in_dir(a, book_dir) for a in anchors] if book_dir else ["查无(未找到原文目录)"] * len(anchors)
            n_ok = sum(1 for r in results if r == "通过")
            n_par = sum(1 for r in results if r == "部分命中")
            n_miss = sum(1 for r in results if r == "查无" or r.startswith("查无"))
            status = "✅ 通过" if n_miss == 0 else ("⚠️ 部分通过" if n_miss <= max(1, len(results)//3) else "❌ 失败")
            total += 1; valid += n_ok; partial += n_par; missing += n_miss
            print(f"  {fn} [{book}] {status}（通过 {n_ok} / 部分 {n_par} / 查无 {n_miss} / 共 {len(results)}）")
        print(f"\n汇总: {total} 张卡 | 锚点 通过 {valid} / 部分 {partial} / 查无 {missing}")
        print("查无 = 编造风险：删除该锚点或返工；部分 = 建议人工复核")
    else:
        card_path, orig_root = sys.argv[1], sys.argv[2]
        text = load_text(card_path)
        if is_scene_card(text):
            anchors_src = extract_anchors_with_sources(text)
            print(f"场景专卡共 {len(anchors_src)} 句锚点:")
            for a, src in anchors_src:
                bdir = find_book_dir(orig_root, src) if src else None
                r = verify_in_dir(a, bdir) if bdir else "查无(未找到原文目录)"
                print(f"  [{r}] {a[:28]}（{src or '无出处'}）")
        else:
            book = get_book_name(text)
            book_dir = find_book_dir(orig_root, book)
            anchors = extract_anchors(text)
            print(f"《{book}》共 {len(anchors)} 句锚点:")
            for a in anchors:
                r = verify_in_dir(a, book_dir) if book_dir else "查无(未找到原文目录)"
                print(f"  [{r}] {a[:28]}")

if __name__ == "__main__":
    main()
