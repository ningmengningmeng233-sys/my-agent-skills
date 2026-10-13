# -*- coding: utf-8 -*-
"""IDX-01 回归：--append 追更与 --volume 分卷不得清空既有段数据集。

缺陷：Stager.commit() 把段目录里所有不在本次终态集合里的文件都移入
<lib>/.superseded/。追更（只新增/并入末段）与分卷（只产出本卷段）运行时，
未被覆盖的前续段不在终态集合里，也会被隔离，导致活跃数据集被清空；
追更并入末段分支甚至绕过 Stager 直接写盘，提交时连刚并入的末段一起隔离。

受控 RED：以下断言在修复前必须失败（段文件被移入 .superseded），修复后通过。
全离线，不调模型。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
PY = sys.executable
NOVEL, AUTHOR = "追更保护", "验证作者"
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
    path.write_text("# 追更保护\n" + "".join(
        f"第{n}章 标题{n}\n这是第{n}章的正文内容，独有句子{n}。\n" for n in range(1, chapters + 1)),
        encoding="utf-8")


def new_lib(tmp_path):
    lib = tmp_path / "lib"
    for d in ("小说原文", "小说索引", "章节索引"):
        (lib / d).mkdir(parents=True)
    return lib


def split(tmp_path, lib, src, *extra):
    return run(tmp_path, "split_segments.py", src,
               "--novel", NOVEL, "--author", AUTHOR, "--lib", lib, *extra)


def segs(lib):
    d = lib / "小说原文" / PAIR
    return sorted(p.name for p in d.glob("*.md")) if d.is_dir() else []


def quarantine(lib):
    return sorted(str(p.relative_to(lib)).replace(os.sep, "/") for p in lib.rglob("*")
                  if p.is_file() and ".superseded" in p.parts)


def test_append_merge_keeps_earlier_segments(tmp_path):
    """追更并入末段：前续段保留在活跃目录，未被移入 .superseded。"""
    src = tmp_path / "src.txt"
    lib = new_lib(tmp_path)
    write_source(src, 7)
    assert split(tmp_path, lib, src, "--chapters-per-seg", "5").returncode == 0
    before = segs(lib)
    assert len(before) == 2
    first_bytes = (lib / "小说原文" / PAIR / before[0]).read_bytes()

    write_source(src, 9)  # 新增 2 章（<5）→ 并入末段
    r = split(tmp_path, lib, src, "--chapters-per-seg", "5", "--append")
    assert r.returncode == 0, (r.stdout or "")[-500:] + (r.stderr or "")[-500:]

    live = segs(lib)
    assert before[0] in live, f"前续段被隔离/丢失：{live}，隔离区：{quarantine(lib)}"
    assert (lib / "小说原文" / PAIR / before[0]).read_bytes() == first_bytes
    assert not quarantine(lib), f"追更并入不得隔离任何段：{quarantine(lib)}"
    merged = (lib / "小说原文" / PAIR / before[1]).read_text(encoding="utf-8")
    assert "第9章" in merged, "末段必须包含追更章节"


def test_append_new_segments_keep_earlier_segments(tmp_path):
    """追更新建段：既有段保留在活跃目录，未被移入 .superseded。"""
    src = tmp_path / "src.txt"
    lib = new_lib(tmp_path)
    write_source(src, 5)
    assert split(tmp_path, lib, src, "--chapters-per-seg", "5").returncode == 0
    before = segs(lib)
    assert len(before) == 1
    first_bytes = (lib / "小说原文" / PAIR / before[0]).read_bytes()

    write_source(src, 11)  # 新增 6 章（>=5）→ 新建段
    r = split(tmp_path, lib, src, "--chapters-per-seg", "5", "--append")
    assert r.returncode == 0, (r.stdout or "")[-500:] + (r.stderr or "")[-500:]

    live = segs(lib)
    assert before[0] in live, f"既有段被隔离/丢失：{live}，隔离区：{quarantine(lib)}"
    assert (lib / "小说原文" / PAIR / before[0]).read_bytes() == first_bytes
    assert len(live) == 3, f"应为 1 旧段 + 2 新段：{live}"
    assert not quarantine(lib), f"追更新建不得隔离任何段：{quarantine(lib)}"


def test_volume_split_keeps_other_volumes(tmp_path):
    """分卷切分：其它卷的既有段保留在活跃目录，未被移入 .superseded。"""
    vol1 = tmp_path / "vol1.txt"
    vol2 = tmp_path / "vol2.txt"
    lib = new_lib(tmp_path)
    vol1.write_text("# 卷一\n" + "".join(
        f"第{n}章 卷一标题{n}\n卷一第{n}章正文，独有句子卷一{n}。\n" for n in range(1, 6)),
        encoding="utf-8")
    vol2.write_text("# 卷二\n" + "".join(
        f"第{n}章 卷二标题{n}\n卷二第{n}章正文，独有句子卷二{n}。\n" for n in range(1, 4)),
        encoding="utf-8")

    assert split(tmp_path, lib, vol1, "--volume", "第1卷").returncode == 0
    vol1_segs = segs(lib)
    assert len(vol1_segs) == 1
    vol1_bytes = (lib / "小说原文" / PAIR / vol1_segs[0]).read_bytes()

    r = split(tmp_path, lib, vol2, "--volume", "第2卷")
    assert r.returncode == 0, (r.stdout or "")[-500:] + (r.stderr or "")[-500:]

    live = segs(lib)
    assert vol1_segs[0] in live, f"第1卷段被隔离/丢失：{live}，隔离区：{quarantine(lib)}"
    assert (lib / "小说原文" / PAIR / vol1_segs[0]).read_bytes() == vol1_bytes
    assert len(live) == 2, f"应为第1卷 1 段 + 第2卷 1 段：{live}"
    assert not quarantine(lib), f"分卷切分不得隔离其它卷段：{quarantine(lib)}"
