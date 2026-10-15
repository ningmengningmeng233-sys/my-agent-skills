# -*- coding: utf-8 -*-
"""v5 统一配置入口（单源真相）。

优先级：命令行参数 > 环境变量 > .env/配置文件 > 安全默认值。
所有脚本（semantic_index / semantic_search / check_env / start_web）
必须从本模块读取 Ollama 地址、模型名、维度、索引版本与切块策略，
禁止各自维护独立默认值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 硬件自适配结果缓存（进程级；避免每次 get_config 都调用 nvidia-smi）
_ADAPTIVE_MODEL_CACHE: str | None = None

OLLAMA_HOST_ENV = "OLLAMA_HOST"
OLLAMA_MODEL_ENV = "INDEX_EMBEDDING_MODEL"
EMBEDDING_DIM_ENV = "INDEX_EMBEDDING_DIM"
LIB_DIR_ENV = "INDEX_LIB_DIR"
SEMANTIC_ENABLE_ENV = "INDEX_ENABLE_SEMANTIC"
OFFLINE_TEST_ENV = "INDEX_OFFLINE_TEST_VECTORS"

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen3-embedding:8b"
DEFAULT_ST_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
INDEX_DIR_NAME = ".semantic_index"
MANIFEST_NAME = "manifest.json"
# 应用版本（Web/SKILL/pyproject 显示用，UI 热修只 bump 此处，不触发重建）
APP_VERSION = "5.4.7"
# 索引格式版本（manifest schema；只有模型/维度/切块/索引格式变化才重建）
INDEX_VERSION = "5.4.0"
CHUNK_STRATEGY = "knowledge-block-v1"
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 5888


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(SKILL_DIR, ".env"))
    except Exception:
        pass


_load_dotenv()


@dataclass(frozen=True)
class V5Config:
    lib_dir: str
    ollama_base_url: str
    embedding_model: str
    embedding_dim: int | None
    chunk_strategy: str
    index_version: str
    request_timeout: int
    request_retries: int
    web_host: str
    web_port: int
    semantic_enabled: bool
    offline_test_vectors: bool


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


def resolve_adaptive_model(base_url: str) -> str:
    """按本机硬件自适配向量模型（结果进程内缓存，避免每次调用都探测）。

    仅当 .env 未显式指定 INDEX_EMBEDDING_MODEL 时生效；用户显式配置优先。
    """
    global _ADAPTIVE_MODEL_CACHE
    if _ADAPTIVE_MODEL_CACHE is not None:
        return _ADAPTIVE_MODEL_CACHE
    try:
        import hardware_profile
        model, reason = hardware_profile.resolve_embedding_model(base_url)
        import logging
        logging.getLogger(__name__).info("硬件自适配向量模型: %s（%s）", model, reason)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("硬件自适配失败，用默认模型：%s", exc)
        model = DEFAULT_OLLAMA_MODEL
    _ADAPTIVE_MODEL_CACHE = model
    return model


def get_config(overrides: dict[str, Any] | None = None) -> V5Config:
    ov = overrides or {}
    lib_dir = (
        ov.get("lib_dir")
        or os.environ.get(LIB_DIR_ENV, "").strip()
        or os.path.join(SKILL_DIR, "索引库")
    )
    base_url = (
        ov.get("ollama_base_url")
        or os.environ.get(OLLAMA_HOST_ENV, "").strip()
        or DEFAULT_OLLAMA_BASE_URL
    ).rstrip("/")
    explicit_model = (
        ov.get("embedding_model")
        or os.environ.get(OLLAMA_MODEL_ENV, "").strip()
    )
    # 显式配置优先；未配置则按本机硬件自适配（见 scripts/hardware_profile.py）
    model = explicit_model or resolve_adaptive_model(base_url)
    dim_raw = ov.get("embedding_dim", os.environ.get(EMBEDDING_DIM_ENV, "").strip())
    try:
        dim = int(dim_raw) if str(dim_raw).strip() else None
    except (TypeError, ValueError):
        dim = None
    return V5Config(
        lib_dir=lib_dir,
        ollama_base_url=base_url,
        embedding_model=model,
        embedding_dim=dim,
        chunk_strategy=CHUNK_STRATEGY,
        index_version=INDEX_VERSION,
        request_timeout=int(ov.get("request_timeout", _int_env("INDEX_REQUEST_TIMEOUT", 30))),
        request_retries=int(ov.get("request_retries", _int_env("INDEX_REQUEST_RETRIES", 3))),
        web_host=str(ov.get("web_host", os.environ.get("INDEX_WEB_HOST", DEFAULT_WEB_HOST))),
        web_port=int(ov.get("web_port", _int_env("INDEX_WEB_PORT", DEFAULT_WEB_PORT))),
        semantic_enabled=bool(ov.get("semantic_enabled", os.environ.get(SEMANTIC_ENABLE_ENV, "").strip() == "1")),
        offline_test_vectors=bool(ov.get("offline_test_vectors", os.environ.get(OFFLINE_TEST_ENV, "").strip() == "1")),
    )


def match_model(wanted: str, available: list[str]) -> str | None:
    """模型匹配：wanted 含 tag（如 :8b）必须精确匹配完整名称；
    不含 tag 时才允许匹配同名模型的任意 tag。返回命中的完整名或 None。"""
    wanted = (wanted or "").strip()
    if not wanted:
        return None
    names = [str(m) for m in (available or [])]
    if ":" in wanted:
        for m in names:
            if m == wanted or m.split("@")[0] == wanted:
                return m
        return None
    for m in names:
        base = m.split("@")[0]
        if base == wanted or base.split(":")[0] == wanted:
            return m
    return None


def probe_ollama(base_url: str, model: str, timeout: int = 10, embed_timeout: int = 60) -> dict[str, Any]:
    """探测 Ollama 服务在线、模型存在、实际向量维度（真实 embed 探测）。"""
    import requests
    result: dict[str, Any] = {"online": False, "models": [], "model_found": False, "dimension": None, "error": ""}
    try:
        resp = requests.get(base_url.rstrip("/") + "/api/tags", timeout=timeout)
        resp.raise_for_status()
        models = [m.get("name", "") for m in resp.json().get("models", [])]
        result["models"] = models
        result["online"] = True
        hit = match_model(model, models)
        result["model_found"] = hit is not None
        result["matched_model"] = hit
        if hit is None:
            result["error"] = f"模型未安装: {model}"
            return result
        # 真实 embed 探测维度（/api/embed 优先，/api/embeddings 兼容）
        last_err = ""
        for endpoint, payload in (
            ("/api/embed", {"model": hit, "input": ["维度探测"]}),
            ("/api/embeddings", {"model": hit, "prompt": "维度探测"}),
        ):
            try:
                r = requests.post(base_url.rstrip("/") + endpoint, json=payload, timeout=embed_timeout)
                if r.status_code == 404:
                    last_err = f"{endpoint} 404"
                    continue
                r.raise_for_status()
                data = r.json()
                vec = None
                if isinstance(data, dict) and data.get("embeddings"):
                    vec = data["embeddings"][0]
                elif isinstance(data, dict) and data.get("embedding"):
                    vec = data["embedding"]
                if vec:
                    result["dimension"] = len(vec)
                    return result
                last_err = "响应无向量字段"
            except Exception as exc:
                last_err = str(exc)
        result["error"] = f"维度探测失败: {last_err}"
    except Exception as exc:
        result["error"] = str(exc)
    return result


def assert_vector(vector: Any, expected_dimension: int | None, *, what: str = "embedding") -> list[float]:
    """校验 embedding 响应：类型/非空/维度/有限值，不通过直接抛错。"""
    import numpy as np
    if not isinstance(vector, (list, tuple)) or not vector:
        raise RuntimeError(f"{what} response has no vector")
    array = np.asarray(vector, dtype=np.float32)
    if array.ndim != 1 or array.size == 0:
        raise RuntimeError(f"{what} is not a 1-D vector: shape={array.shape}")
    if expected_dimension and int(array.size) != int(expected_dimension):
        raise RuntimeError(f"{what} dimension mismatch: got {array.size}, expected {expected_dimension}")
    if not bool(np.isfinite(array).all()):
        raise RuntimeError(f"{what} contains non-finite values")
    return [float(v) for v in array.tolist()]
