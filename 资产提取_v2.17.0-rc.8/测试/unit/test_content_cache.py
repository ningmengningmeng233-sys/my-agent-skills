#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 8：内容寻址缓存。

覆盖：
- 只复用「完整成功」结果：失败/部分输出/审核不通过/结构版本不兼容一律不命中；
- 完全相同输入第二次运行不重复请求模型（完成条件）；
- 写入原子化；并发写同一键有锁语义；
- `inspect` / `invalidate` / `prune`，且**默认不自动删除用户产物**。

模块按 pytest fixture 惰性导入：红测阶段模块尚未实现时只让本文件红，不中断整个回归。
"""
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="module")
def cc():
    from scripts.engine import content_cache as module

    return module


# --- 命中与不命中 --------------------------------------------------------------

def test_successful_result_is_reused(cc, tmp_path):
    c = cc.ContentCache(tmp_path)
    c.put("prompt:甲", "sha256:fp1", b"hello")
    assert c.get("prompt:甲", "sha256:fp1") == b"hello"
    # 相同输入第二次取用不上报新写（完成条件：不重复请求模型）
    again = c.put("prompt:甲", "sha256:fp1", b"hello")
    assert again.reused is True


def test_fingerprint_change_misses(cc, tmp_path):
    c = cc.ContentCache(tmp_path)
    c.put("prompt:甲", "sha256:fp1", b"hello")
    assert c.get("prompt:甲", "sha256:fp2") is None


def test_failed_partial_and_rejected_results_never_hit(cc, tmp_path):
    c = cc.ContentCache(tmp_path)
    for status in (cc.STATUS_FAILED, cc.STATUS_PARTIAL, cc.STATUS_REJECTED):
        c.put(f"prompt:{status}", "sha256:fp", b"body", status=status)
        assert c.get(f"prompt:{status}", "sha256:fp") is None, status


def test_schema_version_mismatch_misses(cc, tmp_path):
    cc.ContentCache(tmp_path, schema_version=1).put("prompt:甲", "sha256:fp1", b"hello")
    reader = cc.ContentCache(tmp_path, schema_version=2)
    assert reader.get("prompt:甲", "sha256:fp1") is None
    assert [e.key for e in reader.inspect()] == ["prompt:甲"]


def test_unknown_key_misses(cc, tmp_path):
    assert cc.ContentCache(tmp_path).get("prompt:不存在", "sha256:fp") is None


# --- 原子写与并发 --------------------------------------------------------------

def test_write_leaves_no_partial_files(cc, tmp_path):
    cc.ContentCache(tmp_path).put("prompt:甲", "sha256:fp1", b"payload")
    leftovers = [p.name for p in tmp_path.rglob("*.tmp")]
    assert leftovers == [], leftovers


def test_same_fingerprint_with_different_payload_is_surfaced_not_silently_kept(cc, tmp_path):
    """指纹相同但载荷不同＝指纹函数漏了输入维度，必须显式失败而不是沿用旧内容。"""
    c = cc.ContentCache(tmp_path)
    c.put("prompt:甲", "sha256:fp1", b"v1")
    with pytest.raises(cc.CacheError) as ctx:
        c.put("prompt:甲", "sha256:fp1", b"v2-much-longer-than-before")
    assert "指纹碰撞" in str(ctx.value)
    assert c.get("prompt:甲", "sha256:fp1") == b"v1"


def test_republished_identical_payload_is_reused_not_rewritten(cc, tmp_path):
    c = cc.ContentCache(tmp_path)
    first = c.put("prompt:甲", "sha256:fp1", b"v1")
    second = c.put("prompt:甲", "sha256:fp1", b"v1")
    assert first.reused is False and second.reused is True
    assert c.get("prompt:甲", "sha256:fp1") == b"v1"


def test_concurrent_writes_to_same_key_keep_one_consistent_payload(cc, tmp_path):
    payload = b"x" * 4096
    errors = []

    def worker():
        try:
            local = cc.ContentCache(tmp_path)
            for _ in range(5):
                local.put("prompt:甲", "sha256:fp1", payload)
        except Exception as exc:      # pragma: no cover - 失败时用于报错
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    reader = cc.ContentCache(tmp_path)
    assert reader.get("prompt:甲", "sha256:fp1") == payload
    assert [e.key for e in reader.inspect()] == ["prompt:甲"]


# --- inspect / invalidate / prune ---------------------------------------------

def test_inspect_reports_key_fingerprint_status_and_size(cc, tmp_path):
    c = cc.ContentCache(tmp_path)
    c.put("prompt:甲", "sha256:fp1", b"12345")
    c.put("prompt:乙", "sha256:fp2", b"123", status=cc.STATUS_FAILED)
    rows = {e.key: e for e in c.inspect()}
    assert rows["prompt:甲"].fingerprint == "sha256:fp1"
    assert rows["prompt:甲"].status == cc.STATUS_SUCCESS
    assert rows["prompt:甲"].size == 5
    assert rows["prompt:乙"].status == cc.STATUS_FAILED


def test_invalidate_drops_the_key(cc, tmp_path):
    c = cc.ContentCache(tmp_path)
    c.put("prompt:甲", "sha256:fp1", b"hello")
    assert c.invalidate("prompt:甲") is True
    assert c.get("prompt:甲", "sha256:fp1") is None
    assert c.invalidate("prompt:甲") is False


def test_prune_is_dry_run_by_default(cc, tmp_path):
    c = cc.ContentCache(tmp_path)
    c.put("prompt:甲", "sha256:fp1", b"hello", status=cc.STATUS_FAILED)
    planned = c.prune()
    assert planned == ["prompt:甲"]
    # 默认不删除：dry-run 后仍能在 inspect 里看到
    assert [e.key for e in c.inspect()] == ["prompt:甲"]


def test_prune_with_apply_removes_only_rejected_and_failed(cc, tmp_path):
    c = cc.ContentCache(tmp_path)
    c.put("prompt:好", "sha256:fp1", b"good")
    c.put("prompt:坏", "sha256:fp2", b"bad", status=cc.STATUS_REJECTED)
    removed = c.prune(apply=True)
    assert removed == ["prompt:坏"]
    assert c.get("prompt:好", "sha256:fp1") == b"good"


def test_prune_never_touches_user_artifacts_outside_cache(cc, tmp_path):
    user_artifact = tmp_path / "提示词" / "基础" / "基础提示词.md"
    user_artifact.parent.mkdir(parents=True)
    user_artifact.write_text("用户产物", encoding="utf-8")
    c = cc.ContentCache(tmp_path)
    c.put("prompt:坏", "sha256:fp2", b"bad", status=cc.STATUS_FAILED)
    c.prune(apply=True)
    assert user_artifact.read_text(encoding="utf-8") == "用户产物"


def test_cache_lives_under_process_dir(cc, tmp_path):
    cc.ContentCache(tmp_path).put("prompt:甲", "sha256:fp1", b"hello")
    assert (tmp_path / ".process").is_dir()
    assert all(str(p).startswith(str(tmp_path / ".process")) for p in tmp_path.rglob("*.json"))
