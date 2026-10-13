# -*- coding: utf-8 -*-
"""转录索引 · 快照同步（镜像副本生成）

2026-08-16 拷问 Q1/E 落地：运行目录（Obsidian 源目录）是权威，
本脚本把产物（索引库/校对版/向量库）复制到外部快照目录，并重写快照 README
（只读镜像声明 + 校验摘要 + 生成时间），替代手动复制，防止快照过期/失实。

用法: python _同步快照.py [目标目录]
  缺省目标: 桌面/知识库索引/v2（快照只读，勿直接修改）
"""
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import IDX_DIR, PROOF_DIR, SNAPSHOT_DEST, VEC_DIR, SRC_DIR

# 桌面可能重定向（本机 D:\桌面），优先用已有「知识库索引」目录的桌面，否则系统默认桌面
_DESKTOP_CANDS = [Path("D:/桌面"), Path.home() / "Desktop"]
_DESKTOP = next((d for d in _DESKTOP_CANDS if (d / "知识库索引").exists()), Path.home() / "Desktop")
DEFAULT_DEST = _DESKTOP / "知识库索引" / "v2"

README_TEMPLATE = """# 转录索引 v2（只读镜像快照）

> **{time} 由 `_同步快照.py` 自动生成**。运行目录为 Obsidian 源目录
> （`{src}`），本目录只是产物镜像，**请勿直接修改**——重新生成请运行技能内
> `python scripts/_同步快照.py`。

## 内容与校验

| 目录 | 数量 | 校验 |
|---|---|---|
| `索引库/` | {idx_n} 个 `*-idx.md` + manifest.json | entries 摘要 `{entries_md5_short}…` |
| `校对版/` | {proof_n} 个 `*_校对版.txt` | 行号引用体检见技能 `_检查行号引用.py` |
| `向量库/` | embeddings.npy + entries.json（{vec_n} 条） | 维度 {vec_shape} |

## 使用提示

- 本镜像**不含源转录文件**（体积/隐私考虑）：条目行号绑定源文件，精读定位请回到运行目录。
- 检索入口 = Cherry Studio 的「转录索引」技能（SKILL.md 在 `Data\\Skills\\转录索引`）。
- 体检：`python scripts/_检查行号引用.py`（在运行目录跑，含数据完整性四项检查）。
"""


def md5(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else (SNAPSHOT_DEST or DEFAULT_DEST)
    dest = dest.expanduser().resolve()
    src = Path(SRC_DIR)
    required = [src / sub for sub in ("索引库", "校对版", "向量库")]
    missing = [str(p) for p in required if not p.is_dir()]
    if missing:
        raise RuntimeError("源目录产物不完整，无法同步快照：" + "; ".join(missing))
    entries_path = VEC_DIR / "entries.json"
    emb_path = VEC_DIR / "embeddings.npy"
    if not entries_path.exists() or not emb_path.exists():
        raise RuntimeError("向量库不完整，请先运行 build.sh --force")

    # 临时目录完成全部复制和 README 写入后，再整体替换，避免中断产生半份快照。
    tmp = dest.with_name(dest.name + f".tmp-{os.getpid()}")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        for sub in ("索引库", "校对版", "向量库"):
            s, d = src / sub, tmp / sub
            shutil.copytree(s, d)
            n = len(list(d.iterdir()))
            print(f"  ✓ {sub}/ → {dest / sub}（{n} 个文件）")

        # 复制校验：源 entries.json 与临时副本必须一致，防止复制损坏后替换。
        if md5(entries_path) != md5(tmp / "向量库" / "entries.json"):
            raise RuntimeError("快照复制校验失败：向量库 entries.json 不一致")
        entries = json.loads(entries_path.read_text(encoding="utf-8"))
        import numpy as np
        shape = np.load(emb_path, mmap_mode="r").shape
        idx_n = len(list((tmp / "索引库").glob("*-idx.md")))
        proof_n = len(list((tmp / "校对版").glob("*_校对版.txt")))
        (tmp / "README.md").write_text(
            README_TEMPLATE.format(
                time=time.strftime("%Y-%m-%d %H:%M"), src=src,
                idx_n=idx_n, proof_n=proof_n, vec_n=len(entries),
                vec_shape=f"{shape[0]}×{shape[1]}",
                entries_md5_short=md5(entries_path)[:16],
            ), encoding="utf-8")
        old = dest.with_name(dest.name + f".old-{os.getpid()}")
        if old.exists():
            shutil.rmtree(old)
        if dest.exists():
            dest.replace(old)
        tmp.replace(dest)
        if old.exists():
            shutil.rmtree(old)
    except Exception:
        if tmp.exists():
            shutil.rmtree(tmp)
        raise
    print(f"  ✓ README.md 已生成")
    print(f"快照完成: {dest}")


if __name__ == "__main__":
    main()
