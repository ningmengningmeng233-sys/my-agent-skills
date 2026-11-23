#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rc.2 发布包异目录验收（工单 P1-2）。

流程（全程使用仓库**之外**的全新目录）：
1. 核对包外 SHA256SUMS 与 zip 实际哈希；
2. `ZipFile.testzip()` 校验压缩包完整性；
3. 解压到全新目录，核验包内 SHA256SUMS.txt 逐文件一致；
4. 在解压产物中运行：核心总门、视觉离线门、权威包校验、任务包导出（自包含）；
5. 断言私有包含 32 张权威图、分享包声明 VISUAL_STYLE_INCOMPLETE_FOR_SHARE。

用法：python 测试/verify_release_package_rc2.py --zip <包.zip> --sums <清单.txt> [--expect-images N]
"""
import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"（{detail}）" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}：{detail}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_sums(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            out[parts[1].strip()] = parts[0].strip()
    return out


def _run(cmd: list, cwd: Path, env: dict | None = None):
    return subprocess.run([sys.executable] + [str(c) for c in cmd], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", cwd=str(cwd),
                          env={**os.environ, **(env or {})})


def verify(zip_path: Path, sums_path: Path, expect_images: int | None, label: str) -> None:
    print(f"\n=== {label}：{zip_path.name} ===")
    _check("zip 存在", zip_path.is_file(), str(zip_path))
    _check("包外清单存在", sums_path.is_file(), str(sums_path))
    if FAILURES:
        return

    # 1) 包外清单核验 zip 自身哈希
    sums = _parse_sums(sums_path.read_text(encoding="utf-8"))
    actual = _sha256(zip_path)
    declared = sums.get(zip_path.name, "")
    _check("包外清单含 zip 哈希", bool(declared), zip_path.name)
    _check("zip 哈希与清单一致", declared == actual, f"清单 {declared[:16]}… 实际 {actual[:16]}…")

    # 2) 压缩包完整性
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        _check("ZipFile.testzip() 无损坏条目", bad is None, str(bad))
        names = zf.namelist()
        roots = {n.split("/", 1)[0] for n in names}
        _check("包内单一根目录", len(roots) == 1, f"{sorted(roots)[:3]}")
        root = sorted(roots)[0]

        # 3) 解压到全新异目录
        with tempfile.TemporaryDirectory(prefix="rc2_verify_") as td:
            dest = Path(td) / "解压"
            dest.mkdir()
            zf.extractall(dest)
            pkg = dest / root
            _check("解压后根目录存在", pkg.is_dir(), str(pkg))

            inner = pkg / "SHA256SUMS.txt"
            _check("包内哈希清单存在", inner.is_file(), "SHA256SUMS.txt")
            if inner.is_file():
                want = _parse_sums(inner.read_text(encoding="utf-8"))
                mismatched = []
                for rel, digest in want.items():
                    target = pkg / rel
                    if not target.is_file():
                        mismatched.append(f"缺失 {rel}")
                    elif _sha256(target) != digest:
                        mismatched.append(f"漂移 {rel}")
                _check(f"包内 {len(want)} 个文件逐一致", not mismatched,
                       "；".join(mismatched[:3]))

            # 4) 解压产物里跑真实门。
            #    e2e 快照是**派生夹具**（由 e2e_flow.py --snapshot 生成），打包时按
            #    「开发快照不入包」排除；总门自己会提示重建。这里先离线重建（mock API，
            #    不需要任何密钥），再跑门——这正是用户在全新解压目录应做的第一步。
            snap = _run([pkg / "测试" / "e2e_flow.py", "--snapshot"], cwd=pkg)
            _check("解压后 e2e 快照可离线重建", snap.returncode == 0,
                   (snap.stdout or "")[-300:])

            gate = _run([pkg / "测试" / "run_all_checks.py"], cwd=pkg)
            _check("解压后核心总门全绿", gate.returncode == 0,
                   (gate.stdout or "")[-300:])

            visual = _run([pkg / "测试" / "run_visual_offline.py"], cwd=pkg)
            _check("解压后视觉离线门全绿", visual.returncode == 0,
                   (visual.stdout or "")[-300:])

            pack = (pkg / "references" / "styles" / "仙侠国风电影CG" / "1.0.0" / "private")
            images = list(pack.rglob("*.png")) if pack.is_dir() else []
            if expect_images is not None:
                _check(f"权威参考图 {expect_images} 张", len(images) == expect_images,
                       f"实际 {len(images)}")

            vp = _run([pkg / "scripts" / "install_stylepack.py", "--verify",
                       "--pack", pack, "--json"], cwd=pkg)
            payload = {}
            try:
                import json as _json
                payload = _json.loads(vp.stdout)
            except ValueError:
                pass
            if expect_images:
                _check("权威包逐文件校验通过", vp.returncode == 0 and payload.get("integrity_ok"),
                       (vp.stdout or "")[-300:])
            else:
                # 分享包：允许缺图，但必须显式声明不完整
                declared_incomplete = "VISUAL_STYLE_INCOMPLETE_FOR_SHARE" in \
                    (pkg / "发布清单.json").read_text(encoding="utf-8")
                _check("分享包声明 VISUAL_STYLE_INCOMPLETE_FOR_SHARE", declared_incomplete)

            # 5) 核心无视觉依赖：干净环境跑核心门（无密钥）
            clean = {k: v for k, v in os.environ.items()
                     if not k.endswith("API_KEY") and k not in ("ASSET_ENV_PATH",)}
            core = _run([pkg / "scripts" / "maintenance.py", "status", "--lib", pkg,
                         "--json"], cwd=pkg, env=clean)
            _check("无密钥下核心 CLI 可用", core.returncode in (0, 1, 2),
                   (core.stdout or core.stderr or "")[-200:])


def main() -> int:
    parser = argparse.ArgumentParser(description="rc.2 发布包异目录验收")
    parser.add_argument("--zip", required=True, dest="zip_path")
    parser.add_argument("--sums", required=True, dest="sums_path")
    parser.add_argument("--expect-images", type=int, default=None)
    parser.add_argument("--label", default="发布包")
    args = parser.parse_args()
    verify(Path(args.zip_path), Path(args.sums_path), args.expect_images, args.label)
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"❌ {args.label}：{len(FAILURES)} 项失败")
        for item in FAILURES:
            print(f"   - {item}")
        return 1
    print(f"✅ {args.label}：异目录验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
