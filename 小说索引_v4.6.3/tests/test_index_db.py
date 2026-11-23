import os

import pytest

from index_db import IndexDB, file_hash


def test_file_hash_changes_with_content(tmp_lib):
    path = os.path.join(tmp_lib, "test.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("hello")
    h1 = file_hash(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write("world")
    h2 = file_hash(path)
    assert h1 != h2
    assert len(h1) == 16


def test_should_skip_done_unchanged(tmp_lib, sample_segment):
    idx_path = sample_segment.replace(".md", "-idx.md")
    with open(idx_path, "w", encoding="utf-8") as f:
        f.write("idx")

    with IndexDB(tmp_lib) as db:
        seg_hash = file_hash(sample_segment)
        db.mark_done(sample_segment, idx_path, seg_hash=seg_hash)
        assert db.should_skip(sample_segment, idx_path, seg_hash)


def test_should_not_skip_when_hash_changes(tmp_lib, sample_segment):
    idx_path = sample_segment.replace(".md", "-idx.md")
    with open(idx_path, "w", encoding="utf-8") as f:
        f.write("idx")

    with IndexDB(tmp_lib) as db:
        db.mark_done(sample_segment, idx_path, seg_hash="oldhash")
        assert not db.should_skip(sample_segment, idx_path, file_hash(sample_segment))


def test_mark_failed_increments_retries(tmp_lib, sample_segment):
    with IndexDB(tmp_lib) as db:
        db.mark_failed(sample_segment, "boom")
        db.mark_failed(sample_segment, "boom again")
        row = db.get(sample_segment)
        assert row[2] == "failed"
        assert row[3] == 2


def test_stats(tmp_lib, sample_segment):
    idx_path = sample_segment.replace(".md", "-idx.md")
    with open(idx_path, "w", encoding="utf-8") as f:
        f.write("idx")

    with IndexDB(tmp_lib) as db:
        db.mark_done(sample_segment, idx_path, prompt_tokens=10, completion_tokens=20)
        statuses, (p, c) = db.stats()
        assert statuses["done"] == 1
        assert p == 10
        assert c == 20
