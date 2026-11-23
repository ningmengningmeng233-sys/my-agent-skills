#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自制 Agent 技能柜 · 根级自动化校验与分发打包工具（stdlib + PyYAML）。

用法：
  python scripts/cabinet_tools.py check                      # 全仓一致性与契约体检
  python scripts/cabinet_tools.py status                     # 输出全部公开技能状态总览
  python scripts/cabinet_tools.py pack --all                 # 打包全部公开技能至 dist/
  python scripts/cabinet_tools.py pack --id lemon-thinking   # 打包指定单套技能至 dist/
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

try:
    import yaml
except ImportError:
    print("缺少 PyYAML，请先安装 PyYAML 后运行。", file=sys.stderr)
    sys.exit(2)

ROOT = Path(__file__).resolve().parent.parent
KEBAB_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

EXCLUDE_DIRS = {
    ".git",
    ".github",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "output",
    "state",
    ".db",
    ".lemon",
    ".logs",
}
EXCLUDE_FILES = {".env", ".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".pyd"}
FIXED_ZIP_TIME = (2026, 11, 24, 0, 0, 0)


def load_catalog() -> dict:
    return json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))


def parse_frontmatter(skill_path: Path) -> dict:
    text = skill_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{skill_path} 缺少 YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"{skill_path} frontmatter 未闭合")
    data = yaml.safe_load(parts[1])
    if not isinstance(data, dict):
        raise ValueError(f"{skill_path} frontmatter 不是键值映射")
    return data


def extract_version(fm: dict) -> str | None:
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    v = meta.get("version") or fm.get("version")
    return str(v).strip() if v is not None else None


def cmd_check(_args: argparse.Namespace) -> int:
    catalog = load_catalog()
    products = catalog.get("products", [])
    errors: list[str] = []

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    ci_path = ROOT / ".github" / "workflows" / "ci.yml"
    ci_text = ci_path.read_text(encoding="utf-8") if ci_path.is_file() else ""

    for p in products:
        pid = p["id"]
        ver = p["version"]
        folder_name = p["folder"]
        folder = ROOT / folder_name
        if not folder.is_dir():
            errors.append(f"[{pid}] 目录不存在：{folder_name}")
            continue

        skill = folder / "SKILL.md"
        if not skill.is_file():
            errors.append(f"[{pid}] 缺少入口：{folder_name}/SKILL.md")
            continue

        try:
            fm = parse_frontmatter(skill)
        except Exception as exc:
            errors.append(f"[{pid}] SKILL.md frontmatter 解析失败：{exc}")
            continue

        sname = str(fm.get("name") or "").strip()
        if not KEBAB_RE.match(sname):
            errors.append(f"[{pid}] SKILL.md name 必须为英文 kebab-case，实测：{sname!r}")

        if "skill_name" in p and p["skill_name"] != sname:
            errors.append(f"[{pid}] catalog skill_name ({p['skill_name']}) != SKILL.md name ({sname})")

        sv = extract_version(fm)
        if sv != ver:
            errors.append(f"[{pid}] 版本不一致：catalog={ver} vs SKILL.md={sv}")

        for req_key in ("description", "author", "license"):
            if not str(fm.get(req_key) or (fm.get("metadata") or {}).get(req_key) or "").strip():
                errors.append(f"[{pid}] SKILL.md frontmatter 缺少字段：{req_key}")

        meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
        if not str(meta.get("display_name") or "").strip():
            errors.append(f"[{pid}] SKILL.md metadata 缺少 display_name")

        # License / third-party notice presence
        has_lic = (
            (folder / "LICENSE").is_file()
            or (folder / "LICENSE-PONYTAIL").is_file()
            or (folder / "package" / "THIRD_PARTY_NOTICES.md").is_file()
        )
        if not has_lic:
            errors.append(f"[{pid}] 缺少 LICENSE 或 THIRD_PARTY_NOTICES.md")

        # No stray nested .github/
        if (folder / ".github").exists():
            errors.append(f"[{pid}] 存在未清理的子 .github/ 目录")

        # README & docs guide check
        expected_link = f"{folder_name}/SKILL.md"
        if expected_link not in readme:
            errors.append(f"[{pid}] README.md 未链接到 {expected_link}")
        guide = ROOT / "docs" / "skills" / f"{pid}.md"
        if not guide.is_file():
            errors.append(f"[{pid}] 缺少中文使用指南 docs/skills/{pid}.md")

        # Root CI coverage
        if f'"{folder_name}"' not in ci_text:
            errors.append(f"[{pid}] 根 CI (.github/workflows/ci.yml) 未收录 {folder_name}")

    # Run schemas/contract_check.py
    cc = subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "schemas" / "contract_check.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if cc.returncode != 0:
        errors.append(f"[schemas/contract_check.py] 契约门禁失败：\n{cc.stdout}\n{cc.stderr}")

    if errors:
        print(f"FAIL: 共发现 {len(errors)} 项问题：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"PASS: 全部 {len(products)} 套技能包元数据、文档导航、许可声明、CI 矩阵与跨技能契约 100% 对齐！")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    catalog = load_catalog()
    print(f"Cabinet Revision: {catalog.get('cabinet_revision')} | State: {catalog.get('state')}")
    print(f"{'ID':26s} {'VERSION':12s} {'SKILL_NAME':28s} {'FOLDER'}")
    print("-" * 100)
    for p in catalog.get("products", []):
        fm = parse_frontmatter(ROOT / p["folder"] / "SKILL.md")
        print(f"{p['id']:26s} {p['version']:12s} {str(fm.get('name', '-')):28s} {p['folder']}")
    return 0


def _should_exclude(rel_parts: tuple[str, ...], file_name: str, folder_name: str) -> bool:
    for part in rel_parts[:-1]:
        if part in EXCLUDE_DIRS:
            # 保留 UMH 预置包内的只读目录结构
            if folder_name.startswith("全局记忆UMH") and part == "dist":
                continue
            return True
    if file_name in EXCLUDE_FILES:
        return True
    if any(file_name.endswith(suf) for suf in EXCLUDE_SUFFIXES):
        return True
    return False


def pack_product(product: dict, out_dir: Path) -> tuple[Path, str, int]:
    folder = ROOT / product["folder"]
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{product['folder']}.zip"
    count = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(folder)
            if _should_exclude(rel.parts, path.name, product["folder"]):
                continue
            arcname = f"{product['folder']}/{rel.as_posix()}"
            info = zipfile.ZipInfo(arcname, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
            count += 1
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    return zip_path, digest, count


def cmd_pack(args: argparse.Namespace) -> int:
    catalog = load_catalog()
    products = catalog.get("products", [])
    if args.id:
        targets = [p for p in products if p["id"] == args.id]
        if not targets:
            print(f"未找到技能 id: {args.id}", file=sys.stderr)
            return 2
    elif args.all:
        targets = products
    else:
        print("请指定 --all 或 --id <技能ID>", file=sys.stderr)
        return 2

    out_dir = Path(args.out) if Path(args.out).is_absolute() else (ROOT / args.out)
    sums: list[str] = []
    for p in targets:
        zp, sha, cnt = pack_product(p, out_dir)
        size_kb = zp.stat().st_size / 1024.0
        sums.append(f"{sha}  {zp.name}")
        print(f"  PACKED {zp.name:38s} | {cnt:4d} files | {size_kb:8.1f} KB | {sha[:12]}")

    sum_file = out_dir / "SHA256SUMS.txt"
    sum_file.write_text("\n".join(sums) + "\n", encoding="utf-8", newline="\n")
    print(f"完成：已输出 {len(targets)} 个干净分发包及校验清单 -> {sum_file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="自制 Agent 技能柜根级校验与分发打包工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="全仓元数据、许可、CI 矩阵与跨技能契约体检")
    sub.add_parser("status", help="输出全部公开技能状态总览")

    p_pack = sub.add_parser("pack", help="生成干净可分发 ZIP 压缩包与 SHA256SUMS")
    p_pack.add_argument("--all", action="store_true", help="打包全部公开技能")
    p_pack.add_argument("--id", help="仅打包指定 catalog id")
    p_pack.add_argument("--out", default="dist", help="输出目录（默认 dist/）")

    args = parser.parse_args(argv)
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "pack":
        return cmd_pack(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
