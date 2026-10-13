#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识库语义索引构建器。

该模块同时保留 v4 的公开函数名，避免升级后已有编排器和测试失效。
索引清单记录真实引擎与向量维度，查询端不得静默换用不同维度的引擎。

失败策略（P0）：任何 embedding 失败都抛错并停止本次构建，旧索引不被覆盖；
禁止零向量/空向量/静默换模型。hash 向量只允许离线测试模式（engine=test）。
发布策略（P0）：staging 临时目录 + 校验 + 同卷原子替换，manifest 最后写入；
进程锁禁止同一 library 并发构建。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
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

import config as v5config
from config import assert_vector
from _common import parse_frontmatter, read_text_auto
from _logging import add_log_format_argument, configure_logging, get_logger

# 统一配置单源真相（本模块不再维护独立默认值）
DEFAULT_OLLAMA_MODEL = v5config.DEFAULT_OLLAMA_MODEL
DEFAULT_ST_MODEL = v5config.DEFAULT_ST_MODEL
INDEX_DIR_NAME = v5config.INDEX_DIR_NAME
MANIFEST_NAME = v5config.MANIFEST_NAME
INDEX_VERSION = v5config.INDEX_VERSION
CHUNK_STRATEGY = v5config.CHUNK_STRATEGY
BUILDER_VERSION = INDEX_VERSION

_LINE_RANGE_RE = re.compile(r"(?:[（(]行号[：:]\s*(\d+)\s*[~～-]\s*(\d+)\s*[）)]|行\s*(\d+)\s*[~～-]\s*(\d+))")
_HEADING_RE = re.compile(r"^##\s+(.+?)(?:（行号：\s*(\d+)\s*[~～-]\s*(\d+)\s*）)?\s*$")

INDEX_LIB_DIR = v5config.get_config().lib_dir
logger = get_logger(__name__)

LOCK_NAME = ".build.lock"
# 长块按窗口拆分：窗口内按整行切割，窗口不足以容纳单行时该单行独占一块并记录 subchunk_index
SUBCHUNK_WINDOW = 4000


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class OllamaEmbedder:
    engine_name = "ollama"

    def __init__(
        self,
        model_name: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        retries: int | None = None,
        expected_dim: int | None = None,
    ):
        import requests
        cfg = v5config.get_config()
        self.requests = requests
        self.model_name = model_name or cfg.embedding_model
        self.base_url = (base_url or cfg.ollama_base_url).rstrip("/")
        self.timeout = timeout or cfg.request_timeout
        self.retries = retries if retries is not None else cfg.request_retries
        self.dim = self._probe_dimension(expected_dim)

    def _post_once(self, text: str, timeout: int) -> Any:
        last_error: Exception | None = None
        # /api/embed（新）优先，/api/embeddings（旧）兼容；input/prompt 双写兼容不同版本
        payloads = [
            {"model": self.model_name, "input": [text]},
            {"model": self.model_name, "prompt": text},
        ]
        endpoints = ("/api/embed", "/api/embeddings")
        for endpoint in endpoints:
            for payload in payloads:
                try:
                    resp = self.requests.post(self.base_url + endpoint, json=payload, timeout=timeout)
                    if resp.status_code == 404:
                        last_error = RuntimeError(f"{endpoint} 不存在(404)")
                        continue
                    resp.raise_for_status()
                    try:
                        data = resp.json()
                    except Exception as exc:
                        last_error = RuntimeError(f"坏 JSON 响应: {exc}")
                        continue
                    if isinstance(data, dict) and data.get("embeddings"):
                        return data["embeddings"][0]
                    if isinstance(data, dict) and data.get("embedding"):
                        return data["embedding"]
                    last_error = RuntimeError(f"响应无向量字段: {str(data)[:200]}")
                except RuntimeError as exc:
                    last_error = exc
                    continue
                except Exception as exc:
                    last_error = exc
                    continue
        raise RuntimeError(f"Ollama 未返回有效 embedding：{self.model_name}（{last_error}）")

    def _post_embedding(self, text: str, timeout: int | None = None) -> list[float]:
        timeout = timeout or self.timeout
        last: Exception | None = None
        for attempt in range(max(1, self.retries)):
            try:
                return self._post_once(text, timeout)
            except Exception as exc:
                last = exc
                if attempt + 1 < self.retries:
                    time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"Ollama embedding 失败（已重试{self.retries}次）: {last}")

    def _post_batch(self, texts: list[str], timeout: int | None = None) -> list[list[float]]:
        """一次请求编码多条（Ollama /api/embed 原生支持 input 数组）。

        实测批量比逐条快约 47 倍（0.90s/条 → 0.02s/条），且向量与逐条完全一致。
        只走 /api/embed：/api/embeddings（旧接口）不支持批量。
        """
        timeout = timeout or self.timeout
        last: Exception | None = None
        for attempt in range(max(1, self.retries)):
            try:
                resp = self.requests.post(
                    self.base_url + "/api/embed",
                    json={"model": self.model_name, "input": texts},
                    timeout=timeout,
                )
                if resp.status_code == 404:
                    raise RuntimeError("/api/embed 不存在(404)")
                resp.raise_for_status()
                data = resp.json()
                embs = data.get("embeddings") if isinstance(data, dict) else None
                if not embs or len(embs) != len(texts):
                    raise RuntimeError(
                        f"批量响应条数不符: 期望 {len(texts)}, 实得 {len(embs) if embs else 0}"
                    )
                return embs
            except Exception as exc:
                last = exc
                if attempt + 1 < self.retries:
                    time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"Ollama 批量 embedding 失败（已重试{self.retries}次）: {last}")

    def _probe_dimension(self, expected_dim: int | None = None) -> int:
        try:
            vector = self._post_embedding("探测", 10)
        except Exception as exc:
            raise RuntimeError(f"无法连接 Ollama ({self.base_url}): {exc}") from exc
        checked = assert_vector(vector, expected_dim, what="ollama probe")
        return len(checked)

    # 单次批量请求的文本条数上限（防御超长 payload；Ollama 按 token 动态批处理）
    EMBED_BATCH_LIMIT = 64

    def encode(self, texts: list[str], **kwargs) -> Any:
        """批量编码：优先走 /api/embed 原生批量，失败或不可用时回退逐条。

        任一条失败即抛错中止整批，禁止零向量填充掩盖故障。
        """
        import numpy as np
        cleaned = [(t or "")[:2500].strip() or "空" for t in texts]
        vectors: list[list[float]] = []
        if cleaned:
            try:
                for start in range(0, len(cleaned), self.EMBED_BATCH_LIMIT):
                    chunk = cleaned[start:start + self.EMBED_BATCH_LIMIT]
                    raw_list = self._post_batch(chunk, 120)
                    for j, raw in enumerate(raw_list):
                        vectors.append(
                            assert_vector(raw, self.dim, what=f"ollama[{start + j}]")
                        )
            except Exception as batch_exc:
                # 批量不可用（旧版 Ollama / 代理裁剪）→ 逐条回退，保证正确性优先
                logger.warning("Ollama 批量编码不可用，回退逐条：%s", batch_exc)
                vectors = []
                for i, text in enumerate(cleaned):
                    try:
                        raw = self._post_embedding(text, 30)
                        vectors.append(assert_vector(raw, self.dim, what=f"ollama[{i}]"))
                    except Exception as exc:
                        raise RuntimeError(
                            f"embedding 失败于第 {i} 条，本次构建已终止，旧索引未被替换：{exc}"
                        ) from exc
        arr = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1e-10
        return arr / norms


class TfidfEmbedder:
    """确定性的字符 n-gram 兼容回退引擎。

    正式离线词法检索由 semantic_search 的 LexicalIndex 负责，避免把哈希词频误称为 TF-IDF。
    仅允许离线测试模式使用（engine=test），禁止用于正式发布。
    """
    engine_name = "test"

    def __init__(self, dim: int = 512):
        self.dim = dim
        self.model_name = "local-hash-fallback"

    def encode(self, texts: list[str], **kwargs) -> Any:
        import numpy as np
        vectors = []
        for text in texts:
            vector = np.zeros(self.dim, dtype=np.float32)
            clean = text.strip().lower()
            tokens = []
            for i, ch in enumerate(clean):
                tokens.append(ch)
                if i + 2 <= len(clean):
                    tokens.append(clean[i:i + 2])
                if i + 3 <= len(clean):
                    tokens.append(clean[i:i + 3])
            for token in tokens or ["empty"]:
                idx = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % self.dim
                vector[idx] += 1.0
            norm = np.linalg.norm(vector)
            if norm:
                vector /= norm
            vectors.append(vector)
        return np.asarray(vectors, dtype=np.float32)


_ST_MODEL_CACHE = {}


def _ollama_is_local(base_url: str) -> bool:
    """判断 Ollama 地址是否为本机（仅本机才允许自动拉起服务）。"""
    try:
        host = (urllib.parse.urlparse(base_url).hostname or "").lower()
    except Exception:
        return False
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def ensure_ollama_ready(base_url: str, model_name: str, *, autostart: bool = True,
                        wait_seconds: float = 25.0) -> bool:
    """确保本机 Ollama 可用：未监听则自动拉起服务并等待就绪。

    背景：Ollama 不随系统自启，用户重启后检索会静默回退到 sentence-transformers，
    导致「建索引与检索用了不同模型」的维度冲突。此处主动自愈而非把责任推给用户。

    返回 True 表示最终可用。远程地址或关闭自启时只做探测、不启动进程。
    """
    def _alive() -> bool:
        try:
            with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=3):
                return True
        except Exception:
            return False


    if _alive():
        return True
    if not autostart or not _ollama_is_local(base_url):
        return False

    # 定位 ollama 可执行文件
    exe = shutil.which("ollama")
    if not exe:
        for cand in (
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
            r"C:\Program Files\Ollama\ollama.exe",
            "/usr/local/bin/ollama",
            "/usr/bin/ollama",
        ):
            if os.path.isfile(cand):
                exe = cand
                break
    if not exe:
        logger.warning("未找到 ollama 可执行文件，无法自动启动服务")
        return False

    try:
        logger.info("检测到本机 Ollama 未运行，正在自动启动服务...")
        creationflags = 0
        if os.name == "nt":
            # 脱离父进程，避免脚本退出时把服务一起带走
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            [exe, "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, creationflags=creationflags,
            start_new_session=(os.name != "nt"),
        )
    except Exception as exc:
        logger.warning("自动启动 Ollama 失败：%s", exc)
        return False

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(1.0)
        if _alive():
            logger.info("Ollama 已就绪（自动启动成功）")
            return True
    logger.warning("Ollama 自动启动后 %.0fs 内仍未就绪", wait_seconds)
    return False


def load_embedding_engine(model_name: str | None = None, prefer_ollama: bool = True,
                          autostart_ollama: bool = True):
    """按明确顺序加载 Ollama、sentence-transformers 或测试回退引擎。

    sentence-transformers 模型带进程级缓存：Web 服务每次搜索都调此函数，
    不缓存会导致每个请求重载 199 层权重（约 10s），表现为页面一直转圈。

    失败策略：Ollama 不可用时先尝试**自动拉起本机服务**（autostart_ollama），
    仍失败才切换到配置明确的 ST 模型（manifest 如实记录，维度变化触发重建）；
    ST 也失败时直接抛错；hash 回退仅在离线测试模式
    （INDEX_OFFLINE_TEST_VECTORS=1）下允许，且 manifest 标记 engine=test。
    """
    cfg = v5config.get_config()
    name = model_name or cfg.embedding_model
    if prefer_ollama:
        try:
            logger.info("尝试加载 Ollama embedding：%s", name)
            return OllamaEmbedder(model_name=name)
        except Exception as exc:
            logger.warning("Ollama 不可用：%s", exc)
        # 自愈：本机服务未启动时自动拉起，再试一次（避免静默回退到不同模型）
        if autostart_ollama and ensure_ollama_ready(cfg.ollama_base_url, name):
            try:
                return OllamaEmbedder(model_name=name)
            except Exception as exc:
                logger.warning("Ollama 自愈后仍不可用：%s", exc)
        if cfg.offline_test_vectors:
            logger.warning("离线测试模式：使用 hash-ngram 回退（engine=test，禁止发布）")
            return TfidfEmbedder()
    try:
        from sentence_transformers import SentenceTransformer
        clean_name = name if ":" not in name else DEFAULT_ST_MODEL
        if clean_name not in _ST_MODEL_CACHE:
            _ST_MODEL_CACHE[clean_name] = SentenceTransformer(clean_name)
        return _ST_MODEL_CACHE[clean_name]
    except Exception as exc:
        if cfg.offline_test_vectors:
            logger.warning("sentence-transformers 不可用，离线测试回退 hash-ngram：%s", exc)
            return TfidfEmbedder()
        raise RuntimeError(
            f"embedding 引擎加载失败，旧索引未被替换：Ollama 不可用且 sentence-transformers 加载失败（{exc}）。"
            "如需离线测试请设置 INDEX_OFFLINE_TEST_VECTORS=1（engine=test，禁止发布）。"
        ) from exc


def load_embedding_model(model_name: str):
    """v4 兼容别名。"""
    return load_embedding_engine(model_name, prefer_ollama=False)


def _extract_line_range(text: str, heading_match=None) -> tuple[int | None, int | None]:
    if heading_match and heading_match.group(2):
        return int(heading_match.group(2)), int(heading_match.group(3))
    match = _LINE_RANGE_RE.search(text)
    if not match:
        return None, None
    if match.group(1):
        return int(match.group(1)), int(match.group(2))
    return int(match.group(3)), int(match.group(4))


def _split_long_text(title: str, body: str) -> list[tuple[str, int]]:
    """长文本按整行窗口拆分，返回 [(文本, subchunk_index)]。

    不截断丢弃：超长内容拆成多块并记录 subchunk_index；overlap 按完整行携带
    由调用方在块边界处理，行号不漂移。
    """
    full = f"{title}\n{body.strip()}"
    if len(full) <= SUBCHUNK_WINDOW:
        return [(full[:12000], 0)] if len(full) > 12000 else [(full, 0)]
    lines = full.splitlines()
    out: list[tuple[str, int]] = []
    cur: list[str] = []
    cur_len = 0
    sub = 0
    for line in lines:
        # 单行超窗：独占一块
        if len(line) > SUBCHUNK_WINDOW:
            if cur:
                out.append(("\n".join(cur), sub))
                sub += 1
                cur, cur_len = [], 0
            for start in range(0, len(line), SUBCHUNK_WINDOW):
                out.append((line[start:start + SUBCHUNK_WINDOW], sub))
                sub += 1
            continue
        if cur_len + len(line) + 1 > SUBCHUNK_WINDOW and cur:
            out.append(("\n".join(cur), sub))
            sub += 1
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line) + 1
    if cur:
        out.append(("\n".join(cur), sub))
    return out


def parse_knowledge_blocks(index_path: str, lib_dir: str) -> list[dict[str, Any]]:
    """按索引卡中的二级标题拆出章节/文档块（行号从原文标题/行号标记提取，从 1 开始）。"""
    text = read_text_auto(index_path)
    meta, body = parse_frontmatter(text)
    title = meta.get("资料名称") or meta.get("书名") or meta.get("小说名称") or os.path.basename(os.path.dirname(index_path))
    author = meta.get("作者") or "佚名"
    segment_path = str(meta.get("源文件", "")).replace("\\", "/").strip()
    rel_index_path = os.path.relpath(index_path, lib_dir).replace("\\", "/")
    lines = body.splitlines()
    headings = [(i, m) for i, line in enumerate(lines) if (m := _HEADING_RE.match(line.strip()))]
    blocks: list[dict[str, Any]] = []
    if not headings:
        headings = [(0, None)]
    for pos, (start, heading) in enumerate(headings):
        end = headings[pos + 1][0] if pos + 1 < len(headings) else len(lines)
        section = "\n".join(lines[start:end]).strip()
        if not section:
            continue
        heading_name = heading.group(1).strip() if heading else title
        line_start, line_end = _extract_line_range(section, heading)
        if line_start is None:
            line_start, line_end = _extract_line_range(text)
        chapter_range = re.split(r"[（(]", heading_name, maxsplit=1)[0].strip()
        range_match = re.match(r"^(第[^\s·]+[章卷回节集]|(?:Chapter|Book|Volume|Episode|Part)\s*\d+)", chapter_range, re.I)
        if range_match:
            chapter_range = range_match.group(1)
        for sub_idx, (piece, subchunk) in enumerate(_split_long_text(heading_name, section)):
            blocks.append({
                "title": title,
                "novel": title,
                "author": author,
                "chapter_range": chapter_range,
                "chapter_title": heading_name,
                "segment_path": segment_path,
                "segment_line_start": line_start,
                "segment_line_end": line_end,
                "index_path": rel_index_path,
                "relative_path": rel_index_path,
                "subchunk_index": subchunk,
                "summary": section,
                "text": piece,
                "full_content": f"【{title} - {author}】\n{piece}",
                "text_hash": _text_hash(piece),
            })
    # chunk_id 唯一且可反查：<相对路径>#<块序号>
    for i, b in enumerate(blocks):
        b["chunk_id"] = f"{b['relative_path']}#block-{i:04d}"
    return blocks


def parse_chapter_blocks(index_path: str, lib_dir: str) -> list[dict[str, Any]]:
    return parse_knowledge_blocks(index_path, lib_dir)


def verify_block_text(block: dict[str, Any]) -> bool:
    """行号可验证：块文本能从声明的章节摘要中找到对应（防清洗吞行）。"""
    piece = block.get("text", "")
    summary = block.get("summary", "")
    if not piece:
        return False
    first_line = next((ln for ln in piece.splitlines() if ln.strip()), "")
    return bool(first_line) and first_line.strip() in summary


def _engine_metadata(model, model_name: str) -> tuple[str, str, int]:
    """提取 (engine, 实际模型名, 维度) 供 manifest 如实标注。

    sentence_transformers 的 SentenceTransformer 实例没有 model_name 属性，
    直接 getattr 回退会拿到**调用方传入的配置名**（可能是 Ollama 模型名），
    造成 manifest 标注不实。此处优先读实例自身的模型标识，读不到才回退。
    """
    engine = getattr(model, "engine_name", model.__class__.__name__.lower())
    name = getattr(model, "model_name", None)
    if not name:
        tok = getattr(model, "tokenizer", None)
        name = getattr(tok, "name_or_path", None)
    if not name:
        name = model_name
    dim = getattr(model, "dim", None)
    if dim is None and hasattr(model, "get_sentence_embedding_dimension"):
        dim = model.get_sentence_embedding_dimension()
    if dim is None and hasattr(model, "get_embedding_dimension"):
        dim = model.get_embedding_dimension()
    return engine, str(name), int(dim or 0)


def _acquire_lock(semantic_dir: str) -> str:
    os.makedirs(semantic_dir, exist_ok=True)
    lock_path = os.path.join(semantic_dir, LOCK_NAME)
    if os.path.exists(lock_path):
        raise RuntimeError(f"已有构建任务在运行（{lock_path}），本次构建拒绝启动以保护旧索引")
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"pid": os.getpid(), "started_at": int(time.time())}))
    return lock_path


def _release_lock(lock_path: str) -> None:
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except OSError:
        pass


def _gc_snapshots(semantic_dir: str, keep: set, keep_n: int = 2) -> None:
    """快照 GC：只删除 .semantic_index 内、不被保留 build 引用的快照文件。

    keep 为 build_id 集合；额外按 manifest 引用保护当前文件；不用宽泛通配符
    递归删除；失败只告警。
    """
    try:
        manifest_path = os.path.join(semantic_dir, MANIFEST_NAME)
        protected: set[str] = set()
        try:
            with open(manifest_path, encoding="utf-8") as f:
                m = json.load(f)
            for seg in m.get("segments", []) or []:
                if seg.get("npz"):
                    protected.add(str(seg["npz"]))
            if isinstance(m.get("lexical"), dict) and m["lexical"].get("npz"):
                protected.add(str(m["lexical"]["npz"]))
        except Exception:
            pass
        import re
        pat = re.compile(r"^(knowledge_vectors|lexical_index)\.([0-9a-f]{6,})\.npz$")
        cands: list[tuple[str, str]] = []
        for name in os.listdir(semantic_dir):
            mt = pat.match(name)
            if not mt:
                continue
            if name in protected:
                continue
            if mt.group(2) in {k for k in keep if k}:
                continue
            cands.append((mt.group(2), name))
        # 按 build 名排序，只删超出保留数的最旧文件（文件名含 build_id，保守起见全删非保留）
        for _, name in sorted(cands):
            full = os.path.join(semantic_dir, name)
            if os.path.commonpath([os.path.abspath(full), os.path.abspath(semantic_dir)]) != os.path.abspath(semantic_dir):
                continue
            try:
                os.remove(full)
            except OSError as exc:
                logger.warning("快照 GC 删除失败 %s：%s", name, exc)
    except Exception as exc:
        logger.warning("快照 GC 失败（不影响发布）：%s", exc)


def _publish_empty_index(semantic_dir, source_hashes, old_manifest) -> None:
    """全删清空：发布空索引，保证 manifest/vectors 同步清空，而非静默返回。"""
    import numpy as np
    import shutil
    dim = int(old_manifest.get("dimension", 0)) if old_manifest else 0
    engine = str(old_manifest.get("engine", "unknown")) if old_manifest else "unknown"
    model = str(old_manifest.get("model", "")) if old_manifest else ""
    build_id = uuid.uuid4().hex[:12]
    from semantic_search import LEXICAL_SCHEMA_VERSION, lexical_snapshot_name, vector_snapshot_name
    vec_name = vector_snapshot_name(build_id)
    lex_name = lexical_snapshot_name(build_id)
    manifest = {
        "schema_version": INDEX_VERSION,
        "version": INDEX_VERSION,
        "builder_version": BUILDER_VERSION,
        "build_id": build_id,
        "engine": engine,
        "model": model,
        "dimension": dim,
        "normalization": "l2",
        "chunk_strategy": CHUNK_STRATEGY,
        "chunk_count": 0,
        "vector_count": 0,
        "created_at": int(time.time()),
        "source_hashes": {},
        "segments": [{"npz": vec_name, "count": 0}],
        "lexical": {"npz": lex_name, "schema_version": LEXICAL_SCHEMA_VERSION, "document_count": 0},
    }
    staging = f"{semantic_dir}.staging.{os.getpid()}.{build_id}"
    os.makedirs(staging, exist_ok=True)
    try:
        np.savez_compressed(
            os.path.join(staging, vec_name),
            embeddings=np.zeros((0, dim if dim else 1), dtype=np.float32),
            chunks=json.dumps([], ensure_ascii=False),
        )
        from semantic_search import LexicalIndex, save_lexical_snapshot
        save_lexical_snapshot(LexicalIndex([], build_id), os.path.join(staging, lex_name))
        os.replace(os.path.join(staging, vec_name), os.path.join(semantic_dir, vec_name))
        os.replace(os.path.join(staging, lex_name), os.path.join(semantic_dir, lex_name))
        manifest_path = os.path.join(semantic_dir, MANIFEST_NAME)
        tmp_manifest = manifest_path + f".tmp.{build_id}"
        with open(tmp_manifest, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        os.replace(tmp_manifest, manifest_path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _load_old_state(semantic_dir: str) -> tuple[dict, list[dict], Any | None]:
    """读取旧 manifest/chunks/vectors（损坏返回空状态，不抛错）。"""
    import numpy as np
    manifest_path = os.path.join(semantic_dir, MANIFEST_NAME)
    if not os.path.exists(manifest_path):
        return {}, [], None
    try:
        with open(manifest_path, encoding="utf-8") as f:
            old_manifest = json.load(f)
    except Exception:
        return {}, [], None
    chunks: list[dict] = []
    vectors = None
    try:
        from semantic_search import _npz_str
        parts: list[Any] = []
        for entry in old_manifest.get("segments", []):
            npz_path = os.path.join(semantic_dir, entry.get("npz", ""))
            if not os.path.exists(npz_path):
                return {}, [], None
            # Windows 下必须关闭 npz 句柄，否则后续原子替换报 WinError 32
            with np.load(npz_path, allow_pickle=False) as data:
                parts.append(np.asarray(data["embeddings"], dtype=np.float32))
                chunks.extend(json.loads(_npz_str(data["chunks"])))
        if not parts or sum(len(p) for p in parts) != len(chunks):
            return {}, [], None
        vectors = parts[0] if len(parts) == 1 else np.vstack(parts)
    except Exception:
        return {}, [], None
    return old_manifest, chunks, vectors


def build_semantic_index(
    lib_dir: str,
    model=None,
    model_name: str | None = None,
    *,
    force: bool = False,
    batch_size: int = 32,
    prefer_ollama: bool = True,
    source: str | None = None,
) -> int:
    """构建语义索引，返回成功写入的知识块数量。

    兼容旧调用：build_semantic_index(lib, model, model_name, force=True)。
    source：只重建该来源（匹配 index_path 前缀），其他来源复用旧向量。
    """
    cfg = v5config.get_config()
    resolved_model = model_name or DEFAULT_OLLAMA_MODEL
    semantic_dir = os.path.join(lib_dir, INDEX_DIR_NAME)
    os.makedirs(semantic_dir, exist_ok=True)
    segment_index_dir = os.path.join(lib_dir, "段索引")
    if not os.path.exists(segment_index_dir):
        segment_index_dir = os.path.join(lib_dir, "章节索引")
    if not os.path.isdir(segment_index_dir):
        logger.error("未找到段索引目录：%s", segment_index_dir)
        return 1
    index_files = sorted(
        os.path.join(root, name)
        for root, _, names in os.walk(segment_index_dir)
        for name in names
        if name.endswith(".md")
    )
    if source:
        index_files = [p for p in index_files if source in os.path.relpath(p, lib_dir).replace("\\", "/")]
        if not index_files:
            logger.info("来源 %s 下没有段索引文件，其他来源保持不变", source)
            return 0
    if not index_files:
        logger.info("没有发现段索引 Markdown 文件")
        return 0

    lock_path = _acquire_lock(semantic_dir)
    try:
        return _build_inner(
            lib_dir, semantic_dir, index_files, model, resolved_model,
            force=force, batch_size=batch_size, prefer_ollama=prefer_ollama,
            source=source, cfg=cfg,
        )
    finally:
        _release_lock(lock_path)


def _build_inner(lib_dir, semantic_dir, index_files, model, model_name, *, force, batch_size, prefer_ollama, source, cfg) -> int:
    import numpy as np
    chunks: list[dict[str, Any]] = []
    source_hashes: dict[str, str] = {}
    for path in index_files:
        blocks = parse_knowledge_blocks(path, lib_dir)
        chunks.extend(blocks)
        source_hashes[os.path.relpath(path, lib_dir).replace("\\", "/")] = _sha256_file(path)
    if not source:
        # 无 source 过滤：当前磁盘文件集合即全量；删除文件同步从 manifest/索引/向量移除
        current_rels = set(source_hashes.keys())
    else:
        current_rels = None  # source 局部重建由 keep 逻辑处理删除
    if not chunks:
        # 全删清空：发布空索引，保证 manifest/vectors 同步清空，而非静默返回
        old_manifest_e, _, _ = _load_old_state(semantic_dir)
        _publish_empty_index(semantic_dir, source_hashes, old_manifest_e)
        logger.info("全部文件已删除，索引已清空")
        return 0

    old_manifest, old_chunks, old_vectors = _load_old_state(semantic_dir)
    # 删除同步：旧 manifest 有、当前磁盘无的文件，其 chunk 不得进入新索引
    if current_rels is not None and old_manifest.get("source_hashes"):
        deleted = set(old_manifest["source_hashes"].keys()) - current_rels
        if deleted:
            logger.info("检测到删除文件 %d 个，将同步移除其向量", len(deleted))
            chunks = [b for b in chunks if b["relative_path"] in current_rels]
            source_hashes = {k: v for k, v in source_hashes.items() if k in current_rels}
            if not chunks:
                _publish_empty_index(semantic_dir, source_hashes, old_manifest)
                logger.info("全部文件已删除，索引已清空")
                return 0
    old_by_id = {c.get("chunk_id"): (c, i) for i, c in enumerate(old_chunks) if c.get("chunk_id")}

    if model is None:
        model = load_embedding_engine(model_name, prefer_ollama=prefer_ollama)
    engine, actual_model, declared_dim = _engine_metadata(model, model_name)
    if engine == "test" and not cfg.offline_test_vectors:
        raise RuntimeError("拒绝用测试引擎（engine=test）发布正式索引")

    # 模型/维度/切块版本变化：不得复用旧向量，必须全量重建。
    if old_manifest and not force:
        model_changed = old_manifest.get("model") != actual_model and old_manifest.get("model") != model_name
        strategy_changed = old_manifest.get("chunk_strategy", "knowledge-block-v1") != CHUNK_STRATEGY
        if model_changed or strategy_changed:
            logger.warning("模型/切块策略变化（%s -> %s），自动触发全量重建", old_manifest.get("model"), model_name)
            force = True

    # 增量：未变化文件复用旧向量
    texts: list[str] = []
    to_embed_idx: list[int] = []
    reused: dict[int, Any] = {}
    if not force and old_manifest and old_manifest.get("source_hashes"):
        old_hashes = old_manifest.get("source_hashes", {})
        for i, b in enumerate(chunks):
            rel = b["relative_path"]
            old_h = old_hashes.get(rel)
            new_h = source_hashes.get(rel)
            old_entry = old_by_id.get(b["chunk_id"])
            if old_h is not None and old_h == new_h and old_entry is not None and old_vectors is not None:
                old_c, old_i = old_entry
                if old_c.get("text_hash") == b.get("text_hash") and old_i < len(old_vectors):
                    reused[i] = old_vectors[old_i]
                    continue
            texts.append(b["full_content"])
            to_embed_idx.append(i)
    else:
        texts = [b["full_content"] for b in chunks]
        to_embed_idx = list(range(len(chunks)))

    # source 局部重建：其他来源直接复用旧块+旧向量。
    # 注意：keep 里的块不得再参与增量复用/编码（chunks 仅为本次 source 范围），
    # 否则 keep_vecs 与新编码向量会重复拼接导致数量翻倍。
    if source and not force and old_chunks and old_vectors is not None:
        keep_chunks = [c for c in old_chunks if source not in str(c.get("relative_path", ""))]
        keep_vecs = [old_vectors[i] for i, c in enumerate(old_chunks) if source not in str(c.get("relative_path", ""))]
        keep_ids = {c.get("chunk_id") for c in keep_chunks}
        # 本次 chunks 只保留 source 范围（source 过滤已保证），并剔除 keep 残留
        chunks = [b for b in chunks if b.get("chunk_id") not in keep_ids]
        source_hashes = {k: v for k, v in source_hashes.items() if source in k}
        # 复用判定重算：仅针对本次 chunks
        texts, to_embed_idx, reused = [], [], {}
        if old_manifest and old_manifest.get("source_hashes"):
            old_hashes = old_manifest.get("source_hashes", {})
            for i, b in enumerate(chunks):
                rel = b["relative_path"]
                old_h = old_hashes.get(rel)
                new_h = source_hashes.get(rel)
                old_entry = old_by_id.get(b["chunk_id"])
                if old_h is not None and old_h == new_h and old_entry is not None and old_vectors is not None:
                    old_c, old_i = old_entry
                    if old_c.get("text_hash") == b.get("text_hash") and old_i < len(old_vectors):
                        reused[i] = old_vectors[old_i]
                        continue
                texts.append(b["full_content"])
                to_embed_idx.append(i)
        else:
            texts = [b["full_content"] for b in chunks]
            to_embed_idx = list(range(len(chunks)))
        logger.info("局部重建 %s：保留其他来源 %d 块，本次 %d 块", source, len(keep_chunks), len(chunks))
    else:
        keep_chunks, keep_vecs = [], []

    new_embeddings = None
    if texts:
        encoded = model.encode(texts, batch_size=batch_size, normalize_embeddings=True)
        new_embeddings = np.asarray(encoded, dtype=np.float32)
        if new_embeddings.ndim != 2 or len(new_embeddings) != len(texts):
            raise RuntimeError(f"embedding 形状无效：{new_embeddings.shape}，待编码={len(texts)}（旧索引未被覆盖）")
        if not bool(np.isfinite(new_embeddings).all()):
            raise RuntimeError("embedding 含非有限值（NaN/Inf），本次构建已终止，旧索引未被覆盖")
        dim = int(new_embeddings.shape[1])
        if declared_dim and dim != declared_dim:
            raise RuntimeError("模型声明维度与实际向量维度不一致（旧索引未被覆盖）")
    else:
        dim = int(old_manifest.get("dimension", 0)) if old_manifest else 0

    # 组装全量向量（复用 + 新编码），顺序与 chunks 对齐。
    # 注意：keep 逻辑只在 source 局部重建时生效；无 source 的删除同步已在上游
    # 把已删文件的 chunk 剔除，chunks 即当前磁盘全量，不得再拼回 old_chunks。
    if keep_chunks:
        full_chunks = keep_chunks + chunks
        if len(keep_vecs):
            keep_arr = np.asarray(keep_vecs, dtype=np.float32)
        else:
            keep_arr = np.zeros((0, dim), dtype=np.float32)
        if texts:
            assert new_embeddings is not None
            # 新 chunks 的向量：复用与新编码交织
            assembled = np.zeros((len(chunks), dim), dtype=np.float32)
            new_ptr = 0
            for i in range(len(chunks)):
                if i in reused:
                    assembled[i] = reused[i]
                else:
                    assembled[i] = new_embeddings[new_ptr]
                    new_ptr += 1
            full_vectors = np.vstack([keep_arr, assembled]) if len(keep_arr) else assembled
        else:
            # texts 为空：本次 source 范围全部命中复用，拼上 keep 向量
            order = [reused[i] for i in range(len(chunks))]
            assembled = np.asarray(order, dtype=np.float32)
            full_vectors = np.vstack([keep_arr, assembled]) if len(keep_arr) else assembled
        chunks = full_chunks
    else:
        if texts:
            assert new_embeddings is not None
            assembled = np.zeros((len(chunks), dim), dtype=np.float32)
            new_ptr = 0
            for i in range(len(chunks)):
                if i in reused:
                    assembled[i] = reused[i]
                else:
                    assembled[i] = new_embeddings[new_ptr]
                    new_ptr += 1
            full_vectors = assembled
        else:
            # 无新编码内容：全部当前块都命中复用（reused 与 chunks 等长），
            # 直接按 chunks 顺序组装复用向量。删除同步已在上游剔除已删文件，
            # 此处不得用 old_chunks 回填。
            if len(reused) == len(chunks) and old_vectors is not None:
                order = [reused[i] for i in range(len(chunks))]
                full_vectors = np.asarray(order, dtype=np.float32)
            elif current_rels is not None and old_manifest.get("source_hashes"):
                if set(source_hashes.keys()) == set(old_manifest["source_hashes"].keys()) \
                        and old_vectors is not None and len(old_vectors) == len(chunks):
                    full_vectors = np.asarray(old_vectors, dtype=np.float32)
                else:
                    raise RuntimeError("增量状态不一致，拒绝用旧向量回填（旧索引未被覆盖）")
            else:
                full_vectors = np.asarray(old_vectors, dtype=np.float32) if old_vectors is not None else np.zeros((0, dim), dtype=np.float32)
                chunks = old_chunks or chunks

    if len(full_vectors) != len(chunks):
        raise RuntimeError(f"向量与知识块数量不一致：{len(full_vectors)} vs {len(chunks)}（旧索引未被覆盖）")
    if full_vectors.size and not bool(np.isfinite(full_vectors).all()):
        raise RuntimeError("最终向量含非有限值（旧索引未被覆盖）")
    # chunk_id 唯一性
    ids = [c.get("chunk_id") for c in chunks]
    if len(ids) != len(set(ids)):
        raise RuntimeError("chunk_id 不唯一，拒绝发布（旧索引未被覆盖）")

    build_id = uuid.uuid4().hex[:12]
    vec_name = f"knowledge_vectors.{build_id}.npz"
    lex_name = f"lexical_index.{build_id}.npz"
    manifest = {
        "schema_version": INDEX_VERSION,
        "version": INDEX_VERSION,
        "builder_version": BUILDER_VERSION,
        "build_id": build_id,
        "engine": engine,
        "model": actual_model,
        "dimension": int(full_vectors.shape[1]) if full_vectors.size else dim,
        "normalization": "l2",
        "chunk_strategy": CHUNK_STRATEGY,
        "chunk_count": len(chunks),
        "vector_count": int(len(chunks)),
        "created_at": int(time.time()),
        "source_hashes": source_hashes if not (source and old_manifest) else {**(old_manifest.get("source_hashes", {})), **source_hashes},
        "segments": [{"npz": vec_name, "count": len(chunks)}],
        "lexical": {"npz": lex_name, "schema_version": "lexical-v1", "document_count": len(chunks)},
    }
    if manifest["chunk_count"] != manifest["vector_count"]:
        raise RuntimeError("manifest 数量不一致，拒绝发布")

    # 非 force 且全未变化：跳过
    if not force and old_manifest.get("source_hashes") == manifest["source_hashes"] \
            and old_manifest.get("engine") == engine \
            and old_manifest.get("dimension") == manifest["dimension"] \
            and not source:
        logger.info("语义索引已是最新，跳过构建")
        return int(old_manifest.get("chunk_count", len(chunks)))

    # staging：写不可变 build_id 文件，校验后移动到正式目录，最后原子切换 manifest。
    # 任意一步崩溃：旧 manifest 仍引用完整旧文件，不存在固定文件名混合状态。
    import shutil
    keep_builds = 2
    staging = f"{semantic_dir}.staging.{os.getpid()}.{build_id}"
    os.makedirs(staging, exist_ok=True)
    try:
        from semantic_search import (
            LEXICAL_SCHEMA_VERSION,
            LexicalIndex,
            get_lexical_index,
            save_lexical_snapshot,
            vector_snapshot_name,
            lexical_snapshot_name,
        )
        vec_name = vector_snapshot_name(build_id)
        lex_name = lexical_snapshot_name(build_id)
        staging_vec = os.path.join(staging, vec_name)
        staging_lex = os.path.join(staging, lex_name)
        np.savez_compressed(
            staging_vec,
            embeddings=np.asarray(full_vectors, dtype=np.float32),
            chunks=json.dumps(chunks, ensure_ascii=False),
        )
        # 词法快照与向量同一 build_id 一起发布
        lex_index = LexicalIndex(chunks, build_id)
        lex_sha = save_lexical_snapshot(lex_index, staging_lex)
        # 回读校验（Windows 下必须 close 句柄后才能移动）
        with np.load(staging_vec, allow_pickle=False) as data:
            if data["embeddings"].shape[0] != len(chunks):
                raise RuntimeError("staging 向量回读数量不一致")
            if not bool(np.isfinite(data["embeddings"]).all()):
                raise RuntimeError("staging 向量回读含非有限值")
        lex_check = LexicalIndex.load_file(staging_lex, expected_build_id=build_id)
        if lex_check.document_count != len(chunks):
            raise RuntimeError("staging 词法回读数量不一致")
        # sha256
        vec_sha = _sha256_file(staging_vec)
        manifest["segments"] = [{"npz": vec_name, "count": len(chunks), "sha256": vec_sha}]
        manifest["lexical"] = {"npz": lex_name, "schema_version": LEXICAL_SCHEMA_VERSION,
                               "document_count": len(chunks), "sha256": lex_sha}
        # 移动不可变文件到正式目录（禁止覆盖 manifest 当前引用的旧文件：新文件名唯一）
        os.replace(staging_vec, os.path.join(semantic_dir, vec_name))
        os.replace(staging_lex, os.path.join(semantic_dir, lex_name))
        manifest_path = os.path.join(semantic_dir, MANIFEST_NAME)
        tmp_manifest = manifest_path + f".tmp.{build_id}"
        with open(tmp_manifest, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        os.replace(tmp_manifest, manifest_path)
        # GC：只保留当前与上一个 build 引用的文件
        _gc_snapshots(semantic_dir, keep={build_id, old_manifest.get("build_id")}, keep_n=keep_builds)
        try:
            _LEX_PROCESS_CACHE = get_lexical_index
            _LEX_PROCESS_CACHE(chunks, build_id)
        except Exception:
            pass
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    logger.info("语义索引构建完成：%d 个知识块，%s/%s/%d 维 build=%s", len(chunks), engine, actual_model, manifest["dimension"], build_id)
    return len(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库语义索引构建工具 v5.0")
    parser.add_argument("--lib", default=None)
    parser.add_argument("--build", action="store_true", help="显式触发构建")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--source", default=None, help="只重建该来源，其他来源复用")
    parser.add_argument("--ollama-url", default=None, help="Ollama 地址（默认环境变量/配置）")
    add_log_format_argument(parser)
    args = parser.parse_args()
    configure_logging(fmt=args.log_format)
    cfg = v5config.get_config({
        "lib_dir": args.lib,
        "embedding_model": args.model,
        "ollama_base_url": args.ollama_url,
    })
    enabled = cfg.semantic_enabled
    if not args.build and not enabled:
        logger.info("语义索引默认关闭；设置 INDEX_ENABLE_SEMANTIC=1 或传入 --build")
        return 0
    try:
        build_semantic_index(cfg.lib_dir, model_name=cfg.embedding_model, force=args.force, source=args.source)
        return 0
    except Exception as exc:
        logger.error("语义索引构建失败：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
