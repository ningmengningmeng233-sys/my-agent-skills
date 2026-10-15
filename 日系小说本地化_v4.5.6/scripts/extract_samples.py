#!/usr/bin/env python3
"""
日系小说本地化 — 样本抽取脚本 v1.0

从爽文索引库（INDEX_LIB_DIR 或 skill 内 索引库/）抽取风格样本，
写入 references/样本库/（13本书×8场景结构，新书自动纳入）。

用法:
  python extract_samples.py                # 抽取全部书（每书5文件×每场景2段）
  python extract_samples.py --full         # 全量抽取（每书20文件×每场景5段，覆盖更全面）
  python extract_samples.py --book 书名     # 只抽某本书
  python extract_samples.py --list         # 列出索引库现有书目

场景类型（8类）:
  打脸碾压/升级突破/战斗对决/围观震撼/金句霸气/复仇归来/心境意境/日常过渡
"""
import argparse
import os
import random
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
random.seed(7)

SCENES = {
    "打脸碾压": r"冷哼|不屑|轻蔑|蝼蚁|也配|不自量力|冷声道|嗤笑",
    "升级突破": r"突破|晋级|破境|顿悟|境界|修为|蜕变|觉醒",
    "战斗对决": r"一掌|一拳|剑光|轰然|爆射|撞击|刀芒|崩溃|撕裂",
    "围观震撼": r"满场|死寂|倒吸|失声|哗然|瞠目|鸦雀无声|全场寂静",
    "金句霸气": r"蝼蚁|傲然|冷冷道|天不容|轮回|逆天|也配",
    "复仇归来": r"复仇|血债|归来|清算|跪下|灭门|讨回",
    "心境意境": r"轻叹|默默|夜色|琴音|灯火|一壶|凭栏|雾蒙蒙|凭吊",
    "日常过渡": r"清晨|午后|转身|回到|收拾|沏茶|泡茶",
}

# 垃圾内容过滤（盗版站广告/更新备注/审查掩码/爬取残留）
JUNK_RE = re.compile(
    r"http|百度|小说网|笔趣阁|最新章节|更新时间|字数[:：]|www\.|转载|⑹|⑴|"
    r"第[一二三四五六七八九十百0-9]+更|今日第|叶子换了|站内"
)


def is_junk(p: str) -> bool:
    """广告/更新备注/审查掩码段落 → True（跳过）"""
    if JUNK_RE.search(p):
        return True
    if "***" in p or "**" in p or "［>］" in p:
        return True
    return False


def trim_to_sentence(text: str, max_len: int = 400) -> str:
    """截断到句子边界（最后一个句末标点处），找不到就硬截"""
    if len(text) <= max_len:
        return text
    head = text[:max_len]
    idx = max(head.rfind("。"), head.rfind("！"), head.rfind("？"), head.rfind("…"))
    if idx > max_len * 0.6:
        return head[:idx + 1]
    return head

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 索引库路径：优先 .env 的 INDEX_LIB_DIR，其次 skill 内 索引库/
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(SKILL_DIR, ".env"))
except Exception:
    pass
INDEX_LIB_DIR = os.environ.get("INDEX_LIB_DIR", "").strip() or os.path.join(SKILL_DIR, "索引库")
OUT_DIR = os.path.join(SKILL_DIR, "references", "样本库")


def extract_book_samples(book: str, max_files: int = 5, per_scene: int = 2) -> dict:
    """从一本书抽取样本，返回 {场景: [段落,...]}"""
    d = os.path.join(INDEX_LIB_DIR, "小说原文", book)
    if not os.path.isdir(d):
        return {}
    files = sorted(f for f in os.listdir(d) if f.endswith(".md"))  # 先过滤 .md 再抽样
    picked = random.sample(files, min(max_files, len(files)))
    found = {k: [] for k in SCENES}
    for fname in picked:
        fpath = os.path.join(d, fname)
        try:
            with open(fpath, encoding="utf-8") as fh:
                text = fh.read()
        except Exception:
            continue
        paras = re.split(r"\n[\s　]*\n", text)
        for p in paras:
            p = p.strip().strip("　")
            if len(p) < 60:
                continue
            if is_junk(p):  # 广告/更新备注/审查掩码 → 跳过
                continue
            for scene, kw in SCENES.items():
                if re.search(kw, p):
                    if len(found[scene]) < per_scene:
                        found[scene].append(trim_to_sentence(p, 400))
                    break  # 一段只归入首个命中场景（防同一段重复进多个场景）
    return found


def write_book_file(book: str, samples: dict) -> int:
    """写入 样本库/<书>.md，返回段数"""
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"{book}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# {book} 风格样本\n\n")
        for scene, paras in samples.items():
            if paras:
                f.write(f"## {scene}\n\n")
                for s in paras:
                    f.write(f"> {s[:400]}\n\n")
    return sum(len(v) for v in samples.values())


def list_books() -> list:
    """列出索引库现有书目"""
    d = os.path.join(INDEX_LIB_DIR, "小说原文")
    if not os.path.isdir(d):
        return []
    return sorted(x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)))


def main():
    ap = argparse.ArgumentParser(description="样本抽取")
    ap.add_argument("--book", help="只抽某本书")
    ap.add_argument("--list", action="store_true", help="列出索引库书目")
    ap.add_argument("--max-files", type=int, default=5, help="每书抽几个文件")
    ap.add_argument("--full", action="store_true",
                    help="全量抽取：每书20文件×每场景5段（默认5文件×2段）")
    args = ap.parse_args()

    if args.list:
        books = list_books()
        print(f"索引库现有 {len(books)} 本书:")
        for b in books:
            print(f"  - {b}")
        sys.exit(0)

    books = [args.book] if args.book else list_books()
    if not books:
        print(f"索引库为空: {INDEX_LIB_DIR}")
        print("请将你的爽文索引库放入 skill 的 索引库/ 目录，或在 .env 设置 INDEX_LIB_DIR")
        sys.exit(1)

    total = 0
    max_files = 20 if args.full else args.max_files
    per_scene = 5 if args.full else 2
    for book in books:
        samples = extract_book_samples(book, max_files=max_files, per_scene=per_scene)
        n = sum(len(v) for v in samples.values())
        if n == 0:
            print(f"⚠️ {book}: 抽到 0 段（目录为空/段落过短(<60字)/无场景词命中）——"
                  f"样本库将不含此书，风格学习会缺这一本")
            continue
        n = write_book_file(book, samples)
        total += n
        print(f"✅ {book}: {n} 段")

    print(f"\n总样本: {total} 段 → {OUT_DIR}")


if __name__ == "__main__":
    main()
