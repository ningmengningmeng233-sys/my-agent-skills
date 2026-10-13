# -*- coding: utf-8 -*-
"""release_surface.py — 发布档位与裁剪清单判定。

发布包会按档位裁剪部分文件（例如运行期日志 *.log 不随包分发，但其 sha256 已封存在
evidence-index.json 里）。判定只有三种结果，绝不因为「文件不存在」就放过：

- declared        清单里声明要随包发布的文件，磁盘上必须存在，缺了就是被删/被丢
- withheld_covered 命中清单声明的裁剪模式，且随包的索引里存着它的路径与 sha256
- undeclared      既没声明随包、也不在裁剪档位里 —— 一律按缺失报错，不给通用 skip

用法：python scripts/release_surface.py check [--root <目录>]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = "release-manifest.json"


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def index_entries(root: Path, manifest: dict) -> dict:
    """随包证据索引里的条目：{路径或文件名: sha256}。"""
    out: dict[str, str] = {}
    candidates = []
    report = manifest.get("closure_report")
    if isinstance(report, str):
        candidates.append(root / report / "evidence-index.json")
    evidence = root / "evidence"
    if evidence.is_dir():
        candidates += sorted(evidence.glob("*/evidence-index.json"))
    for path in candidates:
        doc = _load_json(path)
        if not doc:
            continue
        items = doc if isinstance(doc, list) else (doc.get("files") or doc.get("entries") or [])
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            key = item.get("path") or item.get("file") or item.get("log")
            sha = item.get("sha256") or item.get("hash")
            if key and sha:
                out[str(key).replace("\\", "/")] = str(sha)
                out[Path(str(key)).name] = str(sha)
    return out


class ReleaseSurface:
    def __init__(self, root: Path = REPO):
        self.root = Path(root)
        self.manifest = _load_json(self.root / MANIFEST) or {}
        files = self.manifest.get("files")
        self.declared = set()
        if isinstance(files, dict):
            self.declared = set(files)
        elif isinstance(files, list):
            self.declared = {f.get("path", "") for f in files if isinstance(f, dict)}
        self.withheld_patterns = tuple(self.manifest.get("withheld_patterns") or ())
        self.tier = str(self.manifest.get("release_tier") or "")
        self._index = None

    @property
    def index(self) -> dict:
        if self._index is None:
            self._index = index_entries(self.root, self.manifest)
        return self._index

    def is_withheld_pattern(self, rel: str) -> bool:
        rel = str(rel).replace("\\", "/")
        return bool(self.withheld_patterns) and rel.lower().endswith(self.withheld_patterns)

    def status(self, rel: str) -> str:
        rel = str(rel).replace("\\", "/")
        if rel in self.declared:
            return "declared"
        if self.is_withheld_pattern(rel):
            sha = self.index.get(rel) or self.index.get(Path(rel).name)
            return "withheld_covered" if sha else "undeclared"
        return "undeclared"

    def check(self, rel: str) -> tuple[bool, str]:
        """返回 (可接受, 说明)。不存在时只有 withheld_covered 才可接受。"""
        rel = str(rel).replace("\\", "/")
        path = self.root / rel
        if path.is_file():
            return True, "present"
        state = self.status(rel)
        if state == "declared":
            return False, f"发布清单声明要随包的文件不见了: {rel}"
        if state == "withheld_covered":
            return True, (f"按档位 {self.tier} 裁剪未随包，sha256 已封存证据索引"
                          f"（{self.index.get(rel, '')[:12]}）")
        return False, (f"产物既未声明随包、也不在档位裁剪清单里，且磁盘不存在: {rel}"
                       f"（裁剪档位={list(self.withheld_patterns) or '未声明'}）")


def main() -> int:
    root = REPO
    argv = sys.argv[1:]
    if argv and argv[0] == "check":
        if "--root" in argv:
            root = Path(argv[argv.index("--root") + 1])
        surface = ReleaseSurface(root)
        doc = _load_json(root / "凭证协议.json") or _load_json(root / "verification-protocol.json")
        if not doc:
            print("[check] 找不到协议文件，跳过", file=sys.stderr)
            return 0
        bad = []
        for rec in doc.get("records", []):
            if rec.get("status") != "PASS":
                continue
            for p in rec.get("artifact_paths") or []:
                ok, why = surface.check(p)
                if not ok:
                    bad.append(f"{rec.get('gate')}: {why}")
        for line in bad:
            print("[check]", line, file=sys.stderr)
        print(f"[check] tier={surface.tier} declared={len(surface.declared)} "
              f"withheld={list(surface.withheld_patterns)} problems={len(bad)}")
        return 1 if bad else 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
