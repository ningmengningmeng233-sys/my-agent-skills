import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(ROOT, "scripts")

sys.path.insert(0, SCRIPTS)

semantic_index = pytest.importorskip("semantic_index", reason="semantic_index 模块未找到")
semantic_search = pytest.importorskip("semantic_search", reason="semantic_search 模块未找到")

numpy = pytest.importorskip("numpy", reason="numpy 未安装")


class FakeModel:
    def __init__(self, dim: int = 8):
        self._dim = dim

    def get_sentence_embedding_dimension(self) -> int:
        return self._dim

    def encode(self, texts, *, normalize_embeddings: bool = True, **kwargs):
        rng = numpy.random.default_rng(42)
        emb = rng.random((len(texts), self._dim)).astype(numpy.float32)
        if normalize_embeddings:
            norms = numpy.linalg.norm(emb, axis=1, keepdims=True)
            emb = emb / numpy.where(norms == 0, 1, norms)
        return emb


def test_parse_chapter_blocks_reads_sample_index(tmp_lib, sample_index):
    blocks = semantic_index.parse_chapter_blocks(sample_index, tmp_lib)
    assert len(blocks) == 2
    assert blocks[0]["chapter_range"] == "第1章"
    assert blocks[0]["segment_line_start"] == 1
    assert blocks[0]["segment_line_end"] == 2
    assert blocks[1]["chapter_range"] == "第2章"


def test_build_semantic_index_creates_manifest_and_npz(tmp_lib, sample_index):
    model = FakeModel(dim=8)
    count = semantic_index.build_semantic_index(
        tmp_lib,
        model,
        "fake-model",
        force=True,
        batch_size=2,
    )
    assert count == 2

    semantic_dir = os.path.join(tmp_lib, semantic_index.INDEX_DIR_NAME)
    manifest_path = os.path.join(semantic_dir, semantic_index.MANIFEST_NAME)
    assert os.path.exists(manifest_path)
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest["model"] == "fake-model"
    assert manifest["chunk_count"] == 2

    npz_name = manifest["segments"][0]["npz"]
    assert os.path.exists(os.path.join(semantic_dir, npz_name))


def test_search_returns_top_k(tmp_path):
    chunks = [
        {"novel": "A", "chapter_range": "第1章", "summary": "one"},
        {"novel": "A", "chapter_range": "第2章", "summary": "two"},
        {"novel": "A", "chapter_range": "第3章", "summary": "three"},
    ]
    embeddings = numpy.array([
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ], dtype=numpy.float32)
    model = FakeModel(dim=3)
    model.encode = lambda texts, **kwargs: numpy.array([[1.0, 0.0, 0.0]], dtype=numpy.float32)

    results = semantic_search.search("query", model, embeddings, chunks, top_k=2, threshold=0.0)
    assert len(results) == 2
    assert results[0]["chapter_range"] == "第1章"


def test_search_cli_without_index_reports_error(tmp_lib, monkeypatch):
    monkeypatch.setenv("INDEX_ENABLE_SEMANTIC", "1")

    class PassModel:
        def encode(self, texts, **kwargs):
            return numpy.array([[1.0, 0.0]], dtype=numpy.float32)

    monkeypatch.setattr(semantic_search, "load_embedding_model", lambda _: PassModel())

    old_argv = sys.argv
    sys.argv = [semantic_search.__file__, "anything", "--lib", tmp_lib]
    try:
        rc = semantic_search.main()
    finally:
        sys.argv = old_argv
    assert rc == 1


def test_index_cli_default_disabled(tmp_lib, monkeypatch):
    monkeypatch.delenv("INDEX_ENABLE_SEMANTIC", raising=False)
    monkeypatch.setattr(
        semantic_index,
        "build_semantic_index",
        lambda *args, **kwargs: pytest.fail("should not build when disabled"),
    )
    # Prevent argparse from reading sys.argv by invoking main with explicit empty argv.
    import sys as _sys
    old_argv = _sys.argv
    _sys.argv = [semantic_index.__file__, "--lib", tmp_lib]
    try:
        rc = semantic_index.main()
    finally:
        _sys.argv = old_argv
    assert rc == 0
