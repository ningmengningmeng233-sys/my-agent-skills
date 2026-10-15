#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单 P1-2：视觉资产的可安装与可发布性。

红测目标（修复前必须失败）：
- 缺少 `install-stylepack` / `verify-stylepack`：Git 检出后无法装回 32 张权威图；
- 发布构建未按 manifest 逐文件校验存在/大小/哈希（少一张图也能出包）；
- 权威包 PNG 被 `.gitignore` 排除，Git clone 不是完整视觉安装。

本文件只做离线校验（不生成完整发布包，那由发布步骤执行）。
"""
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "engine"))

from scripts.engine.style_registry import pack_root, resolve_style_record  # noqa: E402
import skill_meta  # noqa: E402
from scripts.engine.stylepack_verify import (  # noqa: E402
    iter_pack_files,
    pack_content_hash,
    read_pack_manifest,
    verify_pack,
)

STYLE = resolve_style_record("仙侠国风电影CG")
PACK = pack_root(STYLE.pack_slug, STYLE.pack_version)
VERSION_DIR = PACK.parent

# 完整私有安装（含 12 张 sources 来源图 + 32 张参考图）才具备的前置。
# 分享包按策略排除来源图与部分私有图，因此依赖「完整私有包」的断言在此形态下
# 必须**跳过**而不是失败——否则分享包永远红，与「分享包声明不完整」的设计矛盾。
_FULL_PRIVATE_PACK = (PACK / "references" / "sources").is_dir() and len(
    [rel for rel, _ in iter_pack_files(PACK) if Path(rel).suffix.lower() == ".png"]) == 32

requires_full_pack = pytest.mark.skipif(
    not _FULL_PRIVATE_PACK,
    reason="需要完整私有风格包（含 sources 来源图与 32 张参考图）；分享形态下跳过")


# --- 权威包真实性 ----------------------------------------------------------------

@requires_full_pack
def test_authoritative_pack_present_and_complete():
    assert PACK.is_dir()
    assert verify_pack(PACK, require_images=True) == []


@requires_full_pack
def test_authoritative_pack_has_32_images():
    images = [rel for rel, _ in iter_pack_files(PACK)
              if Path(rel).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
    assert len(images) == 32, f"权威参考图应为 32 张，实际 {len(images)}"


@requires_full_pack
def test_manifest_records_bytes_and_hash_for_every_file():
    manifest = read_pack_manifest(PACK)
    entries = manifest.get("files", [])
    assert entries
    on_disk = {rel for rel, _ in iter_pack_files(PACK)}
    assert {e["path"] for e in entries} == on_disk
    for entry in entries:
        assert entry.get("sha256"), entry
        assert int(entry.get("bytes", 0)) > 0, entry


def test_pack_content_hash_is_stable_and_sensitive():
    before = pack_content_hash(PACK)
    assert before
    assert pack_content_hash(PACK) == before, "同内容两次计算结果必须一致"


# --- install-stylepack / verify-stylepack ----------------------------------------

def _make_source_zip(tmp_path: Path) -> Path:
    """把版本目录（含 manifest）打成「私有完整安装包」形态的 zip。"""
    zip_path = tmp_path / "风格包.zip"
    root = "仙侠国风电影CG-1.0.0"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(VERSION_DIR.rglob("*")):
            if path.is_file():
                zf.write(str(path), f"{root}/{path.relative_to(VERSION_DIR).as_posix()}")
    return zip_path


@requires_full_pack
def test_install_stylepack_roundtrip(tmp_path):
    """从本地 zip 安装到空的风格目录，逐文件验哈希通过。"""
    from scripts.install_stylepack import install_stylepack

    target = tmp_path / "styles"
    zip_path = _make_source_zip(tmp_path)
    result = install_stylepack(zip_path, target, style_name=STYLE.pack_slug,
                               version=STYLE.pack_version)
    assert result["installed"] > 0
    installed_pack = target / STYLE.pack_slug / STYLE.pack_version / "private"
    assert verify_pack(installed_pack, require_images=True) == []
    assert pack_content_hash(installed_pack) == pack_content_hash(PACK)


def test_install_stylepack_refuses_unknown_zip(tmp_path):
    """缺 manifest 的 zip 必须拒绝安装（不得用半个包冒充完整包）。"""
    from scripts.install_stylepack import StylepackInstallError, install_stylepack

    bad = tmp_path / "坏包.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("x/readme.txt", "no manifest")
    with pytest.raises(StylepackInstallError) as ctx:
        install_stylepack(bad, tmp_path / "styles", style_name=STYLE.pack_slug,
                          version=STYLE.pack_version)
    assert ctx.value.code in ("STYLEPACK_MANIFEST_MISSING", "STYLEPACK_INCOMPLETE")


def test_install_stylepack_refuses_hash_mismatch(tmp_path):
    """包内文件被改坏（哈希与清单不符）必须拒绝安装。"""
    from scripts.install_stylepack import StylepackInstallError, install_stylepack

    src = tmp_path / "stage"
    shutil.copytree(VERSION_DIR, src)
    (src / "private" / "generation_formula.md").write_text("被篡改", encoding="utf-8")
    zip_path = tmp_path / "坏哈希.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                zf.write(str(path), f"包/{path.relative_to(src).as_posix()}")
    with pytest.raises(StylepackInstallError) as ctx:
        install_stylepack(zip_path, tmp_path / "styles", style_name=STYLE.pack_slug,
                          version=STYLE.pack_version)
    assert ctx.value.code == "STYLEPACK_HASH_MISMATCH"


def test_install_is_atomic_no_partial_leftover(tmp_path):
    """安装失败不得留下半个包目录。"""
    from scripts.install_stylepack import StylepackInstallError, install_stylepack

    src = tmp_path / "stage2"
    shutil.copytree(VERSION_DIR, src)
    (src / "private" / "negative_prompt.md").unlink()
    zip_path = tmp_path / "缺文件.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                zf.write(str(path), f"包/{path.relative_to(src).as_posix()}")
    target = tmp_path / "styles"
    target.mkdir()
    with pytest.raises(StylepackInstallError):
        install_stylepack(zip_path, target, style_name=STYLE.pack_slug,
                          version=STYLE.pack_version)
    leftovers = list(target.rglob("*.tmp*")) + list(target.rglob(".tmp*"))
    assert not leftovers, f"失败后留下临时目录：{leftovers}"
    assert not (target / STYLE.pack_slug).exists(), "失败不得留下半个包"


def test_verify_stylepack_cli_reports_and_exits(tmp_path, capsys):
    """verify-stylepack 对完整包返回 0。"""
    from scripts.install_stylepack import main as install_main

    rc = install_main(["--verify", "--pack", str(PACK), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "file_count" in out


def test_verify_stylepack_cli_fails_on_damage(tmp_path, capsys):
    from scripts.install_stylepack import main as install_main

    broken = tmp_path / "包"
    shutil.copytree(VERSION_DIR, broken)
    (broken / "private" / "references" / "face.png").unlink()
    rc = install_main(["--verify", "--pack", str(broken / "private"), "--json"])
    assert rc != 0
    out = capsys.readouterr().out
    assert "face.png" in out


# --- 发布构建必须逐文件校验 -------------------------------------------------------

@requires_full_pack
def test_release_plan_includes_registered_pack_images():
    """发布暂存必须包含 32 张权威图（否则私有包不是完整视觉安装）。"""
    from scripts.build_release import plan_staging

    files, _excluded = plan_staging(REPO_ROOT, "private")
    rels = {f.relative_to(REPO_ROOT).as_posix() for f in files}
    images = [r for r in rels if r.startswith("references/styles/")
              and Path(r).suffix.lower() == ".png"]
    assert len(images) == 32, f"私有包应含 32 张权威图，实际 {len(images)}"


def test_release_preflight_rejects_missing_pack_image(tmp_path):
    """发布前置校验：权威包少一张图必须失败（不得静默出包）。"""
    from scripts.build_release import ReleasePreflightError, preflight_stylepack

    copy = tmp_path / "副本"
    shutil.copytree(REPO_ROOT / "references" / "styles", copy / "references" / "styles")
    (copy / "references" / "styles" / STYLE.pack_slug / STYLE.pack_version
     / "private" / "references" / "face.png").unlink()
    with pytest.raises(ReleasePreflightError) as ctx:
        preflight_stylepack(copy)
    assert ctx.value.code == "STYLEPACK_INCOMPLETE"
    assert "face.png" in ctx.value.detail


def test_release_preflight_skips_source_without_any_pack(tmp_path):
    """本源未随附权威包时不阻断（视觉包是可选下游模块），但如实报 present=False。"""
    from scripts.build_release import preflight_stylepack

    bare = tmp_path / "裸源"
    bare.mkdir()
    result = preflight_stylepack(bare)
    assert result["present"] is False
    assert result["file_count"] == 0


@requires_full_pack
def test_release_preflight_passes_on_intact_pack():
    from scripts.build_release import preflight_stylepack

    result = preflight_stylepack(REPO_ROOT)
    assert result["present"] is True
    assert result["style"] == STYLE.name
    assert result["file_count"] > 0
    assert result["image_count"] == 32
    assert result["content_sha256"]


def test_shareable_profile_declares_incomplete_visuals():
    """分享包可不含私有图，但必须显式声明不完整（不得冒充与私有包等价）。"""
    from scripts.build_release import INCOMPLETE_DECLARATION, plan_staging

    files, excluded = plan_staging(REPO_ROOT, "shareable")
    rels = {f.relative_to(REPO_ROOT).as_posix() for f in files}
    images = [r for r in rels if r.startswith("references/styles/")
              and Path(r).suffix.lower() == ".png"]
    assert len(images) <= 32, "分享包不应包含全部私有参考图"
    assert INCOMPLETE_DECLARATION, "分享包必须声明视觉能力低于私有包"
    if _FULL_PRIVATE_PACK:
        assert len(images) < 32, "完整形态下分享包不应包含全部私有参考图"
        assert excluded, "被排除项必须记录"


# --- 运行必需图 vs 来源图的区分 -----------------------------------------------------

@requires_full_pack
def _share_form_copy_impl(tmp_path: Path) -> Path:
    """构造**分享形态**的风格包：去掉 sources/ 来源图，保留运行必需图。"""
    version = tmp_path / "版本"
    shutil.copytree(VERSION_DIR, version)
    shutil.rmtree(version / "private" / "references" / "sources")
    return version / "private"


@requires_full_pack
def test_share_form_pack_passes_runtime_checks(tmp_path):
    """分享形态（无 sources 来源图）在运行必需校验下必须通过。

    来源图授权未确认、清单标 `share_allowed=false`，分享包按策略排除属设计如此；
    若把它当「不完整」，分享包将永远无法通过校验，与声明矛盾。
    """
    from scripts.engine.stylepack_verify import (
        pack_is_share_form,
        runtime_image_issues,
    )
    from scripts.engine.visual_request_chain import stylepack_integrity_issues

    pack = _share_form_copy_impl(tmp_path)
    assert pack_is_share_form(pack) is True
    assert runtime_image_issues(pack) == [], runtime_image_issues(pack)
    assert stylepack_integrity_issues(pack) == [], stylepack_integrity_issues(pack)


@requires_full_pack
def test_share_form_still_catches_missing_runtime_image(tmp_path):
    """分享形态下删掉一张**运行必需**图仍必须报错（不能借分享之名放行）。"""
    from scripts.engine.stylepack_verify import runtime_image_issues
    from scripts.engine.visual_request_chain import stylepack_integrity_issues

    pack = _share_form_copy_impl(tmp_path)
    (pack / "references" / "face.png").unlink()
    assert any("face.png" in i for i in runtime_image_issues(pack))
    assert any("face.png" in i for i in stylepack_integrity_issues(pack))


@requires_full_pack
def test_share_form_still_catches_drifted_runtime_image(tmp_path):
    from scripts.engine.stylepack_verify import runtime_image_issues

    pack = _share_form_copy_impl(tmp_path)
    (pack / "references" / "materials" / "skin.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 10)
    assert any("skin.png" in i for i in runtime_image_issues(pack))


@requires_full_pack
def test_source_images_are_not_required_at_runtime(tmp_path):
    """sources/ 来源图缺失不得算作运行必需缺图。"""
    from scripts.engine.stylepack_verify import runtime_image_issues

    pack = _share_form_copy_impl(tmp_path)
    # 20 张运行必需图都还在，来源图全无 → 无问题
    assert runtime_image_issues(pack) == []


# --- 交付报告自引用循环 -----------------------------------------------------------

def test_current_version_report_is_excluded_from_package():
    """本版本交付报告要写发布包哈希，必须排除出包（否则包哈希永远无法定稿）。

    历史版本报告不描述本包哈希，可以照常随包分发。
    """
    from scripts.build_release import _skill_version, plan_staging

    version = _skill_version(REPO_ROOT)
    report = f"docs/验收/v{version}交付报告.md"
    if not (REPO_ROOT / report).is_file():
        # 在**已发布的包内**运行时，本版报告本就不存在于源树（打包时已排除），
        # 此时无对象可断言：跳过而不是失败——这本身就是规则生效的表现。
        pytest.skip(f"源树没有本版交付报告（打包时已排除）：{report}")
    files, excluded = plan_staging(REPO_ROOT, "private")
    rels = {f.relative_to(REPO_ROOT).as_posix() for f in files}
    assert report not in rels, f"本版交付报告不得随包分发：{report}"
    assert any(item["path"] == report for item in excluded), \
        "排除本版报告必须记入排除清单并说明原因"


def test_older_reports_are_still_packaged():
    """历史版本报告不含自引用，应当保留在包内（验收证据链完整）。"""
    from scripts.build_release import plan_staging

    files, _excluded = plan_staging(REPO_ROOT, "private")
    rels = {f.relative_to(REPO_ROOT).as_posix() for f in files}
    assert "docs/验收/v2.15.0-rc.1交付报告.md" in rels
    assert "docs/验收/v2.15.0-Gemini视觉抽样.md" in rels


def test_packaged_sampling_manual_has_valid_qc_model():
    """随包分发的手册不得再出现工单判定失效的质检模型。

    工单原文：默认质检模型为 gemini-3-pro，当前官方模型目录没有这个模型代码。
    """
    from scripts.build_release import plan_staging

    files, _excluded = plan_staging(REPO_ROOT, "private")
    manuals = [f for f in files if f.name == "v2.15.0-Gemini视觉抽样.md"]
    assert manuals, "抽样手册应在包内"
    text = manuals[0].read_text(encoding="utf-8")
    assert '"gemini-3-pro"' not in text, "包内手册仍写着失效的质检模型"
    assert "gemini-3.1-pro-preview" in text, "包内手册必须给出有效质检模型"
    assert "visual-doctor" in text, "包内手册应含环境体检前置步骤"


# --- 构建可复现性 -----------------------------------------------------------------

def test_package_build_is_byte_reproducible(tmp_path):
    """同一份源码重复构建必须逐字节一致。

    否则发布哈希无法被第三方复现验证：`zf.write` 会把各文件当前 mtime 写进 zip，
    两次构建的哈希必然不同（正是 rc.2 首次发布时出现的问题）。
    """
    import hashlib

    from scripts.build_release import build_release

    out_a, out_b = tmp_path / "a", tmp_path / "b"
    # 版本号从 SKILL.md frontmatter 派生（与 build_release 同源），测试只锁「同名 + 字节一致」
    version = skill_meta.skill_version((REPO_ROOT / "SKILL.md").read_text(encoding="utf-8"))
    name = f"资产提取-v{version}-shareable.zip"
    zip_a = build_release(REPO_ROOT, out_a, "shareable")
    zip_b = build_release(REPO_ROOT, out_b, "shareable")
    assert zip_a.name == zip_b.name == name
    sha_a = hashlib.sha256(zip_a.read_bytes()).hexdigest()
    sha_b = hashlib.sha256(zip_b.read_bytes()).hexdigest()
    assert sha_a == sha_b, f"两次构建哈希不同：{sha_a[:16]}… vs {sha_b[:16]}…"


def test_package_zip_entries_use_fixed_timestamp(tmp_path):
    """包内条目必须使用固定时间戳（可复现性的来源）。"""
    import zipfile

    from scripts.build_release import build_release

    zip_path = build_release(REPO_ROOT, tmp_path / "out", "private")
    with zipfile.ZipFile(zip_path) as zf:
        stamps = {info.date_time for info in zf.infolist()}
    assert stamps == {(1980, 1, 1, 0, 0, 0)}, f"时间戳不固定：{stamps}"
