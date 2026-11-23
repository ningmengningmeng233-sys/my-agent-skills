# -*- coding: utf-8 -*-
"""转录索引 · 检索（双路召回 → 主题×作者清单）

ADR-0001：本地向量召回（≈0 元/次）+ 关键词规范化兜底，AI 只精读命中条目。
拷问 Q11：输出按主题聚类、按作者分组的清单，供用户选「看某一家/对比全部」。

用法: python 检索.py "<问题>" [--top 15] [--author 作者] [--json]
输出: JSON（供 AI 精读）或 Markdown 清单（直接看）
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EMBED_MODEL, EMBED_MODEL_DIR, VEC_DIR, VEC_META

TOP_K = 15
MIN_SCORE = 0.55   # 向量相似度阈值：实测相关 0.68-0.84 / 无关 0.42-0.46，0.55 安全分割
                   # 低于此且非关键词命中 → 过滤（防无关 query 误报命中）


def load_vec():
    import numpy as np
    emb_path = VEC_DIR / "embeddings.npy"
    entries_path = VEC_DIR / "entries.json"
    if not emb_path.exists() or not entries_path.exists() or not VEC_META.exists():
        raise RuntimeError(f"向量库不完整：{VEC_DIR}；请先运行 build.sh --force")
    try:
        vecs = np.load(emb_path)
        entries = json.loads(entries_path.read_text(encoding="utf-8"))
        metadata = json.loads(VEC_META.read_text(encoding="utf-8"))
        if metadata.get("embedding_model") != EMBED_MODEL:
            raise RuntimeError("向量模型版本与当前配置不一致，请运行 build.sh --force")
    except Exception as e:
        raise RuntimeError(f"向量库读取失败：{e}；请运行 build.sh --force 重建") from e
    if len(vecs) != len(entries):
        raise RuntimeError(f"向量与条目数量不一致：{len(vecs)} != {len(entries)}；请运行 build.sh --force")
    return vecs, entries


def get_model():
    import os
    os.environ.pop("NO_PROXY", None)
    os.environ.pop("no_proxy", None)
    from sentence_transformers import SentenceTransformer
    model_dir = EMBED_MODEL_DIR or (Path(__file__).resolve().parent.parent / "models" / "bge-small-zh-v1.5")
    return SentenceTransformer(str(model_dir))


def keyword_hits(query, entry):
    """关键词规范化匹配（兜底路）：query 拆 2-4 字片段，命中条目文本计数。"""
    text = f"{entry.get('name','')} {entry.get('summary','')} {entry.get('excerpt','')}"
    q = re.sub(r"\s+", "", query)
    if len(q) <= 1:
        return 0
    hits = 0
    # 整句 + 3/4 字滑动窗口（2026-08-16 优化 B：去掉 2 字窗口，减「怎么/如何」类噪声命中）
    # 两字短问句（如“怎么/如何”）只会制造噪声，不走整句命中；至少 3 字才允许整句兜底。
    cands = {q} if 3 <= len(q) <= 30 else set()
    for n in (4, 3):
        if len(q) < n:
            continue
        for i in range(0, len(q) - n + 1, n):
            cands.add(q[i:i + n])
    for c in cands:
        if c and c in text:
            hits += 1
    return hits


def cluster(items):
    """按主题标签聚类，组内按作者+日期。返回 clusters 列表。"""
    groups = {}
    for it in items:
        groups.setdefault(it.get("tag") or "未分类", []).append(it)
    out = []
    for tag, its in groups.items():
        its.sort(key=lambda x: x.get("date") or "")
        out.append({"tag": tag, "items": its})
    # 组间按组内平均分排序（2026-08-16 优化 A：原按最高分，大标签一个高分即压死小标签）
    out.sort(key=lambda g: -(sum(i.get("score", 0) for i in g["items"]) / max(1, len(g["items"]))))
    return out


def main():
    args = [a for a in sys.argv[1:]]
    query = next((a for a in args if not a.startswith("--")), "")
    top = TOP_K
    author = None
    as_json = "--json" in args
    for i, a in enumerate(args):
        if a == "--top" and i + 1 < len(args):
            try:
                top = max(1, int(args[i + 1]))
            except ValueError:
                print(f"⚠ --top 值 '{args[i + 1]}' 非法，使用默认 {top}")
        if a == "--author" and i + 1 < len(args):
            author = args[i + 1]
    if not query:
        print('用法: python 检索.py "<问题>" [--top 15] [--author 作者] [--json]')
        return

    vecs, entries = load_vec()
    model = get_model()
    qv = model.encode([query], normalize_embeddings=True)[0]

    import numpy as np
    sims = (vecs @ qv).tolist()

    # 双路召回：向量 top-K + 关键词命中 ≥2
    order = sorted(range(len(entries)), key=lambda i: -sims[i])
    if author:
        # 2026-08-16 修复 P1：作者过滤必须在 top-N 截断前，否则目标作者条目被截掉 → 误报未命中
        order = [i for i in order if author in (entries[i].get("meta", {}).get("作者") or "")]
    vec_sel = order[:top]
    kw_sel = [i for i in range(len(entries)) if keyword_hits(query, entries[i]) >= 2]
    if author:
        kw_sel = [i for i in kw_sel if author in (entries[i].get("meta", {}).get("作者") or "")]
    sel = sorted(set(vec_sel + kw_sel), key=lambda i: -sims[i])[:top]

    items = []
    filtered_low = 0
    for i in sel:
        e = dict(entries[i])
        e["score"] = round(sims[i], 3)
        e["kw"] = i in kw_sel
        meta = e.pop("meta", {})
        e["author"] = meta.get("作者", "")
        e["date"] = meta.get("发布时间", "")
        e["bv"] = meta.get("BV号", "")
        # 2026-08-16 修复 P2：低分且非关键词命中的条目过滤（阈值 MIN_SCORE），关键词命中保留（字面匹配可信）
        if not e["kw"] and sims[i] < MIN_SCORE:
            filtered_low += 1
            continue
        e.setdefault("source_rel", e.get("file", ""))
        artifact = e.get("artifact_key", "")
        e.setdefault("index_rel", f"索引库/{artifact}-idx.md" if artifact else "")
        e.setdefault("proof_rel", f"校对版/{artifact}_校对版.txt" if artifact else "")
        items.append(e)

    # 组内/组间排序后再编号，编号在一次检索响应中稳定，便于用户选择“看 #2”。
    clusters = cluster(items)
    candidate_no = 1
    for group in clusters:
        for item in group["items"]:
            item["candidate_no"] = candidate_no
            candidate_no += 1
    note = f"（{filtered_low} 条低分条目已过滤，阈值 {MIN_SCORE}）" if filtered_low else ""
    if as_json:
        print(json.dumps({"query": query, "clusters": clusters, "note": note}, ensure_ascii=False, indent=1))
        return

    # Markdown 清单（主题×作者）
    print(f"# 检索：「{query}」\n")
    if filtered_low:
        print(f"> ⚠ {note}\n")
    if not clusters:
        print("（未命中。可换同义词/拆短问题重试，或看相近主题列表）")
        return
    for g in clusters:
        authors = sorted({it.get("author") or "未知" for it in g["items"]})
        print(f"## 主题：{g['tag']}（{len(g['items'])} 条，来自 {len(authors)} 个视频）")
        print(f"> 作者：{'、'.join(authors)}\n")
        for it in g["items"]:
            tag_v = "🔍向量" if not it.get("kw") else "⌨️关键词"
            print(f"- **#{it.get('candidate_no')} {it.get('name')}**（{it.get('author') or '?'} {it.get('date') or ''}，{it.get('bv') or '无BV'}）{tag_v} {it.get('score', 0):.2f}")
            print(f"  {it.get('summary', '')}")
            print(f"  文件：{it.get('file')} ｜ 行号：{it.get('lines', '')}（±3 行容差）")
            # 校对版目录扁平（不含子目录），取 basename 推导
            if it.get('artifact_key'):
                proof = str(it['artifact_key']) + '_校对版.txt'
            else:
                proof = os.path.basename(str(it.get('file', ''))).rsplit('.', 1)[0] + '_校对版.txt' if it.get('file') else ''
            if proof:
                print(f"  校对版：{proof}")
        print()


if __name__ == "__main__":
    main()
