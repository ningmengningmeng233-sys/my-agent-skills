#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""候选发布打包（F5 + 工单04-Task4）：白名单复制 → 敏感扫描 → 双 profile → SHA-256 → zip。

原则：
- 白名单打包（不依赖 .gitignore）：只收运行/验收所需目录与文件。
- **私人完整包与受控分享包分别构建、名称不同**：分享包按 `references/styles/share-policy.toml`
  排除来源图与许可未确认文件；缺运行必需参考时在包内声明
  `VISUAL_STYLE_INCOMPLETE_FOR_SHARE`，绝不声称与私人包质量等价。
- 压缩包写到仓库外（--out 不得在 --source 之内）。
- 发布敏感物零容忍：`.env`、真实密钥、绝对桌面路径、生成缓存、用户资产库、
  未登记图片——任一命中即中止，绝不写入 zip。

用法：
  python scripts/build_release.py --out <目录> --profile private
  python scripts/build_release.py --out <目录> --profile shareable [--source <副本>]
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
sys.path.insert(0, str(SKILL_DIR))

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 回退
    import tomli as tomllib

from scripts import skill_meta  # noqa: E402

WHITELIST = [
    "SKILL.md", "README.md", "QUICKSTART.md", "FAQ.md", "CONTEXT.md",
    "INSTALL.md",
    "pyproject.toml", "requirements.txt", ".env.example", ".flake8",
    # 根级交付物必须逐个点名，否则后来加在仓库根的脚本会静默不入包（缺陷 F34：
    # T9 的 run_pipeline 与 T13 的节点池样例都曾这样漏掉）。守卫见
    # 测试/unit/test_release_ships_root_deliverables.py
    "run_pipeline.sh", "run_pipeline.bat", "llm_nodes.json.example",
    # F-22：许可正文是根级无扩展名交付件，曾因 WHITELIST 没点名而静默不入包
    # （frontmatter 写着 license: Proprietary，收件人手里却没有条款正文）。守卫见
    # 测试/unit/test_release_ships_root_deliverables.py
    "LICENSE",
    "vendor",
    "docs", "references", "scripts", "templates", "服饰设计", "测试",
]
EXCLUDE_FILES = {".env", ".asset_state.db", "交付清单.json", "母版冲突待审.md"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".log")
EXCLUDE_PARTS = {"__pycache__", ".git", ".pytest_cache", ".idea", ".vscode"}
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")

PROFILES = ("private", "shareable")
INCOMPLETE_DECLARATION = "VISUAL_STYLE_INCOMPLETE_FOR_SHARE"
STYLE_ROOT = ("references", "styles")
# 真实密钥判定：长度下限取 30（真实密钥 ≥35 位），并对显式占位串放行——
# 夹具里的 sk-fake-key-… / GEMINI_API_KEY=should-not-be-copied 属占位值，不得阻断发布。
SECRET_VALUE_MIN = 30
PLACEHOLDER_HINTS = ("fake", "example", "placeholder", "changeme", "dummy", "your",
                     "should-not", "should_not", "xxx", "***", "test-key", "not-a-real")
SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_\-]{%d,}" % SECRET_VALUE_MIN),
    re.compile(r"sk-[A-Za-z0-9_-]{%d,}" % SECRET_VALUE_MIN),
    re.compile(
        r"(?:GEMINI_API_KEY|OPENAI_API_KEY|GOOGLE_API_KEY)\s*[=:]\s*[\"']?[A-Za-z0-9_\-]{%d,}"
        % SECRET_VALUE_MIN),
)


class ReleasePreflightError(RuntimeError):
    """发布前置校验失败：code 为稳定原因码。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


def _looks_like_placeholder(value: str) -> bool:
    low = value.lower()
    return any(hint in low for hint in PLACEHOLDER_HINTS)


DESKTOP_PATH_PATTERN = re.compile(r"[A-Za-z]:\\+Users\\+[^\\\s]+\\+Desktop")
TEXT_SCAN_SUFFIXES = (".py", ".md", ".toml", ".json", ".txt", ".yaml", ".yml", ".cfg", ".ini")


def load_share_policy(source: Path) -> dict:
    path = Path(source) / "references" / "styles" / "share-policy.toml"
    if not path.is_file():
        raise SystemExit(f"❌ 缺少分享策略文件: {path}")
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _excluded(rel_parts: tuple[str, ...], version: str = "") -> bool:
    """路径感知排除：开发资产库/快照/e2e 排除；测试夹具（目录名可为资产库）是验收必需品。

    另排除**本版本的交付报告**（`docs/验收/v<版本>交付报告.md`）：报告要记录发布包自身的
    SHA-256 与文件数，若它随包分发就形成「包内容依赖包哈希」的循环，永远无法定稿。
    历史版本的报告不描述本包哈希，可以照常随包分发。
    """
    rel = "/".join(rel_parts)
    if set(rel_parts) & EXCLUDE_PARTS:
        return True
    if rel == "测试/e2e" or rel.startswith("测试/e2e/"):
        return True
    if "资产库备份" in rel_parts:
        return True
    if "资产库" in rel_parts and not rel.startswith("测试/fixtures/"):
        return True
    if version and len(rel_parts) >= 3 and rel_parts[0] == "docs" and rel_parts[1] == "验收":
        if rel_parts[-1] == f"v{version}交付报告.md":
            return True
    return False


def _registered_media(source: Path) -> dict:
    """风格包 manifest 登记过的媒体 → {仓库相对路径: share_allowed}。

    没登记的图片没有许可依据，分享包不放行；登记为 share_allowed=false 的同样排除。
    """
    registered: dict[str, bool] = {}
    styles = Path(source) / "references" / "styles"
    if not styles.is_dir():
        return registered
    for manifest in sorted(styles.glob("*/*/manifest.toml")):
        try:
            doc = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        for base in (manifest.parent / "private", manifest.parent):
            for item in doc.get("files", []) or []:
                rel = str((item or {}).get("path", "")).replace("\\", "/")
                if not rel:
                    continue
                repo_rel = (base / rel).resolve().relative_to(Path(source).resolve()).as_posix()
                registered[repo_rel] = bool(item.get("share_allowed", False))
    return registered


# 兜底清单：策略文件缺键时按最严口径处理，宁可少发也不误发用户数据。
MIN_EXCLUDE_SUFFIXES = (".log", ".pyc", ".sqlite", ".sqlite3", ".db")
MIN_EXCLUDE_SUBSTRINGS = ("用户库", "原稿", "私藏", "token", "cookie", "secret")
MIN_MEDIA_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def _policy_list(policy: dict, key: str, minimum) -> list:
    if key in policy and policy[key] is not None:
        return list(policy[key])
    return list(minimum)


def _shareable_excluded(rel: str, policy: dict, registered_media: dict | None = None) -> str:
    """分享包排除判定：返回排除原因（空串表示保留）。"""
    for prefix in policy.get("shareable_exclude_prefixes", []):
        if rel.startswith(prefix):
            return "来源图/许可未确认文件（分享策略排除）"
    if Path(rel).name in set(policy.get("shareable_exclude_names", [])):
        return "分享策略排除的登记文件"
    suffixes = tuple(_policy_list(policy, "shareable_exclude_suffixes", MIN_EXCLUDE_SUFFIXES))
    if suffixes and rel.lower().endswith(suffixes):
        return "运行期/本地库文件按分享策略排除"
    exempt = set(_policy_list(policy, "shareable_exclude_exempt", ()))
    if rel not in exempt:
        for token in _policy_list(policy, "shareable_exclude_name_substrings",
                                  MIN_EXCLUDE_SUBSTRINGS):
            if token and token in rel:
                return f"用户数据或敏感路径（含 {token}）不得随分享包分发"
    media = tuple(_policy_list(policy, "shareable_media_suffixes", MIN_MEDIA_SUFFIXES))
    if media and rel.lower().endswith(media):
        for prefix in _policy_list(policy, "shareable_media_exempt_prefixes", ()):
            if prefix and rel.startswith(prefix):
                return ""  # 技能自产测试夹具：不是风格包参考图，无需第三方许可依据
        allowed = (registered_media or {}).get(rel)
        if allowed is None:
            return "媒体未在风格包 manifest 登记，缺许可依据"
        if not allowed:
            return "登记为不可分享（share_allowed=false）"
    return ""


def _skill_version(source: Path) -> str:
    text = (source / "SKILL.md").read_text(encoding="utf-8")
    try:
        return skill_meta.skill_version(text)
    except ValueError as exc:
        raise SystemExit(f"❌ {exc}，无法命名发布包")


def plan_staging(source: Path, profile: str = "private",
                 policy: dict | None = None) -> tuple[list[Path], list[dict]]:
    """按 profile 暂存文件；返回 (文件, 分享策略排除项)。私有 profile 不需要策略文件。"""
    if profile == "shareable" and policy is None:
        policy = load_share_policy(source)
    policy = policy or {}
    # 分享包里每张图都要有许可依据：登记表一次算好复用
    registered_media = _registered_media(source) if profile == "shareable" else {}
    version = _skill_version(source)
    files: list[Path] = []
    excluded: list[dict] = []
    for name in WHITELIST:
        root = source / name
        if not root.exists():
            # 白名单点名的**根级具名文件**缺失一律中止：run_pipeline.bat 曾这样从分享包里
            # 无声消失（F34/F-22 同一形状），旧代码只对 5 个名字报错，其余静默 continue。
            # 目录类条目（无扩展名）仍按可选处理，不改「视觉包是可选下游模块」的老口径。
            if "." in name or name in ("LICENSE", "VERSION", "NOTICE"):
                raise SystemExit(f"❌ 白名单条目缺失: {name}")
            continue
        if root.is_file():
            files.append(root)
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            rel_parts = tuple(p.relative_to(source).parts)
            rel = "/".join(rel_parts)
            if _excluded(rel_parts, version):
                if rel_parts[-1] == f"v{version}交付报告.md":
                    excluded.append({"path": rel,
                                     "reason": "本版本交付报告需记录发布包哈希，"
                                               "随包分发会形成自引用循环"})
                continue
            if p.name in EXCLUDE_FILES or p.suffix in EXCLUDE_SUFFIXES:
                continue
            if profile == "shareable":
                reason = _shareable_excluded(rel, policy, registered_media)
                if reason:
                    excluded.append({"path": rel, "reason": reason})
                    continue
            files.append(p)
    return files, excluded


def _staged_files(source: Path, profile: str = "private",
                  policy: dict | None = None) -> list[Path]:
    """兼容入口：只返回暂存文件列表（分享策略排除项见 plan_staging）。"""
    return plan_staging(source, profile, policy)[0]


def _registered_pack_paths(source: Path, images: bool) -> set[str]:
    """内置风格包 manifest 中登记的相对路径：images=True 收图片（用于"未登记图片"检查），
    False 收其余文件即 Markdown（用于完整度核对）。"""
    registered: set[str] = set()
    styles_root = source.joinpath(*STYLE_ROOT)
    if not styles_root.is_dir():
        return registered
    for manifest in styles_root.rglob("manifest.toml"):
        try:
            with open(manifest, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, ValueError):
            continue
        # 清单里的路径相对"内容根"（<版本>/private/）解析；同时兼容直接放版本根的老布局
        for base in (manifest.parent / "private", manifest.parent):
            for entry in data.get("files", []) or []:
                if not isinstance(entry, dict):
                    continue
                rel = str(entry.get("path") or "")
                if (Path(rel).suffix.lower() in IMAGE_SUFFIXES) != images:
                    continue
                candidate = base / rel
                if candidate.is_file():
                    registered.add(candidate.relative_to(source).as_posix())
    return registered


def _primary_style(source: Path) -> tuple[str, str]:
    """登记表中已验证风格包（首个 omni_verified）的名称与版本；缺失时返回空串。"""
    path = Path(source) / "references" / "styles" / "registry.toml"
    if not path.is_file():
        return "", ""
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return "", ""
    for row in data.get("styles", []) or []:
        if isinstance(row, dict) and row.get("validation_level") == "omni_verified":
            return str(row.get("pack_slug") or row.get("name") or ""), str(row.get("pack_version") or "")
    return "", ""


def _sensitive_scan(files: list[Path], source: Path) -> None:
    """敏感物扫描：文件级 + 内容级（密钥/桌面绝对路径）+ 未登记图片。"""
    bad: list[str] = []
    registered_images = _registered_pack_paths(source, images=True)
    for p in files:
        rel = p.relative_to(source).as_posix()
        if p.name == ".env" or (p.name.startswith(".env.") and p.name != ".env.example"):
            bad.append(f"{rel}（真实密钥文件）")
        if p.name == ".asset_state.db":
            bad.append(f"{rel}（断点库/生成缓存）")
        if _excluded(tuple(p.relative_to(source).parts)):
            bad.append(f"{rel}（排除物）")
        if p.suffix in (".pyc", ".pyo"):
            bad.append(f"{rel}（编译缓存）")
        if p.suffix.lower() in IMAGE_SUFFIXES and rel.startswith("references/styles/"):
            if rel not in registered_images:
                bad.append(f"{rel}（未登记进风格包 manifest 的图片）")
        if p.suffix in TEXT_SCAN_SUFFIXES and p.stat().st_size < 4 * 1024 * 1024:
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for pattern in SECRET_PATTERNS:
                matched = [m.group(0) for m in pattern.finditer(text)
                           if not _looks_like_placeholder(m.group(0))]
                if matched:
                    bad.append(f"{rel}（命中密钥字面值）")
                    break
            if DESKTOP_PATH_PATTERN.search(text):
                bad.append(f"{rel}（含绝对桌面路径）")
    if bad:
        raise SystemExit("❌ 发布敏感/排除物出现在暂存树，中止打包:\n  " + "\n  ".join(bad[:10]))


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def preflight_stylepack(source: Path, style_name: str = "") -> dict:
    """发布前置校验：权威风格包必须**逐文件**与清单一致（少一张图即失败）。

    用途：私有包必须包含完整视觉安装。若权威包存在但缺图/漂移，构建必须先失败，
    绝不允许静默产出一个「装不全」的包。

    若该源**根本没有登记风格包**（如仅含核心资料的裁剪副本），返回 `present=False`
    并跳过——这不是缺陷，视觉包本身是可选下游模块。
    """
    source = Path(source)
    styles_root = source.joinpath(*STYLE_ROOT)
    target_name = style_name or _primary_style(source)[0]
    version_dir = None
    if target_name:
        for candidate in sorted(styles_root.glob(f"{target_name}/*")):
            if (candidate / "private").is_dir():
                version_dir = candidate
                break
    if version_dir is None:
        # 未登记/未随源分发：不阻断（核心资料包无需视觉包），但如实报告缺失
        return {"present": False, "style": target_name, "version": "",
                "content_root": "", "file_count": 0, "image_count": 0,
                "content_sha256": ""}
    content_root = version_dir / "private"
    issues = _stylepack_verify(content_root, require_images=True)
    if issues:
        code = "STYLEPACK_INCOMPLETE" if any("缺失" in item for item in issues) \
            else "STYLEPACK_DRIFT"
        raise ReleasePreflightError(code, "；".join(issues[:3]))
    files = list(_stylepack_files(content_root))
    images = [rel for rel, _ in files if Path(rel).suffix.lower() in IMAGE_SUFFIXES]
    return {
        "present": True,
        "style": target_name,
        "version": version_dir.name,
        "content_root": str(content_root),
        "file_count": len(files),
        "image_count": len(images),
        "content_sha256": _stylepack_content_hash(content_root),
    }


def _stylepack_verify(content_root: Path, require_images: bool) -> list[str]:
    sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
    from engine.stylepack_verify import verify_pack

    return verify_pack(content_root, require_images=require_images)


def _stylepack_files(content_root: Path):
    sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
    from engine.stylepack_verify import iter_pack_files

    return iter_pack_files(content_root)


def _stylepack_content_hash(content_root: Path) -> str:
    sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
    from engine.stylepack_verify import pack_content_hash

    return pack_content_hash(content_root)


def build_release(source: Path, out_dir: Path, profile: str = "private") -> Path:
    if profile not in PROFILES:
        raise SystemExit(f"❌ 未知 profile: {profile}（只允许 {list(PROFILES)}）")
    source = Path(source).resolve()
    out_dir = Path(out_dir).resolve()
    if out_dir == source or source in out_dir.parents:
        raise SystemExit("❌ 发布输出目录必须在仓库之外")
    policy = load_share_policy(source)
    version = _skill_version(source)
    # 私有包必须可完整安装：先逐文件校验权威包，缺图即中止
    if profile == "private":
        pack_info = preflight_stylepack(source)
        if pack_info.get("present"):
            print(f"🧩 权威包前置校验通过：{pack_info['style']}@{pack_info['version']}"
                  f"（{pack_info['file_count']} 文件，含参考图 {pack_info['image_count']} 张）")
        else:
            print("ℹ️ 本源未随附权威风格包：跳过视觉包前置校验（核心资料包仍可发布）")
    files, excluded = plan_staging(source, profile, policy)
    _sensitive_scan(files, source)

    staged = {p.relative_to(source).as_posix() for p in files}
    required = list(policy.get("required_for_runtime", []))
    # 按路径后缀匹配：不硬编码某个风格名，任意风格包/测试夹具都适用
    missing_required = [rel for rel in required
                        if not any(s.endswith("/" + rel) for s in staged)]
    # 参考素材完整度：来源图按分享策略排除后，视觉参考层不再与私人包等价
    registered_assets = (_registered_pack_paths(source, images=True)
                         | _registered_pack_paths(source, images=False))
    missing_assets = sorted(rel for rel in registered_assets if rel not in staged)
    declarations: list[str] = []
    if profile == "shareable" and (missing_required or missing_assets):
        declarations.append(INCOMPLETE_DECLARATION)

    manifest_lines = [f"{_sha256(p)}  {p.relative_to(source).as_posix()}" for p in files]
    style_name, style_version = _primary_style(source)
    package_manifest = {
        "profile": profile,
        "version": version,
        "file_count": len(files),
        "style_pack": style_name,
        "style_pack_version": style_version,
        "style_pack_runtime_complete": not missing_required,
        "style_pack_assets_complete": not missing_assets,
        "missing_runtime_references": missing_required,
        "missing_assets": missing_assets[:20],
        "capability_statement": (
            "资料反推：完整；视觉生产：完整（含全部蒸馏参考与来源图记录）"
            if profile == "private" else
            "资料反推：完整；视觉生产：可用但不含来源图（参考素材层不完整，"
            "以 manifest 登记的 file_count 为准）"),
        "declarations": declarations,
        "excluded": excluded,
        "excluded_count": len(excluded),
        "note": ("私人完整包：含来源图与全部蒸馏参考，仅限本机/受控环境使用"
                 if profile == "private" else
                 "受控分享包：已按分享策略排除来源图与许可未确认文件；"
                 "能力声明与私人包不同，不得声称等价"),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    name_tpl = policy.get(f"{profile}_package_name", "资产提取-v{version}-" + profile + ".zip")
    zip_path = out_dir / name_tpl.format(version=version)
    # 包内根目录用**实际包名**（不是含 {version} 的模板）
    root_name = zip_path.stem
    # 固定时间戳：同一份源码重复构建必须逐字节一致，否则发布哈希无法被第三方复现验证
    # （此前 zf.write 会写入各文件当前 mtime，两次构建的 zip 哈希必然不同）。
    fixed_time = (1980, 1, 1, 0, 0, 0)   # ZIP 时间戳下限，兼容性最好
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            info = zipfile.ZipInfo(str(Path(root_name) / p.relative_to(source)),
                                   date_time=fixed_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, p.read_bytes())
        for extra_name, extra_text in (
            ("SHA256SUMS.txt", "\n".join(manifest_lines) + "\n"),
            ("发布清单.json", json.dumps(package_manifest, ensure_ascii=False, indent=2) + "\n"),
        ):
            info = zipfile.ZipInfo(str(Path(root_name) / extra_name), date_time=fixed_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, extra_text)
    (out_dir / f"SHA256SUMS-v{version}-{profile}.txt").write_text(
        "\n".join(manifest_lines)
        + f"\n{hashlib.sha256(zip_path.read_bytes()).hexdigest()}  {zip_path.name}\n",
        encoding="utf-8", newline="\n")
    print(f"✅ {profile} 包已生成: {zip_path}（{zip_path.stat().st_size} 字节，{len(files)} 个文件）")
    print(f"   SHA-256 清单: {out_dir / f'SHA256SUMS-v{version}-{profile}.txt'}")
    if excluded:
        print(f"   分享策略排除: {len(excluded)} 项")
    for declaration in declarations:
        print(f"   ⚠️ 声明: {declaration}（视觉能力低于私人包，不得声称等价）")
    return zip_path


def _release_gate(source: Path) -> None:
    """发布前置（工单 8.3）：工作树必须干净，且发布级总门必须全绿。

    只在**命令行发布**时执行；`build_release()` 库函数保持纯粹，供测试在临时目录快速构建。
    非 git 工作区（解压副本）跳过工作树检查，总门自身也会跳过 `git diff --check`。
    """
    if (source / ".git").exists():
        status = subprocess.run(["git", "status", "--short"], cwd=str(source),
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace")
        if status.returncode != 0 or status.stdout.strip():
            raise ReleasePreflightError(
                "RELEASE_TREE_DIRTY",
                "工作树不干净，拒绝打包（先提交或清理）：\n" + status.stdout.strip())
    gate = source / "测试" / "run_all_checks.py"
    if not gate.is_file():
        raise ReleasePreflightError("RELEASE_GATE_MISSING", f"找不到发布级总门：{gate}")
    result = subprocess.run([sys.executable, str(gate)], cwd=str(source),
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-15:])
        raise ReleasePreflightError("RELEASE_GATE_FAILED", f"发布级总门未通过：\n{tail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="候选发布白名单打包（private / shareable 双包）")
    parser.add_argument("--source", default=str(SKILL_DIR), help="技能仓库根（默认脚本所在仓库）")
    parser.add_argument("--out", required=True, help="发布输出目录（必须在仓库之外）")
    parser.add_argument("--profile", default="private", choices=PROFILES,
                        help="private=私人完整包；shareable=受控分享包")
    parser.add_argument("--skip-gate", action="store_true",
                        help="跳过「工作树干净 + 发布级总门」前置（仅用于本地试打包）")
    args = parser.parse_args(argv)
    source = Path(args.source)
    if not args.skip_gate:
        try:
            _release_gate(source)
        except ReleasePreflightError as e:
            print(f"❌ {e.code}：{e.detail}")
            return 1
    zip_path = build_release(source, Path(args.out), args.profile)
    return 0 if zip_path.is_file() else 1


if __name__ == "__main__":
    sys.exit(main())
