#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语义检索脚本 v5.0（知识库双路召回增强版，融合 ADR-001）

加载本地语义索引，对自然语言查询进行本地 Ollama 向量余弦相似度召回 + 中文同义词意图扩词加权。

用法：
  python semantic_search.py "查询语句" --lib <知识库路径> [--top-k 5] [--threshold 0.3]
  python semantic_search.py "查询语句" --lib <知识库路径> --json
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(SKILL_DIR, ".env"))
except Exception:
    pass

from _logging import add_log_format_argument, configure_logging, get_logger

import config as v5config

DEFAULT_OLLAMA_MODEL = v5config.DEFAULT_OLLAMA_MODEL
INDEX_DIR_NAME = v5config.INDEX_DIR_NAME
MANIFEST_NAME = v5config.MANIFEST_NAME

INDEX_LIB_DIR = v5config.get_config().lib_dir

logger = get_logger(__name__)

# 中文问句功能词：向量化前剥离，避免“什么是/怎么”等功能词把排序带偏。
# （实测：“什么是过拟合”含词块掉到 63 名外，剥离后精确词加权可浮上首屏）
# 注意：“什么叫/叫做”必须整体剥离，否则残留“叫做”污染 must_terms。
QUESTION_PREFIX_RE = re.compile(
    r"^(?:请问|请教|什么叫做|什么叫|什么是|是什么|怎么|如何|怎样|为什么|为何|有哪些|"
    r"哪一个|哪种|哪个|谁是|何为|介绍一下|讲讲|说说|聊聊|总结一下)"
    r"[\s，,。？?：:、]*"
)
QUESTION_SUFFIX_RE = re.compile(
    r"(?:是什么|是什么意思|叫什么|有哪些|有哪几种|有哪些方法|怎么办|怎么解决|"
    r"如何解决|是什么原因|为什么|是怎么回事)"
    r"[\s？?。]*$"
)


def clean_query(query: str) -> str:
    """剥离问句功能词，保留实体与主题词。空则回退原查询。"""
    q = (query or "").strip()
    q = QUESTION_PREFIX_RE.sub("", q).strip()
    q = QUESTION_SUFFIX_RE.sub("", q).strip()
    return q or query.strip()


def is_definition_query(query: str) -> bool:
    """定义型查询判定：含 什么是/是什么/什么叫/定义/含义/概念。"""
    return bool(re.search(r"什么是|是什么|什么叫|定义|含义|概念", (query or "").strip()))


def definition_core_terms(query: str) -> list[str]:
    """定义查询核心词：只取整词分段（滑动二字不入 must，避免“过拟/拟合”泛化）。

    实测：“回测过拟合的防范方法”整词为 [回测过拟合的防范方法/回测]，
    must 取交集后只剩有效整词；“大模型为什么会产生幻觉”整词无有效项
    则 must 为空，退化为普通加权查询，不误杀。
    """
    core = clean_query(query)
    parts = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{2,}", core)
    stop = {"什么", "叫做", "什么叫", "怎么", "为什么", "为何", "如何", "怎样",
            "哪些", "哪个", "哪种", "谁是", "何为", "介绍", "讲讲", "说说", "聊聊",
            "总结", "一下", "意思", "含义", "定义", "概念", "防范", "方法",
            "产生", "幻觉"}
    out = [p for p in parts if p not in stop and len(p) >= 2][:4]
    # 长整词拆出有效子整词（“回测过拟合的防范方法”→“回测/过拟合”），
    # 子词必须≥2 字且不在停用表，保证 must 全是有效整词
    if out and len(out[0]) >= 5 and not re.search(r"[A-Za-z]", out[0]):
        subs = re.findall(r"[\u4e00-\u9fff]{2,}", out[0])
        out = [s for s in subs if s not in stop and len(s) >= 2][:4] or out[:1]
    return out


def chunk_contains_term(chunk: dict[str, Any], term: str) -> bool:
    """候选块标题+章节名+摘要全文是否含词（定义过滤用全文，不只看前 600 字）。"""
    hay = (
        str(chunk.get("title") or "")
        + "\n" + str(chunk.get("chapter_title") or "")
        + "\n" + str(chunk.get("summary") or "")
        + "\n" + str(chunk.get("full_content") or "")[:2000]
    )
    return term in hay


def keyword_terms(query: str) -> list[str]:
    """精确关键词抽取（防污染三原则）。

    1. 整词优先：连续 2+ 汉字 / 2+ 字母数字成段，中英独立成词，绝不跨语言拼接
       （“什么叫做skill”→["skill"]，不产“做s”这类垃圾）。
    2. 滑动二字仅限纯中文：含字母/数字的 bigram 一律丢弃；且短查询（<6 字）不兜底，
       “过拟合”本身已是整词，无需再切“过拟/拟合”。
    3. 功能词残留过滤：“什么/叫做/怎么/为什么”等问句词即使漏网也不入词表。
    """
    stop = {"什么", "叫做", "什么叫", "怎么", "为什么", "为何", "如何", "怎样",
            "哪些", "哪个", "哪种", "谁是", "何为", "介绍", "讲讲", "说说", "聊聊",
            "总结", "一下", "意思", "含义", "定义", "概念", "防范", "方法",
            "产生", "幻觉", "什么", "么会", "为什", "型为", "大模", "合的"}
    parts = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{2,}|[0-9]{2,}", query or "")
    seen, out = set(), []
    for p in parts:
        if p not in seen and p not in stop:
            seen.add(p)
            out.append(p)
    # 纯中文长查询才做二字兜底；中英混合查询跳过（英文整词已足够精确）
    if len(query or "") >= 8 and not re.search(r"[A-Za-z]", query or ""):
        chars = re.sub(r"\s+", "", query or "")
        for i in range(len(chars) - 1):
            bigram = chars[i:i + 2]
            if (re.fullmatch(r"[\u4e00-\u9fff]{2}", bigram)
                    and bigram not in seen and bigram not in stop):
                seen.add(bigram)
                out.append(bigram)
    return out[:8]


# 中文同义词与意图扩词表（轻量零 Token 辅助增强召回）
QUERY_EXPANSION = {
    "架构": ["设计", "系统架构", "模块", "解耦", "组件", "微服务", "高可用"],
    "性能": ["优化", "延迟", "并发", "吞吐", "耗时", "加速", "瓶颈", "缓存"],
    "算法": ["模型", "计算", "逻辑", "优化算法", "复杂度", "推理"],
    "数据": ["指标", "样本", "统计", "数据集", "特征", "字段", "数据流"],
    "规范": ["约束", "原则", "标准", "流程", "最佳实践", "协议", "约定"],
    "交易": ["回测", "策略", "因子", "夏普", "收益", "风险", "对冲", "量化"],
    "写作": ["伏笔", "文风", "金句", "情节", "人物", "人设", "转折", "爽点"],
}


def _char_ngrams(text: str) -> list[str]:
    clean = re.sub(r"\s+", "", (text or "").lower())
    grams = []
    for n in (1, 2, 3):
        grams.extend(clean[i:i + n] for i in range(max(0, len(clean) - n + 1)))
    return grams


_LEXICAL_CACHE: dict = {}
LEXICAL_SCHEMA_VERSION = "lexical-v1"
LEXICAL_NGRAM_RANGE = (1, 2, 3)
LEXICAL_DOC_LIMIT = 600
SCORE_VERSION = "hybrid-v2"


def _document_text(chunk: dict[str, Any]) -> str:
    return str(chunk.get("full_content") or chunk.get("summary") or "")[:LEXICAL_DOC_LIMIT]


def lexical_snapshot_name(build_id: str) -> str:
    return f"lexical_index.{build_id}.npz"


def vector_snapshot_name(build_id: str) -> str:
    return f"knowledge_vectors.{build_id}.npz"


class LexicalIndex:
    """字符 n-gram TF-IDF 倒排索引：一次构建，多次毫秒级查询。

    身份只认 build_id：同一 build_id 不重复构建；build_id 变化强制重建；
    相同块数但内容变化不得复用旧缓存（旧弱缓存键已删除）。
    """

    def __init__(self, chunks: list[dict[str, Any]], build_id: str | None):
        from collections import Counter
        import math
        if not build_id:
            build_id = "legacy"
        self.build_id = build_id
        self.document_count = len(chunks)
        counters = [Counter(_char_ngrams(_document_text(c))) for c in chunks]
        n_docs = max(1, len(counters))
        df: dict[str, int] = {}
        for counter in counters:
            for term in counter:
                df[term] = df.get(term, 0) + 1
        self.idf = {term: math.log((n_docs + 1) / (freq + 1)) + 1.0 for term, freq in df.items()}
        self.postings: dict[str, list[tuple[int, float]]] = {}
        self._doc_norm: list[float] = []
        for doc_index, counter in enumerate(counters):
            weights: dict[str, float] = {}
            for term, count in counter.items():
                weights[term] = (1.0 + math.log(count)) * self.idf[term]
            norm = math.sqrt(sum(v * v for v in weights.values())) or 1.0
            self._doc_norm.append(norm)
            for term, weight in weights.items():
                self.postings.setdefault(term, []).append((doc_index, weight / norm))

    def score(self, query: str) -> Any:
        """只计算查询向量并遍历命中 postings，返回与 chunks 等长的 float32 分数数组。"""
        import math
        from collections import Counter
        import numpy as np
        expanded = query + " " + " ".join(
            s for key, values in QUERY_EXPANSION.items() if key in query for s in values[:4]
        )
        q_counter = Counter(_char_ngrams(expanded))
        q_weights = {
            term: (1.0 + math.log(count)) * self.idf.get(term, math.log((self.document_count + 1) / 1) + 1.0)
            for term, count in q_counter.items()
        }
        qnorm = math.sqrt(sum(v * v for v in q_weights.values())) or 1.0
        scores = np.zeros(self.document_count, dtype=np.float32)
        for term, q_weight in q_weights.items():
            posting = self.postings.get(term)
            if not posting:
                continue
            qw = q_weight / qnorm
            for doc_index, d_weight in posting:
                scores[doc_index] += qw * d_weight
        return scores

    def to_snapshot(self) -> dict[str, Any]:
        """序列化为可持久化快照（terms/postings/norms/idf 全量）。"""
        import numpy as np
        terms = sorted(self.postings.keys())
        offsets = [0]
        doc_ids: list[int] = []
        weights: list[float] = []
        for term in terms:
            for doc_index, weight in self.postings[term]:
                doc_ids.append(int(doc_index))
                weights.append(float(weight))
            offsets.append(len(doc_ids))
        return {
            "schema": LEXICAL_SCHEMA_VERSION,
            "build_id": self.build_id,
            "document_count": int(self.document_count),
            "ngram_range": list(LEXICAL_NGRAM_RANGE),
            "doc_limit": int(LEXICAL_DOC_LIMIT),
            "terms": terms,
            "offsets": np.asarray(offsets, dtype=np.int64),
            "doc_ids": np.asarray(doc_ids, dtype=np.int64),
            "weights": np.asarray(weights, dtype=np.float32),
            "doc_norms": np.asarray(self._doc_norm, dtype=np.float32),
            "idf_terms": np.asarray(terms),
            "idf_values": np.asarray([self.idf[t] for t in terms], dtype=np.float64),
        }

    @classmethod
    def from_snapshot(cls, snap: dict[str, Any]) -> "LexicalIndex":
        """从持久化快照重建（零分词、零 Counter，Web 启动直接加载）。"""
        if snap.get("schema") != LEXICAL_SCHEMA_VERSION:
            raise ValueError(f"词法快照 schema 不兼容：{snap.get('schema')} != {LEXICAL_SCHEMA_VERSION}")
        obj = cls.__new__(cls)
        obj.build_id = snap["build_id"]
        obj.document_count = int(snap["document_count"])
        terms = [str(t) for t in snap["terms"]]
        import numpy as np
        offsets = np.asarray(snap["offsets"], dtype=np.int64).tolist()
        doc_ids = np.asarray(snap["doc_ids"], dtype=np.int64).tolist()
        weights = np.asarray(snap["weights"], dtype=np.float32).tolist()
        obj._doc_norm = np.asarray(snap["doc_norms"], dtype=np.float32).tolist()
        idf_terms = [str(t) for t in np.asarray(snap["idf_terms"]).tolist()]
        idf_values = np.asarray(snap["idf_values"], dtype=np.float64).tolist()
        obj.idf = dict(zip(idf_terms, idf_values))
        obj.postings = {}
        for i, term in enumerate(terms):
            s, e = int(offsets[i]), int(offsets[i + 1])
            obj.postings[term] = [(int(doc_ids[j]), float(weights[j])) for j in range(s, e)]
        return obj

    @classmethod
    def load_file(cls, path: str, expected_build_id: str | None = None) -> "LexicalIndex":
        import numpy as np
        with np.load(path, allow_pickle=True) as data:
            meta = json.loads(str(data["meta"].item()))
            snap = {
                "schema": str(meta.get("schema")),
                "build_id": str(meta.get("build_id")),
                "document_count": int(meta.get("document_count")),
                "terms": [str(t) for t in data["terms"].tolist()],
                "offsets": np.asarray(data["offsets"]),
                "doc_ids": np.asarray(data["doc_ids"]),
                "weights": np.asarray(data["weights"]),
                "doc_norms": np.asarray(data["doc_norms"]),
                "idf_terms": [str(t) for t in data["idf_terms"].tolist()],
                "idf_values": np.asarray(data["idf_values"]),
            }
        if expected_build_id is not None and snap["build_id"] != expected_build_id:
            raise ValueError(f"词法快照 build_id 不一致：{snap['build_id']} != {expected_build_id}")
        return cls.from_snapshot(snap)


def save_lexical_snapshot(index: LexicalIndex, path: str) -> str:
    """持久化词法快照并返回 sha256。调用方负责关闭句柄后回读校验。"""
    import hashlib
    import numpy as np
    snap = index.to_snapshot()
    meta = json.dumps({
        "schema": snap["schema"],
        "build_id": snap["build_id"],
        "document_count": snap["document_count"],
        "ngram_range": snap["ngram_range"],
        "doc_limit": snap["doc_limit"],
    }, ensure_ascii=False)
    np.savez_compressed(
        path,
        meta=np.asarray(meta),
        terms=np.asarray(snap["terms"]),
        offsets=np.asarray(snap["offsets"]),
        doc_ids=np.asarray(snap["doc_ids"]),
        weights=np.asarray(snap["weights"]),
        doc_norms=np.asarray(snap["doc_norms"]),
        idf_terms=np.asarray(snap["idf_terms"]),
        idf_values=np.asarray(snap["idf_values"]),
    )
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def get_lexical_index(chunks: list[dict[str, Any]], build_id: str | None) -> LexicalIndex:
    """按 build_id 缓存词法索引：命中直接复用，未命中重建并替换。

    build_id 为空（旧测试/兼容路径）时使用临时身份 'legacy' 现场构建，
    正式 Web/CLI 路径必须传入真实 manifest.build_id。
    """
    key = build_id or "legacy"
    cached = _LEXICAL_CACHE.get("index")
    if cached is not None and cached.build_id == key and cached.document_count == len(chunks):
        return cached
    index = LexicalIndex(chunks, key)
    _LEXICAL_CACHE["index"] = index
    return index


def _tfidf_scores(
    query: str,
    chunks: list[dict[str, Any]],
    lexical_index: LexicalIndex | None = None,
    build_id: str | None = None,
) -> list[float]:
    """兼容入口：优先使用传入的 LexicalIndex；为空则按 build_id 构建并记警告。"""
    index = lexical_index
    if index is None:
        logger.warning("词法索引现场构建（正式路径应预先传入）：build_id=%s", build_id)
        index = get_lexical_index(chunks, build_id)
    elif build_id is not None and index.build_id != build_id:
        raise ValueError(f"词法索引 build_id 不一致：{index.build_id} != {build_id}")
    return [float(v) for v in index.score(query)]


def _npz_str(value: Any) -> str:
    if hasattr(value, "item"):
        value = value.item()
    return str(value)


def load_semantic_index(semantic_dir: str):
    import numpy as np

    manifest_path = os.path.join(semantic_dir, MANIFEST_NAME)
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(
            f"未找到语义索引清单: {manifest_path}，请先运行 python scripts/semantic_index.py --build --lib <库路径>"
        )

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    # 损坏 manifest 直接失败，不静默跳过：缺 segments/维度/数量字段即拒绝
    if not isinstance(manifest.get("segments"), list) or not manifest["segments"]:
        raise ValueError("语义索引清单损坏：segments 缺失或为空，请重建索引")
    if manifest.get("dimension") is None or manifest.get("chunk_count") is None:
        raise ValueError("语义索引清单损坏：dimension/chunk_count 缺失，请重建索引")

    all_embeddings = []
    all_chunks = []

    for entry in manifest.get("segments", []):
        npz_name = entry["npz"]
        npz_path = os.path.join(semantic_dir, npz_name)
        # staging 不得被当成正式索引：拒绝目录外的引用
        if os.path.isabs(npz_name) or ".." in str(npz_name).replace("\\", "/").split("/"):
            raise ValueError(f"语义索引引用越界：{npz_name}，请重建索引")
        if not os.path.exists(npz_path):
            raise ValueError(f"语义索引文件缺失：{entry['npz']}，请重建索引")
        # sha256 校验（v5.4 manifest 必带；旧 manifest 缺字段则跳过）
        if entry.get("sha256"):
            h = hashlib.sha256()
            with open(npz_path, "rb") as f:
                for block in iter(lambda: f.read(65536), b""):
                    h.update(block)
            if h.hexdigest() != entry["sha256"]:
                raise ValueError(f"语义索引文件哈希不匹配：{entry['npz']}，请重建索引")
        try:
            # Windows 下必须关闭 npz 句柄，否则 Web 持有时阻止重建替换
            with np.load(npz_path, allow_pickle=False) as data:
                embeddings = np.asarray(data["embeddings"], dtype=np.float32).copy()
                chunks = json.loads(_npz_str(data["chunks"]))
            all_embeddings.append(embeddings)
            all_chunks.extend(chunks)
        except Exception as e:
            raise ValueError(f"语义索引文件损坏：{entry['npz']}（{e}），请重建索引") from e

    if not all_embeddings:
        raise ValueError("语义索引库为空或加载失败")

    embeddings = np.vstack(all_embeddings)
    if not bool(np.isfinite(embeddings).all()):
        raise ValueError("语义索引向量含非有限值，请重建索引")
    expected = manifest.get("dimension")
    if expected and embeddings.shape[1] != int(expected):
        raise ValueError(
            f"语义索引维度损坏：manifest={expected}，实际={embeddings.shape[1]}，请重建索引"
        )
    if len(embeddings) != len(all_chunks):
        raise ValueError("语义索引向量数量与知识块数量不一致，请重建索引")
    if int(manifest.get("chunk_count", len(all_chunks))) != len(all_chunks):
        raise ValueError("语义索引清单数量与知识块数量不一致，请重建索引")
    ids = [c.get("chunk_id") for c in all_chunks]
    if len(ids) != len(set(ids)):
        raise ValueError("语义索引 chunk_id 不唯一，请重建索引")
    return manifest, embeddings, all_chunks


def _keyword_bonus(chunk: dict[str, Any], keywords: list[str], is_def_q: bool) -> float:
    """关键词加权（正常与 fallback 共用同一规则）。"""
    title = chunk.get("title", "") or ""
    snippet = str(chunk.get("chapter_title") or "") + "\n" + str(chunk.get("summary") or "")[:600]
    bonus = 0.0
    for kw in keywords:
        if kw in title:
            bonus += 1.20
        elif kw in snippet:
            bonus += 0.60
    if is_def_q and keywords:
        sec = str(chunk.get("summary") or "")
        for m in re.finditer(r"\[概念\][^\n]{0,120}", sec):
            if any(kw in m.group(0) for kw in keywords):
                bonus += 0.80
                break
    return bonus


def build_scored_result(
    chunk: dict[str, Any],
    vector_score: float,
    lexical_score: float,
    keyword_bonus: float,
    filtered: bool,
    result_source: str,
) -> dict[str, Any]:
    """正常与 fallback 共用同一得分公式：0.7×vector + 0.3×lexical + bonus。

    注意浮点舍入：vector_score/lexical_score/keyword_bonus 均已 round(4)，
    final 取三者 round 值合成后再 round(4)，测试按同一口径复算。
    """
    final_score = round(
        0.70 * round(float(vector_score), 4)
        + 0.30 * round(float(lexical_score), 4)
        + round(float(keyword_bonus), 4),
        4,
    )
    return {
        **chunk,
        "vector_score": round(float(vector_score), 4),
        "lexical_score": round(float(lexical_score), 4),
        "keyword_bonus": round(float(keyword_bonus), 4),
        "final_score": final_score,
        "score": final_score,
        "score_version": SCORE_VERSION,
        "filtered": filtered,
        "result_source": result_source,
    }


def search_hybrid(
    query: str,
    embedder,
    embeddings: Any,
    chunks: list[dict[str, Any]],
    lexical_index: LexicalIndex | None = None,
    top_k: int = 5,
    threshold: float = 0.3,
    build_id: str | None = None,
) -> list[dict[str, Any]]:
    import numpy as np

    # 查询长度上限 1~500 字，避免超长输入耗尽 CPU
    if not (query or "").strip() or len(query) > 500:
        raise ValueError("查询长度必须为 1~500 字")
    # 0. 问句清洗：先剥“什么是/怎么/有哪些”，再向量化，避免功能词带偏排序
    core_query = clean_query(query)

    # 1. 意图扩词（基于清洗后的核心查询）
    expanded_words = []
    for term, synonyms in QUERY_EXPANSION.items():
        if term in core_query:
            expanded_words.extend(synonyms)

    augmented_query = core_query
    if expanded_words:
        augmented_query += " " + " ".join(expanded_words[:4])

    # 2. 向量嵌入
    query_vec = embedder.encode([augmented_query])[0]

    # 3. 基础余弦相似度 + 确定性的词法 TF-IDF（倒排索引，毫秒级）
    scores = np.dot(embeddings, query_vec)
    lexical_scores = _tfidf_scores(core_query, chunks, lexical_index, build_id)

    # 4. 精确关键词加权（毫秒级，只扫标题+摘要前 600 字）：
    # 含查询实体的块必须浮上来。标题命中 +1.20，摘要命中 +0.60，
    # 权重足以纠正向量排序（此前 +0.50/+0.30 被向量分淹没导致过拟合沉底）。
    # 定义优先：查询含“什么是/是什么/定义/含义”时，[概念]条目含关键词再 +0.80，
    # 保证定义型答案排在机制/案例型答案前面。
    keywords = keyword_terms(core_query)
    must_terms = definition_core_terms(query) if is_definition_query(query) else []
    is_def_q = bool(must_terms)
    filter_applied = bool(must_terms)
    final_results = []
    for idx, base_score in enumerate(scores):
        vector_score = float(base_score)
        lexical_score = float(lexical_scores[idx])
        chunk = chunks[idx]
        # 定义查询强制含词：全文不含任一核心词直接淘汰（RAG 卡混入即在此拦截）。
        if must_terms and not any(chunk_contains_term(chunk, t) for t in must_terms):
            continue
        bonus = _keyword_bonus(chunk, keywords, is_def_q)
        scored = build_scored_result(chunk, vector_score, lexical_score, bonus, True, "filtered")
        if scored["final_score"] >= threshold:
            final_results.append(scored)

    final_results.sort(key=lambda x: x["score"], reverse=True)
    # 兜底：定义过滤后不足 top_k（库内真无含词内容）时，按原排序补足。
    # 身份用稳定 chunk_id 判定，禁止 id(dict)；补齐结果走同一得分公式，
    # lexical_score 用真实词法分（不写 0），result_source=fallback。
    fallback_used = False
    if must_terms and len(final_results) < top_k:
        selected_ids = {r.get("chunk_id") for r in final_results if r.get("chunk_id")}
        fallback = []
        for idx in __import__("numpy").argsort(scores)[::-1]:
            c = chunks[int(idx)]
            chunk_id = c.get("chunk_id")
            if not chunk_id or chunk_id in selected_ids:
                continue
            bonus = _keyword_bonus(c, keywords, is_def_q)
            scored = build_scored_result(
                c, float(scores[int(idx)]), float(lexical_scores[int(idx)]),
                bonus, False, "fallback",
            )
            fallback.append(scored)
            selected_ids.add(chunk_id)
            if len(final_results) + len(fallback) >= top_k:
                break
        if fallback:
            fallback_used = True
            final_results = (final_results + fallback)[:top_k]
    out = final_results[:top_k]
    for r in out:
        r["_filter_applied"] = filter_applied
        r["_fallback_used"] = fallback_used
    return out


def search(
    query: str,
    model,
    embeddings: Any,
    chunks: list[dict[str, Any]],
    top_k: int = 5,
    threshold: float = 0.3,
) -> list[dict[str, Any]]:
    """v4 兼容入口。"""
    return search_hybrid(query, model, embeddings, chunks, top_k=top_k, threshold=threshold)


def load_embedding_model(model_name: str):
    """v4 兼容入口，便于调用方注入或替换测试模型。"""
    from semantic_index import load_embedding_engine
    return load_embedding_engine(model_name)


def _clean_title(title: str) -> str:
    """统一标题清洗：去首尾书名号，避免《《独立文档集》》叠套。"""
    t = (title or "").strip()
    while t.startswith("《《") and t.endswith("》》"):
        t = "《" + t[2:-2] + "》"
    return t


def _format_result(index: int, r: dict[str, Any]) -> str:
    lines = [
        f"[{index}] 综合排序分 {r.get('final_score', r.get('score', 0)):.3f} "
        f"(向量 {r.get('vector_score', 0):.3f} / 词法 {r.get('lexical_score', 0):.3f} / "
        f"关键词+{r.get('keyword_bonus', 0):.2f}) | 资料: {_clean_title(r['title'])}（{r['author']}）",
        f"    索引卡片: {r['index_path']}",
    ]
    if r.get("segment_path"):
        lines.append(f"    原文段落: {r['segment_path']}")
    if r.get("segment_line_start") and r.get("segment_line_end"):
        lines.append(f"    行号锚点: 行 {r['segment_line_start']} ~ {r['segment_line_end']}")
    lines.append(f"    核心摘要: {r['summary']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库本地语义混合检索工具 v5.0")
    parser.add_argument("query", help="自然语言查询语句")
    parser.add_argument("--lib", default=INDEX_LIB_DIR, help="知识库根目录")
    parser.add_argument("--top-k", type=int, default=5, help="返回结果数量")
    parser.add_argument("--threshold", type=float, default=0.25, help="相似度阈值")
    # 默认取 .env 的 INDEX_EMBEDDING_MODEL（单一真相），而非硬编码常量：
    # 硬编码会让 --model 默认值绕过 .env，导致查询模型与已建索引维度不一致。
    parser.add_argument("--model", default=v5config.get_config().embedding_model,
                        help="向量模型名称（默认取 .env 的 INDEX_EMBEDDING_MODEL）")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    add_log_format_argument(parser)
    args = parser.parse_args()
    configure_logging(fmt=args.log_format)

    semantic_dir = os.path.join(args.lib, INDEX_DIR_NAME)

    try:
        manifest, embeddings, chunks = load_semantic_index(semantic_dir)
        embedder = load_embedding_model(args.model)
        expected_dim = manifest.get("dimension")
        actual_dim = getattr(embedder, "dim", None)
        if actual_dim is None and hasattr(embedder, "get_sentence_embedding_dimension"):
            actual_dim = embedder.get_sentence_embedding_dimension()
        if expected_dim and actual_dim and int(expected_dim) != int(actual_dim):
            raise ValueError(
                f"查询模型维度 {actual_dim} 与索引维度 {expected_dim} 不一致；请启用原模型或重建索引"
            )
    except Exception as exc:
        logger.error("❌ 初始化语义检索失败: %s", exc)
        return 1

    t0 = time.time()
    lexical_index = get_lexical_index(chunks, manifest.get("build_id"))
    results = search_hybrid(
        args.query,
        embedder,
        embeddings,
        chunks,
        lexical_index,
        top_k=args.top_k,
        threshold=args.threshold,
        build_id=manifest.get("build_id"),
    )
    elapsed = time.time() - t0

    if args.json:
        payload = {
            "query": args.query,
            "model": args.model,
            "top_k": args.top_k,
            "threshold": args.threshold,
            "elapsed_ms": round(elapsed * 1000, 2),
            "count": len(results),
            "results": results,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("=" * 60)
        print(f" 语义检索结果: 「{args.query}」 (耗时 {elapsed * 1000:.0f}ms)")
        print("=" * 60)
        if not results:
            print("未找到高度吻合的段落卡片，请检查关键词或重新构建向量索引。")
        else:
            for i, r in enumerate(results, 1):
                print(_format_result(i, r))
                print("-" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
