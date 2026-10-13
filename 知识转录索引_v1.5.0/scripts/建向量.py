# -*- coding: utf-8 -*-
"""转录索引 · 建向量库（本地 embedding）

ADR-0001：检索用本地向量召回（≈0 元/次）。本脚本读取全部索引 md（含文件级摘要），
每条目生成一条 embedding（bge-small-zh-v1.5，512 维），存 向量库/。
增量：全量重建（2200 条 CPU 约几分钟，幂等简单，检索前自动跑）。

用法: python 建向量.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EMBED_MODEL, EMBED_MODEL_DIR, IDX_DIR, VEC_DIR, VEC_META, ensure_dirs

# 首次从 HF 下载模型；国内网络不通时自动切镜像
import os
# Cherry Studio 注入的 NO_PROXY 含 `[::1]` 方括号 IPv6，会让 httpx 端口解析崩溃，必须清掉
os.environ.pop("NO_PROXY", None)
os.environ.pop("no_proxy", None)
if not os.environ.get("HF_ENDPOINT"):
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

MODEL_NAME = EMBED_MODEL
TOPIC_TEXT_LEN = 400          # 向量化文本截断（条目名+概要+摘录前部）
MODEL_DIR = EMBED_MODEL_DIR or (Path(__file__).resolve().parent.parent / "models" / "bge-small-zh-v1.5")
# huggingface_hub 新版 xet 下载协议与本机代理不兼容（requests 实测正常），手动下载到本地目录
MODEL_FILES = [
    "config.json", "model.safetensors", "tokenizer.json",
    "tokenizer_config.json", "vocab.txt", "modules.json",
    "sentence_bert_config.json", "1_Pooling/config.json",
]


def ensure_model():
    """本地模型目录缺失时用 requests 下载（跟随重定向，实测 200）。"""
    if (MODEL_DIR / "model.safetensors").exists():
        return
    import requests as rq
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model_repo = MODEL_NAME.strip("/")
    if "/" not in model_repo:
        raise RuntimeError("EMBED_MODEL 必须是 HuggingFace 仓库名，例如 BAAI/bge-small-zh-v1.5")
    base = f"https://huggingface.co/{model_repo}/resolve/main/"
    for f in MODEL_FILES:
        dest = MODEL_DIR / f
        dest.parent.mkdir(parents=True, exist_ok=True)
        for src in (base + f, f"https://hf-mirror.com/{model_repo}/resolve/main/" + f):
            try:
                r = rq.get(src, timeout=120)
                r.raise_for_status()
                dest.write_bytes(r.content)
                print(f"  ↓ {f} ({len(r.content) // 1024} KB)")
                break
            except Exception as e:
                print(f"  ⚠ {f} 从 {src.split('/')[2]} 下载失败: {e}")
        else:
            raise RuntimeError(f"模型文件下载失败: {f}")
    print("模型下载完成")


def parse_idx(path):
    """解析索引 md → 条目列表 + 文件级摘要。返回 (items, file_summary, meta)。"""
    text = path.read_text(encoding="utf-8")
    meta = {}
    src_file = ""
    artifact = ""
    # 2026-08-16 修复：[^:：] 字符类含 \n，贪婪匹配会把「---」行与下一字段连成 key
    # （如 "---\n源文件"），导致源文件字段从未被赋值 → 显式排除换行
    for m in re.finditer(r"^([^:：\n]+): (.+)$", text, re.M):
        k, v = m.group(1), m.group(2)
        if k == "源文件":
            # 源文件字段含 rel 路径，file 必须用它而非 p.name 推断。
            src_file = v.strip()
        elif k == "产物键":
            artifact = v.strip()
        elif k not in ("段落数", "条目数", "标签"):
            meta[k] = v
    items = []
    parts = re.split(r"^## (条目 \d+：|文件级摘要)", text, flags=re.M)
    # parts: [frontmatter+标题, '条目 1：', 内容, '条目 2：', 内容, ..., '文件级摘要', 内容]
    i = 1
    while i < len(parts) - 1:
        head, body = parts[i], parts[i + 1]
        if head.startswith("条目"):
            # split 捕获组把标题名留在 body 首行（head 只有「条目 N：」）
            blines = body.strip().splitlines()
            name = blines[0].strip() if blines else ""
            body2 = "\n".join(blines[1:])
            tag = re.search(r"^\s*-\s*\*\*标签\*\*:\s*(.+)$", body2, re.M)
            summary = re.search(r"^\s*-\s*\*\*概要\*\*:\s*(.+)$", body2, re.M)
            lines = re.search(r"^\s*-\s*\*\*行号\*\*:\s*(.+)$", body2, re.M)
            excerpt = re.search(r"^\s*-\s*\*\*摘录\*\*:\s*(.+)$", body2, re.M)
            items.append({
                "type": "item",
                "item_no": len(items) + 1,
                "name": name,
                "tag": tag.group(1).strip() if tag else "",
                "summary": summary.group(1).strip() if summary else "",
                "lines": lines.group(1).strip() if lines else "",
                "excerpt": excerpt.group(1).strip() if excerpt else "",
            })
        i += 2
    # 文件级摘要 = 「文件级摘要」段（去掉元数据重复）
    file_sum = ""
    if parts and "文件级摘要" in parts:
        idx = parts.index("文件级摘要")
        if idx + 1 < len(parts):
            file_sum = parts[idx + 1].strip()
    # 2026-08-16 修复：该段含文档头（「B站视频转录文档」+ 元数据行），检索清单 summary
    # 取前 120 字会显示无意义文案——定位到「核心观点」再截取；无标记时回退原逻辑
    m = re.search(r"核心观点", file_sum)
    if m:
        file_sum = file_sum[m.start():]
    return items, file_sum, meta, src_file or (path.name[:-len('-idx.md')] + '.txt'), artifact


def main():
    ensure_dirs()
    from sentence_transformers import SentenceTransformer

    ensure_model()
    print(f"加载模型 {MODEL_DIR}…")
    model = SentenceTransformer(str(MODEL_DIR))
    print("模型就绪")

    entries, texts = [], []
    idx_files = sorted(IDX_DIR.glob("*-idx.md"))
    if not idx_files:
        print("索引库为空，先跑 切条目.py")
        return
    for p in idx_files:
        items, file_sum, meta, src_file, artifact = parse_idx(p)
        # 主题条目
        for it in items:
            text = f"{it['name']}。{it['summary']}。{it['excerpt']}"
            entries.append({**it, "source_rel": src_file,
                             "index_rel": f"索引库/{p.name}",
                             "proof_rel": f"校对版/{artifact}_校对版.txt",
                             "file": src_file, "artifact_key": artifact, "meta": meta})
            texts.append(text[:TOPIC_TEXT_LEN])
        # 文件级摘要条目（ADR/B1：摘要层参与检索）
        if file_sum and len(file_sum) > 40:
            entries.append({
                "type": "file", "name": meta.get("标题", p.stem),
                "tag": "文件级", "summary": file_sum[:120],
                "lines": "全文", "excerpt": file_sum[:TOPIC_TEXT_LEN],
                "source_rel": src_file, "index_rel": f"索引库/{p.name}",
                "proof_rel": f"校对版/{artifact}_校对版.txt",
                "file": src_file, "artifact_key": artifact, "meta": meta,
            })
            texts.append(f"文件总览：{meta.get('标题', '')}。{file_sum[:TOPIC_TEXT_LEN]}")

    print(f"共 {len(entries)} 条（主题条目 + 文件级摘要）开始生成向量…")
    vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=True)
    import numpy as np
    np.save(VEC_DIR / "embeddings.npy", vecs)
    (VEC_DIR / "entries.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    VEC_META.write_text(json.dumps({
        "embedding_model": MODEL_NAME,
        "model_dir": str(MODEL_DIR),
        "dimensions": int(vecs.shape[1]),
        "entries": len(entries),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"向量库已写入: {VEC_DIR} ({len(entries)} 条, 维度 {vecs.shape[1]})")


if __name__ == "__main__":
    main()
