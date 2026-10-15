# -*- coding: utf-8 -*-
"""随包回归：v4.6.1-cand 安全增量事务（全离线，不调模型）。

覆盖：force 重切不留过期段、被取代段与其章节索引一起可恢复隔离、
反复 force 幂等、陌生文件在场拒绝替换、别书不受影响、导航过期给 STALE_NAVIGATION、
导航总章数不多算、冲突时不动既有成果。
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
PY = sys.executable
NOVEL, AUTHOR = "合成风测试", "校验作者"
PAIR = f"{NOVEL}-{AUTHOR}"


def env_for(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if not any(s in k.upper() for s in
                      ("KEY", "TOKEN", "SECRET", "PROXY", "ENDPOINT", "OPENAI", "SUMMARY_"))}
    env.update({"INDEX_LIB_DIR": "", "PYTHONIOENCODING": "utf-8",
                "PYTHONPYCACHEPREFIX": str(tmp_path / "pyc"),
                "TEMP": str(tmp_path), "TMP": str(tmp_path)})
    return env


def run(tmp_path, script, *args):
    return subprocess.run([PY, "-X", "utf8", str(SCRIPTS / script), *[str(a) for a in args]],
                          cwd=str(SCRIPTS.parent), env=env_for(tmp_path), capture_output=True,
                          timeout=300, encoding="utf-8", errors="replace")


def write_source(path, chapters):
    path.write_text("# 合成风测试\n" + "".join(
        f"第{n}章 标题{n}\n这是第{n}章的正文内容，独有句子{n}。\n" for n in range(1, chapters + 1)),
        encoding="utf-8")


def new_lib(tmp_path):
    lib = tmp_path / "lib"
    for d in ("小说原文", "小说索引", "章节索引"):
        (lib / d).mkdir(parents=True)
    return lib


def split(tmp_path, lib, src, per_seg=1, force=False):
    args = [src, "--novel", NOVEL, "--author", AUTHOR, "--lib", lib, "--chapters-per-seg", str(per_seg)]
    if force:
        args.append("--force")
    return run(tmp_path, "split_segments.py", *args)


def segs(lib):
    d = lib / "小说原文" / PAIR
    return sorted(p.name for p in d.glob("*.md")) if d.is_dir() else []


def idxs(lib):
    return sorted(p.name for p in (lib / "章节索引").glob("*.md"))


def nav_total(lib):
    f = sorted((lib / "小说索引").glob("*.md"))
    if not f:
        return None
    m = re.search(r"总章节数:\s*(\d+)", f[0].read_text(encoding="utf-8"))
    return int(m.group(1)) if m else None


def make_indexes(tmp_path, lib):
    for n in segs(lib):
        m = re.match(rf"{re.escape(PAIR)}-(.+)-(\d{{3}})\.md$", n)
        assert m, f"段文件名不符合合同：{n}"
        rng, seq = m.group(1), m.group(2)
        chapters = re.findall(r"第(\d+)章", rng) or ["1"]
        body = "\n".join(f"## 第{c}章 标题{c}\n- 概况：略\n- 金句：独有句子{c}\n" for c in chapters)
        (lib / "章节索引" / f"{PAIR}-{rng}-{seq}-idx.md").write_text(
            "---\n小说名称: 《" + NOVEL + "》\n作者: " + AUTHOR + "\n索引类型: 章节索引\n索引序号: " + seq
            + "\n章节范围: " + rng + "\n总章节数: " + str(len(chapters)) + "\n是否完结: 否\n包含元素: [测试]\n"
              "源文件: 小说原文/" + PAIR + "/" + n + "\n创建日期: 2026-10-01\n最后更新: 2026-10-01\n状态: 完成\n---\n"
            + body + "\n", encoding="utf-8")


def build_nav(tmp_path, lib, force=False):
    args = ["--novel", PAIR, "--lib", lib] + (["--force"] if force else [])
    return run(tmp_path, "build_novel_index.py", *args)


def quarantine(lib):
    return sorted(str(p.relative_to(lib)).replace(os.sep, "/") for p in lib.rglob("*")
                  if p.is_file() and ".superseded" in p.parts)


def test_force_resplit_matches_clean_resplit(tmp_path):
    src = tmp_path / "src.txt"
    ref, inc = new_lib(tmp_path), new_lib(tmp_path / "i")
    write_source(src, 3)
    assert split(tmp_path, inc, src).returncode == 0
    write_source(src, 5)
    assert split(tmp_path, ref, src).returncode == 0
    assert split(tmp_path, inc, src, force=True).returncode == 0
    assert segs(inc) == segs(ref), f"force 重切留下过期段：{sorted(set(segs(inc)) - set(segs(ref)))}"


def test_superseded_segment_and_index_are_quarantined(tmp_path):
    src = tmp_path / "src.txt"
    lib = new_lib(tmp_path)
    write_source(src, 3)
    split(tmp_path, lib, src, per_seg=1)
    make_indexes(tmp_path, lib)
    before = idxs(lib)
    write_source(src, 5)
    assert split(tmp_path, lib, src, per_seg=2, force=True).returncode == 0
    q = quarantine(lib)
    assert any("/小说原文/" in x for x in q), f"被取代段未隔离：{q}"
    assert any("/章节索引/" in x for x in q), f"被取代索引未隔离：{q}"
    live = idxs(lib)
    assert len(live) < len(before), f"过期索引必须退出活动集：{before} → {live}"
    # 真实不变量：活动索引不得成为孤儿（其源段文件必须仍在活动段集里）
    seg_now = set(segs(lib))
    orphans = []
    for n in live:
        ref = re.search(r"源文件: 小说原文/[^\n]+/([^\n]+)",
                        (lib / "章节索引" / n).read_text(encoding="utf-8"))
        if not ref or ref.group(1) not in seg_now:
            orphans.append(n)
    assert not orphans, f"存在指向已消失段文件的孤儿索引：{orphans}"


def test_repeated_force_is_idempotent(tmp_path):
    src = tmp_path / "src.txt"
    lib = new_lib(tmp_path)
    write_source(src, 3)
    split(tmp_path, lib, src, per_seg=1)
    write_source(src, 5)
    split(tmp_path, lib, src, per_seg=2, force=True)
    s1, q1 = segs(lib), quarantine(lib)
    assert split(tmp_path, lib, src, per_seg=2, force=True).returncode == 0
    assert segs(lib) == s1 and quarantine(lib) == q1, "第二次 force 不得继续改动活动集或隔离区"


def test_unknown_file_blocks_replacement(tmp_path):
    src = tmp_path / "src.txt"
    lib = new_lib(tmp_path)
    write_source(src, 3)
    split(tmp_path, lib, src, per_seg=1)
    foreign = lib / "小说原文" / PAIR / "外来的书-别人-第9章-009.md"
    foreign.write_text("不是本书产物\n", encoding="utf-8")
    before = sorted(p.name for p in (lib / "小说原文" / PAIR).iterdir())
    write_source(src, 5)
    r = split(tmp_path, lib, src, per_seg=1, force=True)
    assert r.returncode != 0, "陌生文件在场必须停手"
    assert sorted(p.name for p in (lib / "小说原文" / PAIR).iterdir()) == before
    assert foreign.read_text(encoding="utf-8") == "不是本书产物\n"
    assert not list((lib / "小说原文" / PAIR).glob("*.new*")), "不得留下暂存半件"


def test_other_books_untouched(tmp_path):
    src, other = tmp_path / "src.txt", tmp_path / "other.txt"
    lib = new_lib(tmp_path)
    write_source(src, 3)
    other.write_text("# 另一本\n第一章 他书\n他书正文一句。\n第二章 他书二\n另一句。\n", encoding="utf-8")
    split(tmp_path, lib, src, per_seg=1)
    run(tmp_path, "split_segments.py", other, "--novel", "另一本", "--author", AUTHOR,
        "--lib", lib, "--chapters-per-seg", "1")
    run(tmp_path, "split_segments.py", other, "--novel", NOVEL, "--author", "别人",
        "--lib", lib, "--chapters-per-seg", "1")
    dirs = sorted(p.name for p in (lib / "小说原文").iterdir())
    write_source(src, 5)
    split(tmp_path, lib, src, per_seg=2, force=True)
    assert sorted(p.name for p in (lib / "小说原文").iterdir()) == dirs
    for x in quarantine(lib):
        assert PAIR in x, f"隔离区动了别人的书：{x}"


def test_nav_reports_stale_then_refreshes_to_true_chapter_count(tmp_path):
    src = tmp_path / "src.txt"
    lib = new_lib(tmp_path)
    write_source(src, 3)
    split(tmp_path, lib, src, per_seg=1)
    make_indexes(tmp_path, lib)
    assert build_nav(tmp_path, lib).returncode == 0
    write_source(src, 5)
    split(tmp_path, lib, src, per_seg=1, force=True)
    make_indexes(tmp_path, lib)
    r = build_nav(tmp_path, lib)
    out = (r.stdout or "") + (r.stderr or "")
    assert "STALE_NAVIGATION" in out and r.returncode != 0, \
        f"导航输入已变必须给可处置状态，不得静默 rc0：{out[-300:]}"
    assert build_nav(tmp_path, lib, force=True).returncode == 0
    assert nav_total(lib) == 5, f"导航总章数须等于真实章数，实测 {nav_total(lib)}"


def test_nav_never_double_counts_after_resplit(tmp_path):
    src = tmp_path / "src.txt"
    ref, inc = new_lib(tmp_path), new_lib(tmp_path / "i")
    write_source(src, 3)
    split(tmp_path, inc, src, per_seg=1)
    make_indexes(tmp_path, inc)
    build_nav(tmp_path, inc)
    write_source(src, 5)
    split(tmp_path, ref, src, per_seg=1)
    make_indexes(tmp_path, ref)
    build_nav(tmp_path, ref, force=True)
    split(tmp_path, inc, src, per_seg=1, force=True)
    make_indexes(tmp_path, inc)
    build_nav(tmp_path, inc, force=True)
    assert nav_total(inc) == nav_total(ref) == 5, f"{nav_total(inc)} vs {nav_total(ref)}"


def test_conflict_keeps_previous_results(tmp_path):
    src = tmp_path / "src.txt"
    lib = new_lib(tmp_path)
    write_source(src, 3)
    split(tmp_path, lib, src, per_seg=1)
    make_indexes(tmp_path, lib)
    assert build_nav(tmp_path, lib).returncode == 0
    nav_before = {p.name: p.read_bytes() for p in (lib / "小说索引").glob("*.md")}
    victim = lib / "小说原文" / PAIR / f"{PAIR}-第1章-001.md"
    victim.write_text("被外部改过的段内容\n", encoding="utf-8")
    untouched = {p.name: p.stat().st_mtime_ns for p in (lib / "小说原文" / PAIR).glob("*.md")
                 if p.name != victim.name}
    write_source(src, 5)
    r = split(tmp_path, lib, src, per_seg=1)
    assert r.returncode != 0, "同名不同内容必须报冲突并非零退出"
    assert {p.name: p.stat().st_mtime_ns for p in (lib / "小说原文" / PAIR).glob("*.md")
            if p.name in untouched} == untouched, "失败路径不得改动其它段"
    assert {p.name: p.read_bytes() for p in (lib / "小说索引").glob("*.md")} == nav_before
    assert not list((lib / "小说原文" / PAIR).glob("*.new*")), "失败必须清掉暂存件"
