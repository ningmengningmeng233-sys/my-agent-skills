#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单02-Task2：Omni 私人完整包的确定性同步与 manifest 校验（规格 5/8）。

母版是唯一上游；同步先生成清单与哈希，再原子写入仓库内置副本。
运行时只读内置副本，不依赖桌面绝对路径。
"""
import hashlib
import sys
import os
from pathlib import Path

import pytest
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from sync_stylepack import (  # noqa: E402
    CORE_RELATIVE_FILES,
    SyncError,
    build_style_manifest,
    manifest_path_for,
    pack_dir_for,
    pack_infos,
    sync_stylepack,
    verify_pack,
)

PROJECT_DIR = Path(__file__).resolve().parents[2]
# 母版是本机素材库，不随包分发：可用 ART_MASTER_STYLEPACK 指定，默认落在仓库内的可选目录
DEFAULT_MASTER = PROJECT_DIR / "参考母版" / "luminous-xianxia_仙侠CG风格库"
MASTER = Path(os.environ.get("ART_MASTER_STYLEPACK", str(DEFAULT_MASTER)))
PACK_DIR = pack_dir_for("仙侠国风电影CG", "1.0.0")
MANIFEST = manifest_path_for("仙侠国风电影CG", "1.0.0")
master_available = pytest.mark.skipif(not MASTER.is_dir(), reason="母版不在本机（离线/精简安装）")


def _fake_master(root: Path) -> Path:
    """最小可用母版：核心文件齐全 + 一张视图图 + 一张来源图 + 一个 .env 干扰项。"""
    src = root / "母版"
    (src / "references" / "materials").mkdir(parents=True)
    (src / "references" / "sources").mkdir(parents=True)
    (src / "generation_formula.md").write_text("# Generation formula\n公式内容\n", encoding="utf-8")
    (src / "negative_prompt.md").write_text("# Negative prompt\n负面内容\n", encoding="utf-8")
    (src / "SKILL.md").write_text("# Skill\n说明\n", encoding="utf-8")
    refs = src / "references"
    for name, body in (
        ("face_base_style.md", "脸部基线"),
        ("full_body_base_style.md", "全身基线"),
        ("environment_base_style.md", "环境基线"),
        ("object_base_style.md", "物件基线"),
        ("shared_style_invariants.md", "共享不变量"),
        ("router.md", "路由器"),
        ("iteration_notes.md", "迭代记录"),
    ):
        (refs / name).write_text(f"# {body}\n{body}\n", encoding="utf-8")
    (refs / "face.png").write_bytes(b"\x89PNG\r\n\x1a\nface")
    (refs / "materials" / "skin_base_style.md").write_text("# Skin\n皮肤\n", encoding="utf-8")
    (refs / "materials" / "skin.png").write_bytes(b"\x89PNG\r\n\x1a\nskin")
    (refs / "sources" / "character-1.png").write_bytes(b"\x89PNG\r\n\x1a\nsource")
    (src / ".env").write_text("GEMINI_API_KEY=should-not-be-copied\n", encoding="utf-8")
    return src


def test_pack_dir_and_manifest_exist():
    assert PACK_DIR.is_dir(), f"内置私人包缺失: {PACK_DIR}"
    assert MANIFEST.is_file(), f"缺少清单: {MANIFEST}"


def test_manifest_records_path_hash_bytes_category_and_share_permission():
    manifest = build_style_manifest(PACK_DIR)
    files = manifest["files"]
    assert files, "清单不得为空"
    for entry in files:
        assert entry["path"]
        assert len(entry["sha256"]) == 64
        assert entry["bytes"] > 0
        assert entry["category"]
        assert isinstance(entry["share_allowed"], bool)
        assert isinstance(entry["required"], bool)


def test_manifest_hashes_match_files_on_disk():
    from engine.common import toml_module

    data = toml_module().loads(MANIFEST.read_text(encoding="utf-8"))
    present = 0
    for entry in data["files"]:
        path = PACK_DIR / entry["path"]
        if not path.is_file():
            continue
        present += 1
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], entry["path"]
        assert path.stat().st_size == entry["bytes"], entry["path"]
    assert present >= len(CORE_RELATIVE_FILES)


def test_required_files_are_all_present_locally():
    """风格定义层（Markdown）必须随包分发；参考图可缺失但必须被如实计数。"""
    issues = verify_pack(PACK_DIR, require_images=False)
    assert issues == [], issues


@master_available
def test_builtin_pack_matches_master_manifest():
    """内置包与母版一致：**可分享层必须逐字节一致**；缺失项只能是已声明的来源图。

    精简安装（如受控分享包）会按分享策略排除 `references/sources/`；
    这类缺失必须能在清单里找到 `share_allowed = false` 的登记，绝不能是静默漂移。
    """
    master_manifest = build_style_manifest(MASTER)
    builtin_manifest = build_style_manifest(PACK_DIR)
    master_hashes = {f["path"]: f["sha256"] for f in master_manifest["files"]}
    builtin_hashes = {f["path"]: f["sha256"] for f in builtin_manifest["files"]}

    only_master = set(master_hashes) - set(builtin_hashes)
    only_builtin = set(builtin_hashes) - set(master_hashes)
    assert not only_builtin, f"内置包出现母版没有的文件: {sorted(only_builtin)[:5]}"
    for rel in only_master:
        assert rel.startswith("references/sources/"), f"只允许缺失来源图，实际缺 {rel}"
        entry = next(f for f in master_manifest["files"] if f["path"] == rel)
        assert entry["share_allowed"] is False, f"{rel} 未按分享策略排除却被缺省"

    drift = [p for p in master_hashes
             if p in builtin_hashes and master_hashes[p] != builtin_hashes[p]]
    assert drift == [], f"与母版漂移的文件: {drift[:5]}"


def test_sync_copies_complete_tree_and_skips_env(tmp_path):
    master = _fake_master(tmp_path)
    dest = tmp_path / "内置包"
    manifest_path = sync_stylepack(master, dest, version="9.9.9")
    assert manifest_path.is_file()
    assert (dest / "generation_formula.md").is_file()
    assert (dest / "references" / "sources" / "character-1.png").is_file()
    assert not (dest / ".env").exists()
    assert not list(dest.rglob("*.tmp*")), "不得留下临时目录"


def test_sync_is_deterministic(tmp_path):
    master = _fake_master(tmp_path)
    a = sync_stylepack(master, tmp_path / "包A", version="1.0.0")
    b = sync_stylepack(master, tmp_path / "包B", version="1.0.0")
    assert a.read_bytes() == b.read_bytes()
    # 清单自身哈希与版本写入后仍稳定（同一来源 → 同一清单字节）
    assert build_style_manifest(tmp_path / "包A") == build_style_manifest(tmp_path / "包B")


def test_sync_rejects_incomplete_master(tmp_path):
    master = _fake_master(tmp_path)
    (master / "generation_formula.md").unlink()
    with pytest.raises(SyncError) as ctx:
        sync_stylepack(master, tmp_path / "内置包", version="1.0.0")
    assert ctx.value.code == "STYLEPACK_SOURCE_INCOMPLETE"
    assert "generation_formula.md" in ctx.value.detail


def test_sync_skips_symlinks(tmp_path):
    master = _fake_master(tmp_path)
    outside = tmp_path / "外部机密.md"
    outside.write_text("不应被同步", encoding="utf-8")
    link = master / "linked.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")
    dest = tmp_path / "内置包"
    sync_stylepack(master, dest, version="1.0.0")
    assert not (dest / "linked.md").exists()


def test_verify_pack_detects_drift(tmp_path):
    master = _fake_master(tmp_path)
    dest = tmp_path / "内置包"
    sync_stylepack(master, dest, version="1.0.0")
    assert verify_pack(dest) == []
    target = dest / "generation_formula.md"
    target.write_text(target.read_text(encoding="utf-8") + "被人为改动\n", encoding="utf-8")
    issues = verify_pack(dest)
    assert issues and any("generation_formula.md" in i for i in issues), issues


def test_verify_pack_reports_missing_images_without_blocking(tmp_path):
    master = _fake_master(tmp_path)
    dest = tmp_path / "内置包"
    sync_stylepack(master, dest, version="1.0.0")
    (dest / "references" / "face.png").unlink()
    # 缺失参考图不算硬错误，但必须如实报告数量
    assert verify_pack(dest, require_images=False) == []
    assert any("参考图未随包分发" in i for i in pack_infos(dest)), pack_infos(dest)
    strict = verify_pack(dest, require_images=True)
    assert strict and any("face.png" in i for i in strict), strict


def test_source_images_are_not_share_allowed():
    from engine.common import toml_module

    data = toml_module().loads(MANIFEST.read_text(encoding="utf-8"))
    sources = [f for f in data["files"] if f["path"].startswith("references/sources/")]
    assert sources, "母版含来源图目录，清单必须覆盖"
    assert all(f["share_allowed"] is False for f in sources)
    assert all(f["required"] is False for f in sources)


def test_check_style_lib_flags_pack_drift_exit_code(tmp_path):
    master = _fake_master(tmp_path)
    dest = tmp_path / "内置包"
    sync_stylepack(master, dest, version="1.0.0")
    ok = run_py([str(REPO_ROOT / "scripts" / "check_style_lib.py"), "--pack", str(dest)])
    assert ok.returncode == 0, ok.stdout + ok.stderr
    target = dest / "references" / "router.md"
    target.write_text(target.read_text(encoding="utf-8") + "改动\n", encoding="utf-8")
    bad = run_py([str(REPO_ROOT / "scripts" / "check_style_lib.py"), "--pack", str(dest)])
    assert bad.returncode != 0
    assert "router.md" in (bad.stdout + bad.stderr)


def test_runtime_does_not_depend_on_desktop_master(monkeypatch):
    """运行时只读内置副本：任何对桌面母版路径的访问都视为失败（规格 2.7）。"""
    real_is_file = Path.is_file
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes

    def guard(path: Path):
        try:
            path.resolve().relative_to(MASTER.resolve())
        except ValueError:
            return
        raise AssertionError(f"运行时不得访问桌面母版: {path}")

    def is_file(self):
        guard(self)
        return real_is_file(self)

    def read_text(self, *a, **kw):
        guard(self)
        return real_read_text(self, *a, **kw)

    def read_bytes(self):
        guard(self)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    from scripts.engine.style_registry import resolve_style_record
    from scripts.engine.style import resolve_style

    record = resolve_style_record("仙侠国风电影CG")
    assert record is not None and record.id == "H7"
    card, mode = resolve_style(None, "仙侠国风电影CG")
    assert mode == "card" and card["id"] == "H7"
    assert build_style_manifest(PACK_DIR)["files"]
