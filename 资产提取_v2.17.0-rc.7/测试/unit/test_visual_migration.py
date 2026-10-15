#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工单04-Task1：旧视觉产物识别与非破坏迁移（规格 10）。

- 只读识别旧提示词/旧图片/旧清单，**绝不覆盖、绝不删除**用户文件；
- 迁移前先备份，迁移后旧文件 SHA-256 逐一致；
- 旧图片登记为 `legacy_unreviewed`（默认不可作为参考）；新计划只依据当前资产索引，
  不从旧提示词反推事实；重复执行幂等。
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.engine.style_registry import resolve_style_record  # noqa: E402
from scripts.engine.visual_migration import (  # noqa: E402
    LEGACY_MARKER,
    LEGACY_UNREVIEWED,
    SOURCE_UNVERIFIABLE,
    MigrationError,
    migrate_legacy_visuals,
    scan_legacy_visuals,
)

STYLE = resolve_style_record("仙侠国风电影CG")
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000004000000040802000000269c4b59"
    "0000001349444154789c636c686860800126380b2f07004c040188c6ef54680000000049454e44ae426082")


def _write_index(lib):
    (lib / ".process" / "资产索引.json").write_text(json.dumps({
        "schema_version": 1, "source_fingerprint": "sha256:legacy",
        "assets": [{"asset_id": "CH_林姑娘", "type": "人物", "name": "林姑娘",
                    "priority": "P1", "fact_level": "剧本事实",
                    "evidence": [{"segment": "段01.md", "line": "00001",
                                  "quote": "林姑娘走进茶楼"}],
                    "relations": [], "dependencies": [], "status": "CONFIRMED"}]},
        ensure_ascii=False), encoding="utf-8")


def _legacy_lib(tmp_path, with_index=True):
    """构造一个含旧视觉产物的老库（旧表 + 旧提示词 + 旧图片 + 旧清单）。"""
    lib = tmp_path / "资产库"
    (lib / ".process").mkdir(parents=True)
    if with_index:
        _write_index(lib)
    (lib / "反推_最终.md").write_text(
        "# 反推\n\n## 人物资产提取表\n| 资产ID | 人物姓名 | 别名/称呼 | 出现位置 | 重要等级 | 备注 |\n"
        "|---|---|---|---|---|---|\n| CH_林姑娘 | 林姑娘 | | 第1章 | 主角 | |\n",
        encoding="utf-8")
    (lib / ".start-state.json").write_text(json.dumps(
        {"style": "H7", "input_source": "原文.txt", "genre": "玄幻"}, ensure_ascii=False),
        encoding="utf-8")
    (tmp_path / "原文.txt").write_text("第一章\n林姑娘走进茶楼。", encoding="utf-8")

    base = lib / "提示词" / "基础"
    base.mkdir(parents=True)
    (base / "人物提示词.md").write_text(
        "# 人物基础提示词\n\n## 001. 林姑娘\n\n整体三格图板：左面正面特写，右面正视图与后视图。"
        "两格全身头部与面部一律裁除不入画。\n",
        encoding="utf-8")
    (base / "人物提示词_裁头版.md").write_text(
        "# 人物基础提示词（裁头版）\n\n## 001. 林姑娘\n\n右面两格头部裁除。\n", encoding="utf-8")
    (lib / "交付清单.json").write_text(
        json.dumps({"status": "DELIVERED"}, ensure_ascii=False), encoding="utf-8")

    images = lib / "生成图片"
    images.mkdir()
    (images / "林姑娘_正脸.png").write_bytes(PNG)
    (images / "林姑娘_三宫格.png").write_bytes(PNG + b"panel")
    (lib / "图片清单.md").write_text(
        "# 图片清单\n| 文件 | 资产 |\n|---|---|\n| 林姑娘_正脸.png | 林姑娘 |\n", encoding="utf-8")
    return lib


def _hash_all(lib: Path) -> dict:
    return {str(p.relative_to(lib)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(lib.rglob("*")) if p.is_file()}


# --- 识别 -----------------------------------------------------------------------

def test_scan_finds_crop_head_prompt_and_panels(tmp_path):
    lib = _legacy_lib(tmp_path)
    report = scan_legacy_visuals(lib)
    kinds = {item["kind"] for item in report["items"]}
    assert "裁头版提示词" in kinds
    assert "三格/裁头提示词节" in kinds
    assert "旧生成图片" in kinds
    assert "旧图片清单" in kinds
    assert "旧风格状态" in kinds


def test_scan_reports_style_mapping_suggestion(tmp_path):
    lib = _legacy_lib(tmp_path)
    report = scan_legacy_visuals(lib)
    assert report["legacy_style"] == "H7"
    assert report["suggested_style"] == "仙侠国风电影CG"
    assert report["style_map_confirmed"] is False, "H7→新风格需要用户确认后才生效"


def test_scan_classifies_panel_images_separately(tmp_path):
    lib = _legacy_lib(tmp_path)
    report = scan_legacy_visuals(lib)
    images = [i for i in report["items"] if i["kind"] == "旧生成图片"]
    names = {Path(i["path"]).name for i in images}
    assert "林姑娘_正脸.png" in names
    panel = next(i for i in report["items"] if i.get("panel_suspect"))
    assert Path(panel["path"]).name == "林姑娘_三宫格.png"


def test_scan_does_not_misjudge_ordinary_images(tmp_path):
    lib = _legacy_lib(tmp_path)
    plain = lib / "参考资料"
    plain.mkdir()
    (plain / "普通照片.png").write_bytes(PNG)
    report = scan_legacy_visuals(lib)
    flagged = {Path(i["path"]).name for i in report["items"] if i["kind"] == "旧生成图片"}
    assert "普通照片.png" not in flagged, "普通图片不得误判为视觉产物"


def test_scan_is_read_only(tmp_path):
    lib = _legacy_lib(tmp_path)
    before = _hash_all(lib)
    scan_legacy_visuals(lib)
    assert _hash_all(lib) == before


def test_scan_reports_zero_when_clean(tmp_path):
    lib = tmp_path / "空库"
    lib.mkdir()
    report = scan_legacy_visuals(lib)
    assert report["items"] == []
    assert report["legacy_style"] == ""


# --- 迁移保全 -------------------------------------------------------------------

def test_migration_preserves_every_old_file(tmp_path):
    lib = _legacy_lib(tmp_path)
    before = _hash_all(lib)
    result = migrate_legacy_visuals(lib, "仙侠国风电影CG")
    after = _hash_all(lib)
    for rel, digest in before.items():
        assert rel in after, f"旧文件被删除：{rel}"
        assert after[rel] == digest, f"旧文件被改写：{rel}"
    assert result["migrated"] is True


def test_migration_creates_backup_first(tmp_path):
    lib = _legacy_lib(tmp_path)
    result = migrate_legacy_visuals(lib, "仙侠国风电影CG")
    assert Path(result["backup"]).is_file()


def test_migration_writes_only_into_visual_dir(tmp_path):
    lib = _legacy_lib(tmp_path)
    before = set(_hash_all(lib))
    migrate_legacy_visuals(lib, "仙侠国风电影CG")
    after = {rel.replace("\\", "/") for rel in set(_hash_all(lib)) - before}
    assert after
    # 新产物只写 视觉资产/；备份写在资产库外，不污染库内文件
    assert all(rel.startswith("视觉资产/") for rel in after), sorted(after)


def test_legacy_images_registered_unreviewed(tmp_path):
    lib = _legacy_lib(tmp_path)
    migrate_legacy_visuals(lib, "仙侠国风电影CG")
    index = json.loads((lib / "视觉资产" / LEGACY_MARKER).read_text(encoding="utf-8"))
    assert index["images"], index
    for item in index["images"]:
        assert item["status"] == LEGACY_UNREVIEWED
        assert len(item["sha256"]) == 64
        assert item["usable_as_reference"] is False


def test_migration_is_idempotent(tmp_path):
    lib = _legacy_lib(tmp_path)
    first = migrate_legacy_visuals(lib, "仙侠国风电影CG")
    second = migrate_legacy_visuals(lib, "仙侠国风电影CG")
    assert first["legacy_index_hash"] == second["legacy_index_hash"]
    assert second["already_migrated"] is True


def test_migration_requires_validated_asset_index(tmp_path):
    lib = _legacy_lib(tmp_path, with_index=False)
    with pytest.raises(MigrationError) as ctx:
        migrate_legacy_visuals(lib, "仙侠国风电影CG")
    assert ctx.value.code in ("ASSET_INDEX_REQUIRED",)


def test_migration_builds_plan_from_asset_index_not_old_prompts(tmp_path):
    lib = _legacy_lib(tmp_path)
    migrate_legacy_visuals(lib, "仙侠国风电影CG")
    plan = (lib / "视觉资产" / "计划.toml").read_text(encoding="utf-8")
    assert "CH_林姑娘-正脸-v01" in plan
    assert "林姑娘走进茶楼" in plan
    assert "三格图板" not in plan, "不得把旧提示词内容反推为事实"


def test_migration_uses_new_style_pack_version(tmp_path):
    lib = _legacy_lib(tmp_path)
    result = migrate_legacy_visuals(lib, "仙侠国风电影CG")
    assert result["style_version"] == STYLE.pack_version
    assert result["style_hash"] == STYLE.pack_hash
    assert "H7" not in (lib / "视觉资产" / "计划.toml").read_text(encoding="utf-8").split(
        "style_slug")[1][:20]


# --- 风格映射 -------------------------------------------------------------------

def test_unknown_style_rejected(tmp_path):
    lib = _legacy_lib(tmp_path)
    with pytest.raises(MigrationError) as ctx:
        migrate_legacy_visuals(lib, "并不存在的风格")
    assert ctx.value.code == "STYLE_UNKNOWN"


def test_e1_suggestion_only_when_explicitly_selected(tmp_path):
    """旧 E1 只在题材确认为仙侠且用户显式选择时提出建议，不静默改写。"""
    lib = _legacy_lib(tmp_path)
    (lib / ".start-state.json").write_text(json.dumps(
        {"style": "E1", "input_source": "原文.txt", "genre": "赛博朋克"}, ensure_ascii=False),
        encoding="utf-8")
    report = scan_legacy_visuals(lib)
    assert report["legacy_style"] == "E1"
    assert report["suggested_style"] == "", "非仙侠题材不得建议改到仙侠风格"
    assert report["style_map_confirmed"] is False


def test_unverifiable_source_is_reported(tmp_path):
    lib = _legacy_lib(tmp_path)
    (lib / ".start-state.json").unlink()
    report = scan_legacy_visuals(lib)
    assert any(SOURCE_UNVERIFIABLE in str(w) or SOURCE_UNVERIFIABLE == w
               for w in report["warnings"]), report["warnings"]


def test_migration_cli(tmp_path):
    """真实 CLI：备份 → 登记 → 新计划，退出 0 且旧文件哈希不变。"""

    lib = _legacy_lib(tmp_path)
    before = _hash_all(lib)
    r = run_py(
            [str(REPO_ROOT / "scripts" / "maintenance.py"), "migrate-visual", "--lib", str(lib), "--style", "仙侠国风电影CG"],
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "备份" in r.stdout and "旧图片" in r.stdout
    after = _hash_all(lib)
    for rel, digest in before.items():
        assert after[rel] == digest, rel
    assert (lib / "视觉资产" / LEGACY_MARKER).is_file()
    second = run_py(
            [str(REPO_ROOT / "scripts" / "maintenance.py"), "migrate-visual", "--lib", str(lib), "--style", "仙侠国风电影CG"],
    )
    assert second.returncode == 0
    assert "幂等" in second.stdout


def test_committed_legacy_fixture_is_scannable(tmp_path):
    """仓库内置的旧视觉夹具本身可被识别（只读使用）。"""
    fixture = REPO_ROOT / "测试" / "fixtures" / "legacy_visual"
    report = scan_legacy_visuals(fixture)
    kinds = {item["kind"] for item in report["items"]}
    assert {"裁头版提示词", "三格/裁头提示词节", "旧生成图片", "旧图片清单", "旧风格状态"} <= kinds
    assert report["legacy_style"] == "H7"


def test_fixture_copy_migrates_cleanly(tmp_path):
    """复制夹具到临时库后迁移：全部旧文件哈希不变。"""
    import shutil as _shutil

    src = REPO_ROOT / "测试" / "fixtures" / "legacy_visual"
    lib = tmp_path / "资产库"
    _shutil.copytree(src, lib)
    (lib / ".process").mkdir(parents=True, exist_ok=True)
    _write_index(lib)   # 产品合同：新视觉计划只依据**当前资产索引**，缺索引必须先拒绝（见同文件其它用例）
    before = _hash_all(lib)
    result = migrate_legacy_visuals(lib, "仙侠国风电影CG")
    assert result["migrated"] is True
    after = _hash_all(lib)
    for rel, digest in before.items():
        assert after[rel] == digest, rel
