#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2_similarity.py - 非破坏相似候选适配层（v2.0.0 alpha.2 P06/P07）。

两条通路都只产生候选：模板近重复走上游 text-dedup 的 fingerprint/cluster/jaccard，
语义近重复走上游 FlagEmbedding/BGE 向量 + SemHash/vicinity 检索。

职责边界（设计方案 3/5 节、用户要求 13/20/22/34/37 节）：

  1. **只产生候选，不产生删除**。簇大小、相似度、高频都不是删句理由；本模块输出的
     候选一律 kind/rule_id = ``TEMPLATE_REUSE``，交给 ``v2_report.decide`` 时只会进
     ``review`` 档（规则层永不授权改写）。
  2. **非破坏**。输入候选元组一条不少、一条不改；只调用上游
     ``text_dedup.minhash.fingerprint`` 与 ``cluster``（两者源码只读 ``config.algorithm``），
     **不调用** ``remove_duplicates`` / ``save_dataset`` / 本地数据源管道，也不把上游
     去重落地结果当小说稿。
  3. **中文预设先核查**。上游默认 ``min_length=5``、按非字母正则切词是英文习惯，
     中文整句会被切成极少 token；本模块按核查结果把文本预处理成**字符 token**
     并显式给出 ``min_length``/``ngram_size``（见 P06 探针日志
     p06-upstream-minlength-probe），不重写 MinHash/SimHash，也不另写第二套指纹。
  4. **传递关系不冒充直接相似**。同一簇的成员可能只经由第三方相连；每对参照关系都用
     上游 ``jaccard_similarity`` 当场核对，达标才标 ``text-dedup-pairwise``（直接），
     否则标 ``text-dedup-cluster``（簇级、传递），交给裁判时按簇级对待。
  5. **锚点完整**。新候选沿用被匹配成员自己的原文锚点（可逐字回读），并把参照成员的
     锚点带进 ``context_anchors``；源哈希仍是成员自己订约时那份。

因为不走上游数据源管道，数据集需要自己带上游算法内部使用的 ``__INDEX__`` 列；
配置对象用最小形状交给上游（其源码只取 ``config.algorithm``），这一点由 P06 的
真实跑通测试钉住：上游换形状导致读不到字段时会直接报错，不会静默产出空候选。
"""
from __future__ import annotations

from types import SimpleNamespace

try:
    import datasets
    from semhash import SemHash
    from text_dedup import minhash as _minhash
    from text_dedup.config import MinHashAlgorithmConfig
    from text_dedup.utils.jaccard import jaccard_similarity
    from text_dedup.utils.tokenization import ngrams as _upstream_ngrams
except ModuleNotFoundError as exc:
    from runtime_contract import install_hint
    raise ModuleNotFoundError(f"{exc}；{install_hint('similarity')}", name=exc.name) from exc

import v2_contracts as vc
import v2_embeddings as vemb

TEMPLATE_RULE = "TEMPLATE_REUSE"
DIRECT_SOURCE = "text-dedup-pairwise"
CLUSTER_SOURCE = "text-dedup-cluster"
INTERNAL_INDEX = "__INDEX__"
# 中文预设（由 P06 上游核查与真实跑通测试共同钉住）：把整句按字符切成 token，
# 再用上游 ngrams(n=1) 成 shingle；min_length 2 放行短句；threshold 0.6 是簇召回门槛
# ——中文近重复在字符 unigram 上的 Jaccard 常在 0.65–0.9，0.7 会把短句漏掉。
# 是否「两句直接相似」另用上游 Jaccard 按 direct_similarity 当场核对。
DEFAULT_PRESET: dict = {"min_length": 2, "ngram_size": 1, "threshold": 0.6,
                        "num_perm": 64, "direct_similarity": 0.75,
                        # 上游默认 num_proc=os.cpu_count()，每个 worker 各自 import scipy；
                        # 16 GB 机上整本扫描会把 worker 打成 MemoryError。有界并行是安全不变量。
                        "num_proc": 1}


def upstream_algorithm(*, min_length: int, ngram_size: int, threshold: float,
                       num_perm: int = 64, num_proc: int = 1) -> MinHashAlgorithmConfig:
    """把参数交给上游算法对象——本模块不自算指纹，参数非法直接拒。"""
    if min_length < 1:
        raise ValueError(f"min_length 必须 ≥1（中文短句要靠它放行）: {min_length}")
    if ngram_size < 1:
        raise ValueError(f"ngram_size 必须 ≥1: {ngram_size}")
    if not 0.0 < threshold <= 1.0:
        raise ValueError(f"threshold 必须在 (0,1]: {threshold}")
    if num_proc < 1:
        raise ValueError(f"num_proc 必须 ≥1: {num_proc}")
    return MinHashAlgorithmConfig(algorithm_name="minhash", num_perm=num_perm,
                                  text_column="text", index_column=INTERNAL_INDEX,
                                  min_length=min_length, ngram_size=ngram_size,
                                  threshold=threshold, num_proc=num_proc)


def _config_of(config: dict | None) -> dict:
    merged = dict(DEFAULT_PRESET)
    for key, value in (config or {}).items():
        if key not in merged:
            raise ValueError(f"未知相似度参数: {key}（可调项 {sorted(merged)}）")
        merged[key] = value
    return merged


def _char_tokens(text: str) -> str:
    """中文按字符切 token 后交给上游 ngrams；不做分词器，不引入新依赖。"""
    return " ".join(list(text.strip()))


def _shingles(text: str, *, ngram_size: int, min_length: int) -> set:
    """上游 ngrams() 生成 shingle 集合——不自写切词，保证与指纹通路同源。"""
    return {" ".join(gram) for gram in
            _upstream_ngrams(_char_tokens(text).split(" "), ngram_size, min_length)}


def _pairs_similar(a: str, b: str, *, ngram_size: int, min_length: int,
                   floor: float) -> bool:
    """用上游 Jaccard 当场核对两句是否**直接**相似（簇传递关系不算直接）。"""
    left = _shingles(a, ngram_size=ngram_size, min_length=min_length)
    right = _shingles(b, ngram_size=ngram_size, min_length=min_length)
    if not left or not right:
        return False
    return float(jaccard_similarity(left, right)) >= floor


def find_template_candidates(records: tuple[vc.CandidateRecord, ...], *,
                             texts: dict[str, str],
                             config: dict) -> tuple[vc.CandidateRecord, ...]:
    """在给定候选之间找模板近重复，返回**新增**的 TEMPLATE_REUSE 候选。

    ``texts`` 按 ``source_anchor.segment_id`` 给出该候选所在的可比较文本；缺文本即拒，
    不猜内容。输入 ``records`` 不被修改、不被消耗。
    """
    preset = _config_of(config)
    if not records:
        return ()
    missing = sorted(r.source_anchor.segment_id for r in records
                     if r.source_anchor.segment_id not in texts)
    if missing:
        raise ValueError(f"texts 缺少这些候选的可比较文本: {missing}（不猜内容）")

    by_segment: dict[str, list[vc.CandidateRecord]] = {}
    for record in records:
        by_segment.setdefault(record.source_anchor.segment_id, []).append(record)
    segment_ids = sorted(by_segment)

    rows = [{INTERNAL_INDEX: index, "text": _char_tokens(texts[sid])}
            for index, sid in enumerate(segment_ids)]
    dataset = datasets.Dataset.from_list(rows)
    algorithm = upstream_algorithm(min_length=preset["min_length"],
                                   ngram_size=preset["ngram_size"],
                                   threshold=preset["threshold"],
                                   num_perm=preset["num_perm"],
                                   num_proc=preset["num_proc"])
    upstream_config = SimpleNamespace(algorithm=algorithm)
    fingerprinted = _minhash.fingerprint(upstream_config, dataset)
    clusters = {int(sid): int(cid) for sid, cid in
                dict(_minhash.cluster(upstream_config, fingerprinted)).items()}

    grouped: dict[int, list[int]] = {}
    for sid, cid in clusters.items():
        grouped.setdefault(cid, []).append(sid)

    floor = float(preset["direct_similarity"])
    found: list[vc.CandidateRecord] = []
    for cid in sorted(grouped):
        members = [segment_ids[i] for i in sorted(grouped[cid])]
        if len(members) < 2:
            continue
        for index, segment in enumerate(members):
            references = []
            relation = CLUSTER_SOURCE
            for other in members:
                if other == segment:
                    continue
                direct = _pairs_similar(texts[segment], texts[other],
                                        ngram_size=preset["ngram_size"],
                                        min_length=preset["min_length"], floor=floor)
                if direct:
                    relation = DIRECT_SOURCE
                # 非直接的参照关系保留锚点，但只按簇级（传递）身份交给裁判
                references.extend(by_segment[other])
            anchors = tuple(rec.source_anchor for rec in references)
            for rec in by_segment[segment]:
                found.append(vc.CandidateRecord(
                    candidate_id=f"tpl::{rec.candidate_id}::{cid}",
                    rule_id=TEMPLATE_RULE, kind=TEMPLATE_RULE,
                    source_anchor=rec.source_anchor, context_anchors=anchors,
                    discovery_source=relation, source_hash=rec.source_hash,
                    context_hash=rec.context_hash,
                    retrieval_score=floor if relation == DIRECT_SOURCE else None))
    return tuple(found)


# ---------------------------------------------------------------- 语义近重复（P07）
# 复用上游 FlagEmbedding/BGE（向量由提供方给出）与 SemHash+vicinity（近邻检索），
# 本模块只做锚点映射与候选形状；相似度只产生候选，删除权限仍归裁判。
SEMANTIC_RULE = "SEMANTIC_ECHO"
SEMANTIC_SOURCE = "bge-semhash-cosine"
# threshold 0.75 由钉定 bge-small-zh-v1.5 实测算出（探针 p07-threshold-probe-r2）：
# 「同义重述」对落在 0.776–0.855，「内容不同只是话题接近」的对最高 0.578，无关句 0.25–0.31，
# 0.75 卡在两类之间。真实中文回归集校准前，本阈值只决定候选召回，不给任何删除权限；
# ann_backend=basic 用 vicinity 的精确全近邻，小批量不做近似检索以免「漏检」被当成「不相似」。
# window_overlap 让跨边界的句子至少完整落进一个窗口。
SEMANTIC_PRESET: dict = {"threshold": 0.75, "ann_backend": "basic",
                         "window_overlap": 8}


def _semantic_config_of(config: dict | None) -> dict:
    merged = dict(SEMANTIC_PRESET)
    for key, value in (config or {}).items():
        if key not in merged:
            raise ValueError(f"未知语义参数: {key}（可调项 {sorted(merged)}）")
        merged[key] = value
    if not 0.0 < float(merged["threshold"]) <= 1.0:
        raise ValueError(f"threshold 必须在 (0,1]: {merged['threshold']}")
    if int(merged["window_overlap"]) < 0:
        raise ValueError(f"window_overlap 不得为负: {merged['window_overlap']}")
    return merged


def find_semantic_candidates(records: tuple[vc.CandidateRecord, ...], *,
                             texts: dict[str, str],
                             provider: vemb.EmbeddingProvider,
                             config: dict) -> tuple[vc.CandidateRecord, ...]:
    """在给定候选之间找语义近重复，返回**新增**的 SEMANTIC_ECHO 候选。

    超长可比较文本先按提供方的 token 预算切成完整覆盖的窗口，窗口只作编码单位，
    候选锚点仍是父锚点；同一段落内部的自相似不产生候选（那是回声检测的空转）。
    """
    preset = _semantic_config_of(config)
    if len(records) < 2:
        return ()
    missing = sorted(r.source_anchor.segment_id for r in records
                     if r.source_anchor.segment_id not in texts)
    if missing:
        raise ValueError(f"texts 缺少这些候选的可比较文本: {missing}（不猜内容）")

    units: list[vemb.WindowUnit] = []
    for record in records:
        units.extend(vemb.segment_windows(
            record, text=texts[record.source_anchor.segment_id],
            measure=provider.token_count, budget=provider.max_tokens,
            overlap=int(preset["window_overlap"])))

    vectors = provider.encode(tuple(unit.excerpt for unit in units))
    rows = [{"uid": str(index), "text": unit.excerpt} for index, unit in enumerate(units)]
    index = SemHash.from_embeddings(vectors, rows, model=provider,
                                    columns=["uid", "text"],
                                    ann_backend=str(preset["ann_backend"]))
    threshold = float(preset["threshold"])
    result = index.self_deduplicate(threshold=threshold)

    by_candidate = {record.candidate_id: record for record in records}
    best: dict[tuple[str, str], tuple[float, int, int]] = {}
    for duplicate in result.filtered:
        left = units[int(duplicate.record["uid"])]
        right = units[int(duplicate.duplicate_of["uid"])]
        if left.candidate_id == right.candidate_id:
            continue
        score = float(duplicate.score)
        if score < threshold:
            continue
        pair = (left.candidate_id, right.candidate_id)
        if pair not in best or score > best[pair][0]:
            best[pair] = (score, int(duplicate.record["uid"]),
                          int(duplicate.duplicate_of["uid"]))

    found: list[vc.CandidateRecord] = []
    for (left_id, right_id), (score, _, _) in sorted(best.items()):
        record = by_candidate[left_id]
        peer = by_candidate[right_id]
        found.append(vc.CandidateRecord(
            candidate_id=f"sem::{left_id}::{right_id}",
            rule_id=SEMANTIC_RULE, kind=SEMANTIC_RULE,
            source_anchor=record.source_anchor,
            context_anchors=(peer.source_anchor,),
            discovery_source=SEMANTIC_SOURCE, source_hash=record.source_hash,
            context_hash=record.context_hash, retrieval_score=score))
    return tuple(found)
