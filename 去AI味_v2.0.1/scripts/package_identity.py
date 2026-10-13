#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""package_identity.py - 运行包身份与版本指纹的唯一实现（工单 C3.3）。

为什么单独成模块：构建器、安装校验器和冻结测试都要算同一个摘要，
复制三份实现迟早会分叉，导致“指纹对不上但没人发现”。

规范：
  - 版本唯一权威来源是开发源 SKILL.md frontmatter 的 metadata.version；
    构建器从那里读取，不得在第二个文件里硬编码第二个权威版本。
  - package_digest 只覆盖 release-manifest.json 的 files 列表：
    按相对路径排序后，逐条拼接 `path + NUL + bytes + NUL + sha256 + LF`，
    再对整体 UTF-8 字节求 SHA-256。
  - 文件时间戳、绝对路径、JSON 缩进一律不参与摘要，保证跨机可复现。

本文件下半部分是 v1.2 的**身份链**层（先修问题第 2/6/7 项）：历史证据与当前包
身份分家。为什么必须分家：2026-09-26 冻结的三个 run 记的是**当时安装版**的字节，
安装版之后被手改（frontmatter `name` 换成中文），拿当前安装版去核历史 run 就会
把「历史证据」判成被删改，而真正的漂移位置反而没人负责。两条链的分工：

  历史链 verify_historical_run()
    run 记录自身冻结字段 + run 目录内冻结输入哈希 + **同版本历史包**的字节。
    输入里没有安装目录这一项，签名上就没法拿当前安装版去核历史 run。
  当前链 verify_current_chain()
    开发源 → 发布候选 → 安装版，逐字段比版本与运行文件集合摘要。运行文件集合的
    权威清单在 scripts/runtime_contract.py（Task 4），不由被核对的包自己提供。

``PackageIdentity.origin`` 的取值描述的是**工件种类**：historical-record 是 run
底账里的冻结记录，source 是开发源文件树，candidate 是构建出的发布包（历史包在磁盘
上也是这种形态），installed 是安装目录。哪一份证据被拿来做字节核对，一律同时写进
``IdentityCheck.reason``，排查时不必反推。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MANIFEST_NAME = "release-manifest.json"
VERSION_RE = re.compile(r"^\s+version:\s*(\S+)\s*$", re.MULTILINE)
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_skill_version(skill_md: Path) -> str:
    """从 SKILL.md frontmatter 读取 metadata.version（唯一权威版本来源）。"""
    text = Path(skill_md).read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise SystemExit(f"[STOP] SKILL.md 缺少标准 frontmatter，无法确定版本: {skill_md}")
    block = m.group(1)
    if re.search(r"^version:", block, re.MULTILINE):
        raise SystemExit(f"[STOP] frontmatter 顶层出现 version，违反官方 schema: {skill_md}")
    vm = VERSION_RE.search(block)
    if not vm:
        raise SystemExit(f"[STOP] frontmatter 缺少 metadata.version: {skill_md}")
    version = vm.group(1).strip().strip('"\'')
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        raise SystemExit(f"[STOP] 版本号格式不合法: {version}")
    return version


STAGE_SECTION = "[tool.lemon.v2]"
STAGE_DECL_RE = re.compile(r'^release_stage\s*=\s*"([^"]*)"[ \t]*$', re.MULTILINE)
DEFAULT_STAGE_RE = re.compile(r'^DEV_RELEASE_STAGE = "([^"]*)"', re.MULTILINE)


def read_release_stage(source_root: Path) -> str:
    """构建候选时的阶段权威：先读 pyproject 的 ``[tool.lemon.v2]`` 声明，再读源树里
    ``scripts/v2_task.py`` 的默认值。

    这两处本来就是钉死成一对的权威（见 tests/v2/test_release_stage_default.py），所以：
    都读不到就停（不留空、不猜、也不在构建器里写死阶段名——那是第三份权威）；
    两处都读到却不同字也停（那正是「版本三权威」那类老毛病的形状）。
    只有一处读到时用它：合成源树（回归用例里的手工夹具）通常没有 pyproject，
    但带着 scripts/v2_task.py，那一份默认值就是它要发布的阶段。
    """
    root = Path(source_root)
    declared = None
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8")
        if STAGE_SECTION in text:
            section = text.split(STAGE_SECTION, 1)[1]
            nxt = section.find("\n[")               # 只在本段内找，别读到别的表
            match = STAGE_DECL_RE.search(section if nxt < 0 else section[:nxt])
            if match:
                declared = match.group(1)
    shipped = None
    task_file = root / "scripts" / "v2_task.py"
    if task_file.is_file():
        match = DEFAULT_STAGE_RE.search(task_file.read_text(encoding="utf-8"))
        if match:
            shipped = match.group(1)
    if declared is None and shipped is None:
        raise SystemExit(f"[STOP] 既没有 pyproject 的 release_stage 声明，也读不到 "
                         f"scripts/v2_task.py 的默认阶段，无法确定发布阶段: {root}")
    if declared is not None and shipped is not None and declared != shipped:
        raise SystemExit(f"[STOP] 阶段两处权威不一致：pyproject {declared!r} ≠ "
                         f"v2_task {shipped!r}")
    return declared if declared is not None else shipped


def package_digest(entries) -> str:
    """entries: 可迭代的 {"path","bytes","sha256"}。返回规范化集合摘要。

    条目缺字段、bytes 不是非负整数、sha256 形状不对一律明确报错，
    避免把损坏的 manifest 摘要成一个“看起来正常”的哈希。
    """
    items = []
    seen = set()
    for e in entries:
        try:
            rel = str(e["path"])
            nbytes = e["bytes"]
            sha = str(e["sha256"])
        except (TypeError, KeyError) as exc:
            raise ValueError(f"manifest 文件条目缺少必需字段: {e!r}") from exc
        if isinstance(nbytes, bool) or not isinstance(nbytes, int) or nbytes < 0:
            raise ValueError(f"{rel} 的 bytes 必须是非负整数，实际: {nbytes!r}")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            raise ValueError(f"{rel} 的 sha256 形状不合法: {sha!r}")
        if rel in seen:
            raise ValueError(f"manifest 出现重复路径: {rel}")
        seen.add(rel)
        items.append((rel, nbytes, sha.lower()))
    if not items:
        raise ValueError("files 列表为空，拒绝计算 package_digest")
    items.sort(key=lambda t: t[0])
    payload = bytearray()
    for rel, nbytes, sha in items:
        payload += f"{rel}\x00{nbytes}\x00{sha}\n".encode("utf-8")
    return hashlib.sha256(bytes(payload)).hexdigest()


def load_release_manifest(root: Path) -> dict:
    path = Path(root) / MANIFEST_NAME
    if not path.is_file():
        raise FileNotFoundError(f"缺少 {MANIFEST_NAME}: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def identity_of(root: Path) -> dict:
    """返回一个已安装/已构建运行包的完整身份指纹。

    任一必需字段拿不到就**明确失败**，绝不返回 None 让调用方把“缺数据”当成
    “已绑定”——那会让 run 底账被静默写成 null 指纹。
    """
    root = Path(root)
    if not root.is_dir():
        raise SystemExit(f"[STOP] 运行包目录不存在: {root}")
    manifest = load_release_manifest(root)

    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        raise SystemExit(f"[STOP] release-manifest.json 缺少可用 version 字段: {root}")

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise SystemExit(f"[STOP] release-manifest.json 未声明任何文件: {root}")

    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        raise SystemExit(f"[STOP] 运行包缺少 SKILL.md，无法确定主入口指纹: {root}")

    return {
        "root": str(root),
        "target_version": version,
        "skill_frontmatter_version": read_skill_version(skill_md),
        "target_skill_sha256": sha256_file(skill_md),
        "target_release_manifest_sha256": sha256_file(root / MANIFEST_NAME),
        "target_package_digest": package_digest(files),
        "declared_files": len(files),
        "declared_bytes": sum(int(e["bytes"]) for e in files),
    }


# ==========================================================================
# 身份链层（v1.2 先修问题第 2/6/7 项）：历史证据与当前包身份分家
# ==========================================================================

PACKAGE_ORIGINS = ("historical-record", "source", "candidate", "installed")
CHECK_STATUSES = ("PASS", "FAIL", "SKIP")
STATUS_PASS, STATUS_FAIL, STATUS_SKIP = CHECK_STATUSES
# 设计方案点名的报告口径：历史包不可用必须说成「历史字节核验未执行」，不是通过。
HISTORICAL_BYTES_NOT_EXECUTED = "历史字节核验未执行"
CURRENT_BYTES_NOT_EXECUTED = "当前链字节核验未执行"

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR_NAME = "dist"
PACKAGE_DIR_PREFIX = "lieflat-less-ai-tone"
HISTORICAL_PACKAGE_ENV = "LIEFLAT_HISTORICAL_PACKAGE"
SHA256_FULL_RE = re.compile(r"[0-9a-fA-F]{64}")
# run 底账里那四项目标身份字段（与 bind_run_targets.BOUND_FIELDS 同集合）
RUN_IDENTITY_FIELDS = (
    "target_version",
    "target_skill_sha256",
    "target_release_manifest_sha256",
    "target_package_digest",
)


@dataclass(frozen=True)
class PackageIdentity:
    """一份包身份的不可变记录：版本 + 运行文件集合摘要 + 它来自哪一类工件。"""

    version: str
    tree_sha256: str
    origin: Literal["historical-record", "source", "candidate", "installed"]

    def __post_init__(self):
        # PACKAGE_ORIGINS 由本校验变成 load-bearing 约束（Task 2/3 carried (c)）：
        # 只被测试引用的常量等于没写，代码里飘出一个拼错的 origin 没人拦得住，
        # 而身份报告一旦带着未知工件种类进入验收记录，读的人无从判断它是什么。
        if self.origin not in PACKAGE_ORIGINS:
            raise ValueError(
                f"未知包身份来源 {self.origin!r}（允许 {PACKAGE_ORIGINS}）")


@dataclass(frozen=True)
class IdentityCheck:
    """一次身份核对的结论。SKIP 表示该项**没有执行**，绝不等于通过。"""

    status: Literal["PASS", "FAIL", "SKIP"]
    expected: PackageIdentity
    actual: PackageIdentity | None
    reason: str

    def __post_init__(self):
        # 三态之外的值一律拒绝：任何新造的「差不多算通过」的状态都会从聚合层漏出去
        if self.status not in CHECK_STATUSES:
            raise ValueError(
                f"未知核对状态 {self.status!r}（允许 {CHECK_STATUSES}；SKIP=未执行）")


def _label(identity: PackageIdentity) -> str:
    return f"{identity.origin} v{identity.version} 摘要 {identity.tree_sha256[:12]}…"


def _manifest_files(root: Path):
    """取 (manifest, version, files)；结构不对一律明确失败，不返回半成品。"""
    manifest = load_release_manifest(root)
    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        raise SystemExit(f"[STOP] release-manifest.json 缺少可用 version 字段: {root}")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise SystemExit(f"[STOP] release-manifest.json 未声明任何文件: {root}")
    return manifest, version, files


def declared_entries(root: Path) -> list:
    return _manifest_files(Path(root))[2]


def declared_identity(root: Path, origin: str) -> PackageIdentity:
    """按 release-manifest.json 的**声明**构造身份（这个包声称自己是什么）。"""
    _, version, files = _manifest_files(Path(root))
    return PackageIdentity(version, package_digest(files), origin)


def tree_identity(root: Path, rel_paths, origin: str) -> PackageIdentity:
    """按磁盘实际字节构造身份：版本取 SKILL.md frontmatter，摘要取真实文件哈希。

    清单里缺的文件按 64 个 0 计入摘要位，让「缺件」稳定表现为摘要不符，
    而不是让整条链半路抛错、看不出是哪一份包在漂移。
    SKILL.md 本身缺失或 frontmatter 不合法时按模块规范直接 SystemExit。
    """
    root = Path(root)
    version = read_skill_version(root / "SKILL.md")
    entries = []
    for rel in rel_paths:
        path = root / rel
        present = path.is_file()
        entries.append({
            "path": str(rel),
            "bytes": path.stat().st_size if present else 0,
            "sha256": sha256_file(path) if present else "0" * 64,
        })
    return PackageIdentity(version, package_digest(entries), origin)


def file_set_diff(root: Path, entries) -> list:
    """逐条比对声明哈希与磁盘字节，返回「哪里不符」的清单，只用于失败原因。"""
    root = Path(root)
    problems = []
    for entry in entries:
        rel = str(entry["path"])
        path = root / rel
        if not path.is_file():
            problems.append(f"{rel} 缺失")
        elif sha256_file(path).lower() != str(entry["sha256"]).lower():
            problems.append(f"{rel} 字节不一致")
    return problems


def byte_diff(root_a: Path, root_b: Path, rel_paths) -> list:
    """两棵文件树在同一批相对路径上的字节差异清单（只用于写失败原因）。"""
    problems = []
    for rel in sorted(set(str(p) for p in rel_paths)):
        a, b = Path(root_a) / rel, Path(root_b) / rel
        if a.is_file() != b.is_file():
            problems.append(f"{rel} 只在一侧存在")
        elif a.is_file() and sha256_file(a) != sha256_file(b):
            problems.append(f"{rel} 字节不一致")
    return problems


def run_record_problems(run_manifest: dict, run_dir=None) -> list:
    """run 记录**自身**可核的部分：字段齐备、形状合法、内部不打架、冻结输入可复算。

    这里一行都不读安装目录——当前安装版漂移不该由历史证据负责（先修问题第 6 项）。
    """
    problems = []
    for field in RUN_IDENTITY_FIELDS:
        value = run_manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"冻结字段缺失或为空: {field}")
        elif field != "target_version" and not SHA256_FULL_RE.fullmatch(value):
            problems.append(f"冻结字段不是 SHA-256: {field}")
    version = run_manifest.get("version_under_test")
    if version is not None and version != run_manifest.get("target_version"):
        problems.append(
            f"version_under_test={version} 与 target_version="
            f"{run_manifest.get('target_version')} 自相矛盾")
    if run_dir is not None:
        extra = run_manifest.get("extra_info") or {}
        for key, declared in sorted(extra.items()):
            if not key.endswith("_file_sha256"):
                continue
            rel = extra.get(key[: -len("_sha256")])
            if not rel:
                problems.append(f"{key} 缺少配对的相对路径字段")
                continue
            path = Path(run_dir) / str(rel)
            if not path.is_file():
                problems.append(f"冻结输入文件缺失: {rel}")
            elif sha256_file(path).lower() != str(declared).lower():
                problems.append(f"冻结输入哈希不可复算: {rel}")
    return problems


def historical_package_problems(root: Path) -> list:
    """历史包得先自证清白（声明与自己的字节对得上、版本自洽），才配拿来核历史 run。"""
    root = Path(root)
    try:
        manifest, version, files = _manifest_files(root)
    except (OSError, ValueError, SystemExit) as exc:
        return [f"release-manifest.json 不可用: {exc}"]
    try:
        recomputed = package_digest(files)
    except ValueError as exc:
        return [f"files 列表无法摘要: {exc}"]
    problems = []
    if manifest.get("package_digest") != recomputed:
        problems.append("package_digest 与自身声明集合不符")
    problems += file_set_diff(root, files)
    skill = root / "SKILL.md"
    if not skill.is_file():
        problems.append("缺少 SKILL.md")
        return problems
    try:
        frontmatter = read_skill_version(skill)
    except SystemExit as exc:
        problems.append(f"SKILL.md 版本不可读: {exc}")
    else:
        if frontmatter != version:
            problems.append(f"manifest 版本 {version} 与 frontmatter {frontmatter} 不一致")
    return problems


def find_historical_package(version: str, dev_root=None) -> Path | None:
    """为某个历史版本定位可核验的发布包；找不到返回 None，绝不猜相似目录。

    只认两处：显式环境变量 ``LIEFLAT_HISTORICAL_PACKAGE``，以及本仓 ``dist/`` 下的
    ``lieflat-less-ai-tone-<version>``。目录里必须有 release-manifest.json 且其
    version 正好是这一档——拿别的版本的字节来核你，比不核更危险。
    """
    roots = []
    explicit = os.environ.get(HISTORICAL_PACKAGE_ENV)
    if explicit and str(explicit).strip():
        roots.append(Path(explicit).expanduser())
    base = Path(dev_root) if dev_root else REPO_ROOT
    roots.append(base / DIST_DIR_NAME / f"{PACKAGE_DIR_PREFIX}-{version}")
    for root in roots:
        try:
            found = load_release_manifest(root).get("version")
        except (OSError, ValueError):
            continue
        if str(found) == str(version):
            return root.resolve()
    return None


def verify_historical_run(run_manifest: dict, run_dir=None,
                          historical_package=None) -> IdentityCheck:
    """历史 run 只与自身冻结字段 + 同版本历史包核对；**绝不与当前安装版比较**。

    三种结论：
      PASS —— 记录自洽、run 目录内冻结输入哈希复算得回来，且与已自证的同版本历史包
              逐字段一致；
      FAIL —— 记录被改坏、冻结输入哈希复算不回来，或与历史包不符；
      SKIP —— 历史包不可用，只完成了记录自检；reason 明确写「历史字节核验未执行」。
              调用方不得把 SKIP 当成通过。
    """
    version = str(run_manifest.get("target_version") or "")
    expected = PackageIdentity(
        version, str(run_manifest.get("target_package_digest") or ""), "historical-record")

    problems = run_record_problems(run_manifest, run_dir)
    if problems:
        return IdentityCheck(STATUS_FAIL, expected, None,
                             "run 冻结记录自检失败：" + "；".join(problems))
    if historical_package is None:
        return IdentityCheck(
            STATUS_SKIP, expected, None,
            f"{HISTORICAL_BYTES_NOT_EXECUTED}（未提供 v{version} 历史包）；"
            "已执行的只有冻结记录自检与 run 目录内冻结输入哈希复算")

    package = Path(historical_package)
    package_problems = historical_package_problems(package)
    if package_problems:
        return IdentityCheck(
            STATUS_FAIL, expected, None,
            f"历史包 {package} 不可信，拒绝据此核验字节：" + "；".join(package_problems))

    actual = declared_identity(package, "candidate")
    mismatches = []
    if actual.version != expected.version:
        mismatches.append(f"版本 记录 {expected.version} vs 历史包 {actual.version}")
    if actual.tree_sha256 != expected.tree_sha256:
        mismatches.append(
            "target_package_digest 与历史包声明集合不符："
            f"记录 {expected.tree_sha256[:12]}… vs 历史包 {actual.tree_sha256[:12]}…")
    for field, rel in (("target_skill_sha256", "SKILL.md"),
                       ("target_release_manifest_sha256", MANIFEST_NAME)):
        recorded = str(run_manifest.get(field) or "").lower()
        if sha256_file(package / rel).lower() != recorded:
            mismatches.append(f"{field} 与历史包 {rel} 实际字节不符")
    if mismatches:
        return IdentityCheck(
            STATUS_FAIL, expected, actual,
            f"记录与 v{actual.version} 历史包（{package}）不符：" + "；".join(mismatches))
    return IdentityCheck(
        STATUS_PASS, expected, actual,
        f"已与自洽的 v{actual.version} 历史包（{package}）逐字段核对通过；"
        f"run 目录冻结输入哈希复算：{'通过' if run_dir is not None else '未提供 run 目录'}")


def _compare_identity(expected: PackageIdentity, actual: PackageIdentity,
                      details=()) -> IdentityCheck:
    """只比版本与文件集合摘要——origin 描述工件种类，本来就该不同，不参与比较。"""
    if expected.version == actual.version and expected.tree_sha256 == actual.tree_sha256:
        return IdentityCheck(STATUS_PASS, expected, actual, f"一致：{_label(actual)}")
    bits = []
    if expected.version != actual.version:
        bits.append(f"版本 {expected.version} vs {actual.version}")
    if expected.tree_sha256 != actual.tree_sha256:
        bits.append(f"运行文件集合摘要 {expected.tree_sha256[:12]}… vs "
                    f"{actual.tree_sha256[:12]}…")
    return IdentityCheck(STATUS_FAIL, expected, actual, "；".join(bits + list(details)))


def authoritative_runtime_files() -> list:
    """当前链要比的运行文件集合：唯一权威是 scripts/runtime_contract.py。

    刻意不在模块顶部 import：runtime_contract 侧的夹具又要按需加载本模块的构建器，
    顶部互导会成环。这里延迟到调用点取，取不到就是环境坏了，直接抛，不静默回落。
    """
    here = str(REPO_ROOT / "scripts")
    if here not in sys.path:
        sys.path.insert(0, here)
    from runtime_contract import REQUIRED_RUNTIME_FILES
    return list(REQUIRED_RUNTIME_FILES)


def verify_current_chain(source_root: Path, candidate=None,
                         installed=None, runtime_files=None) -> dict:
    """当前包身份链：开发源 → 发布候选 → 安装版。**不读任何历史 run 记录**。

    固定返回五条核对（缺输入的按 SKIP 记录，不静默消失）：
      安装版、发布候选各自的「manifest 声明 == 磁盘字节」，
      开发源→安装版、开发源→发布候选、发布候选→安装版的版本与运行文件集合摘要。

    ``runtime_files`` 是这套比对的**文件集合权威**，取自 scripts/runtime_contract.py：
    旧实现用被比较包自己的 release-manifest.json 当权威（carried (d)），后果是
    「少声明几个运行文件、再把摘要重签到自洽」这种谎报包永远能过——声明集合与磁盘
    字节都由同一份声明算出来，必然自洽。清单由外部登记，包自己说了不算。
    """
    source_root = Path(source_root)
    installed = Path(installed) if installed else None
    candidate = Path(candidate) if candidate else None
    if installed is None and candidate is None:
        raise SystemExit(
            "[STOP] verify_current_chain 需要 installed 或 candidate 至少一个："
            "运行文件集合的权威来自 scripts/runtime_contract.py")
    rel_paths = [str(r) for r in (runtime_files if runtime_files is not None
                                  else authoritative_runtime_files())]
    if not rel_paths:
        raise SystemExit("[STOP] 运行文件集合权威为空，拒绝据此核对当前链")
    reference = installed or candidate

    def source_view(pkg: Path) -> PackageIdentity:
        """开发源按权威清单、在磁盘上复算出的身份。"""
        return tree_identity(source_root, rel_paths, "source")

    def disk_view(pkg: Path, origin: str) -> PackageIdentity:
        return tree_identity(pkg, rel_paths, origin)

    def not_executed(label: str, extra: str = "") -> IdentityCheck:
        return IdentityCheck(
            STATUS_SKIP, source_view(reference), None,
            f"{CURRENT_BYTES_NOT_EXECUTED}（未提供{label}{extra}）")

    checks = {}
    for label, pkg, origin in (("安装版", installed, "installed"),
                               ("发布候选", candidate, "candidate")):
        name = f"当前链：{label}身份自洽（manifest 声明 == 磁盘字节）"
        if pkg is None:
            checks[name] = not_executed(label)
        else:
            checks[name] = _compare_identity(
                declared_identity(pkg, origin), disk_view(pkg, origin),
                details=file_set_diff(pkg, declared_entries(pkg)))

    for label, pkg, origin in (("安装版", installed, "installed"),
                               ("发布候选", candidate, "candidate")):
        name = f"当前链：开发源 → {label}（版本与运行文件集合摘要）"
        if pkg is None:
            checks[name] = not_executed(label)
        else:
            checks[name] = _compare_identity(
                source_view(pkg), disk_view(pkg, origin),
                details=byte_diff(source_root, pkg, rel_paths))

    name_pairwise = "当前链：发布候选 → 安装版（同一份包字节）"
    if candidate is None or installed is None:
        missing = "发布候选" if candidate is None else "安装版"
        checks[name_pairwise] = not_executed(missing, "，无法比对同一份包字节")
    else:
        checks[name_pairwise] = _compare_identity(
            disk_view(candidate, "candidate"), disk_view(installed, "installed"),
            details=byte_diff(candidate, installed, rel_paths))
    return checks


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="打印一个运行包的版本与规范化身份指纹")
    ap.add_argument("root")
    args = ap.parse_args()
    print(json.dumps(identity_of(Path(args.root)), ensure_ascii=False, indent=2))
