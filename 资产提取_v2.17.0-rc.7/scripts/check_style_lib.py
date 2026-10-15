#!/usr/bin/env python3
"""Self-check the style library (local, no LLM) per ADR-0005 decision 5."""
import argparse
import re
import sys
from pathlib import Path

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "engine"))

from engine import style as style_lib

CARD_RE = re.compile(r"^### ([A-H]\d+)\.", re.MULTILINE)


def run_checks() -> tuple[list[str], list[str], list[str], int, int]:
    """Run all checks. Return (errors, warnings, infos, card_count, map_rows)."""
    errors, warns, infos = [], [], []
    lib_text = style_lib._read(style_lib.STYLE_LIB)
    map_text = style_lib._read(style_lib.STYLE_MAP)

    ids = CARD_RE.findall(lib_text)
    cards: dict[str, dict] = {}
    legacy_missing = {"en": 0, "negative": 0, "render": 0}
    render_capable: list[str] = []
    for sid in ids:
        if sid.startswith("G"):
            infos.append(f"{sid}: G 系速查表，非卡片，跳过")
            continue
        card = style_lib.parse_style_card(sid)
        cards[sid] = card
        if not card or not card["name"]:
            errors.append(f"{sid}: 名称解析为空（标题格式应为「### H1. 名称」）")
            continue
        if not card["zh"].strip():
            errors.append(f"{sid} {card['name']}: 中文定性为空（必填）")
            continue
        layers = card.get("layers") or []
        has_render = any(
            k in l for l in layers for k in ("建模层", "渲染层", "介质层", "材质层", "线条层", "上色层")
        )
        if has_render:
            render_capable.append(sid)
        if sid.startswith("H"):
            if not card["en"].strip():
                errors.append(f"{sid} {card['name']}: 缺英文标签块（必填 4 字段之一，模板契约）")
            if not card["negative"].strip():
                errors.append(f"{sid} {card['name']}: 缺负面词（必填 4 字段之一，模板契约）")
            if not has_render:
                warns.append(f"{sid} {card['name']}: 无建模层/渲染层/介质层等分节——不支持人物/道具定妆注入（需要定妆请补分层）")
            pack_id = str(card.get("pack", "") or "")
            if pack_id:
                pdir = style_lib.STYLE_PACK_DIR / pack_id
                if not pdir.is_dir():
                    errors.append(f"{sid} {card['name']}: 风格包 {pack_id} 不存在（references/stylepacks/{pack_id}/）")
                elif not (pdir / "说明.md").is_file():
                    errors.append(f"{sid} {card['name']}: 风格包 {pack_id} 缺 说明.md")
                else:
                    n_views = len(list(pdir.glob("视图-*.md")))
                    n_mats = len(list((pdir / "材质").glob("*.md"))) if (pdir / "材质").is_dir() else 0
                    has_inv = (pdir / "不变量.md").is_file()
                    has_neg = (pdir / "负面词.md").is_file()
                    infos.append(f"{sid} 风格包 {pack_id}: 视图 {n_views} / 材质 {n_mats} / 不变量 {'有' if has_inv else '无'} / 负面词 {'有' if has_neg else '无'}")
                    for p in list(pdir.glob("视图-*.md")) + list((pdir / "材质").glob("*.md")) + [p for p in (pdir / "不变量.md", pdir / "负面词.md") if p.is_file()]:
                        body = re.sub(r"#[0-9A-Fa-f]{3,8}", "", p.read_text(encoding="utf-8"))
                        if re.search(r"[A-Za-z]{3,}", body):
                            errors.append(f"{sid} 风格包 {pack_id}: {p.name} 含英文（风格包必须纯中文落库，ADR-0076）")
            else:
                infos.append(f"{sid} {card['name']}: 未挂风格包（可按 references/stylepacks/README.md 补建）")
        else:
            if not card["en"].strip():
                legacy_missing["en"] += 1
            if not card["negative"].strip():
                legacy_missing["negative"] += 1
            if not has_render:
                legacy_missing["render"] += 1

    for line in map_text.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| 题材"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3 or not cells[2]:
            continue
        for sid in re.split(r"[/、,，]", cells[2]):
            sid = sid.strip()
            if not sid:
                continue
            if re.match(r"^[A-H]\d+$", sid):
                if sid not in cards:
                    errors.append(f"题材映射引用 {sid}，但画风库无此编号的卡片（请核对编号或先在画风库建卡）")
                continue
            # v2.15.0：映射列可用中文正式名（规格 5）；必须能在登记表精确解析
            if not _resolves_to_registered_style(sid):
                errors.append(f"题材映射引用「{sid}」，但它既不是画风库编号，也不在风格登记表中"
                              "（请核对名称，或先在 references/styles/registry.toml 登记）")

    h_nums = sorted(int(s[1:]) for s in cards if s.startswith("H"))
    for i, n in enumerate(h_nums, 1):
        if n != i:
            errors.append(f"H 系编号跳号：期望 H{i}，实际 H{n}（请顺延编号）")

    try:
        from check_schedule import style_keywords
        for sid, card in cards.items():
            if not style_keywords(card["name"]):
                warns.append(f"{sid} {card['name']}: 必守词提取为空——画风丢失检查将失效（中文定性需含标点分句）")
    except Exception as e:
        warns.append(f"必守词检查跳过: {e}")

    n_map = sum(
        1
        for l in map_text.splitlines()
        if l.strip().startswith("|")
        and not l.strip().startswith("|—")
        and not l.strip().startswith("| 题材")
    )
    if any(legacy_missing.values()):
        legacy = []
        if legacy_missing["en"]:
            legacy.append(f"缺英文标签块 {legacy_missing['en']} 张")
        if legacy_missing["negative"]:
            legacy.append(f"缺负面词 {legacy_missing['negative']} 张")
        if legacy_missing["render"]:
            legacy.append(f"无渲染层分节 {legacy_missing['render']} 张")
        infos.append(
            f"A-F 既有卡现状（历史格式，非错误）: {'、'.join(legacy)}；"
            f"支持人物/道具定妆注入的卡: {', '.join(render_capable) or '无'}"
        )
    return errors, warns, infos, len(cards), n_map


def _resolves_to_registered_style(value: str) -> bool:
    """中文正式名/别名是否能在风格登记表精确解析（冲突视为不可解析并如实报错）。"""
    try:
        from engine.style_registry import resolve_style_record
    except ImportError:
        return False
    try:
        return resolve_style_record(value) is not None
    except ValueError:
        return False


def verify_pack_cli(pack_dir: Path) -> int:
    """校验版本化内置包（工单02-Task2）：清单缺失/哈希漂移/清单外多出文件一律非零。

    传入的是内容根 `<风格>/<版本>/private/`；清单位于其上一级 `<版本>/manifest.toml`。
    """
    from sync_stylepack import MANIFEST_FILE, pack_infos, verify_pack

    manifest = pack_dir.parent / MANIFEST_FILE
    if not manifest.is_file():
        print(f"❌ 缺少风格包清单: {manifest}")
        return 1
    issues = verify_pack(pack_dir, require_images=False)
    for info in pack_infos(pack_dir):
        print(f"  ⚠️ {info}")
    for issue in issues:
        print(f"  ❌ {issue}")
    if issues:
        print(f"❌ 风格包校验未通过（{len(issues)} 项硬错误）: {pack_dir}")
        return 1
    print(f"✅ 风格包校验通过: {pack_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="画风库与风格包自检")
    parser.add_argument("--pack", help="只校验指定内置风格包目录（跳过画风库自检）")
    args = parser.parse_args()
    if args.pack:
        return verify_pack_cli(Path(args.pack))

    errors, warns, infos, n_cards, n_map = run_checks()
    print(f"🔍 画风库自检: 卡片 {n_cards} 张 | 映射 {n_map} 行")
    for tag, items in (("❌", errors), ("⚠️", warns), ("ℹ️", infos)):
        for it in items:
            print(f"  {tag} {it}")
        if items:
            print(f"  → {tag} 共 {len(items)} 项")
    if errors:
        print("❌ 自检未通过：请修复硬错误后再使用新增风格（警告/信息不阻塞）")
        return 1
    print("✅ 自检通过（0 硬错误；H 系警告请逐条确认，A-F 现状见信息行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
