# -*- coding: utf-8 -*-
"""卡工厂：制作/取样/锚点验证/判官验证/注册文风卡与场景卡。

默认工作目录：<skill>/卡工厂/<slug>/。
最终注册到用户索引库 `<lib>/文风蒸馏卡/`。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .audit import append_audit, now_iso
from .jsonio import read_json, write_json_atomic, write_text_atomic
from .paths import PROJECT_ROOT

DEFAULT_CARD_WORKDIR = PROJECT_ROOT / "卡工厂"

CARD_STATES = ("draft", "sampled", "anchored", "judged", "registered")
_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"sampled"},
    "sampled": {"anchored"},
    "anchored": {"judged"},
    "judged": {"registered"},
}


class CardFactoryError(RuntimeError):
    pass


def _slug(book: str, author: str, card_type: str, scene: str | None) -> str:
    """生成目录 slug：保留中文，只去掉路径非法字符。"""
    kind = f"{scene}场景卡" if card_type == "scene" and scene else "风格卡"
    raw = f"{book}-{author}-{kind}"
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "-", raw).strip("-")
    return cleaned


def card_dir(workdir: Path | str | None, book: str, author: str, card_type: str, scene: str | None = None) -> Path:
    root = Path(workdir) if workdir else DEFAULT_CARD_WORKDIR
    return root / _slug(book, author, card_type, scene)


def init_card(
    *,
    book: str,
    author: str,
    lib: Path | str,
    card_type: str = "style",
    scene: str | None = None,
    workdir: Path | str | None = None,
) -> dict[str, Any]:
    """初始化一张卡草稿。"""
    lib = Path(lib)
    if card_type not in {"style", "scene"}:
        raise CardFactoryError("card_type 必须是 style 或 scene")
    if card_type == "scene" and not scene:
        raise CardFactoryError("场景卡必须提供 --scene")
    target_dir = card_dir(workdir, book, author, card_type, scene)
    if target_dir.exists() and any(target_dir.iterdir()):
        raise CardFactoryError(f"卡工作区已存在: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    # 仅在默认卡工厂目录初始化时自动登记索引库；测试/自定义 workdir 不污染全局登记
    if workdir is None or Path(workdir).resolve() == DEFAULT_CARD_WORKDIR.resolve():
        try:
            from .library_registry import add_library

            add_library(lib)
        except Exception:  # noqa: BLE001 - 登记失败不阻断初始化
            pass

    doc = {
        "schema_version": "v4-card-factory-1",
        "slug": target_dir.name,
        "book": book,
        "author": author,
        "card_type": card_type,
        "scene": scene,
        "state": "draft",
        "lib_path": str(lib),
        "draft_path": "draft.md",
        "samples_path": "samples.md",
        "anchors_path": "anchors.json",
        "judge_path": "judge.json",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "blockers": [],
    }
    write_json_atomic(target_dir / "card.json", doc)
    write_text_atomic(
        target_dir / "draft.md",
        _draft_skeleton(book, author, card_type, scene),
    )
    (target_dir / "audit.jsonl").touch()
    append_audit(target_dir / "audit.jsonl", "card_init", **doc)
    return doc


def load_card(card: Path | str) -> dict[str, Any]:
    path = Path(card)
    if path.is_file() and path.name == "card.json":
        card_file = path
    else:
        card_file = path / "card.json"
    if not card_file.is_file():
        raise CardFactoryError(f"卡工作区缺少 card.json: {card_file}")
    return read_json(card_file)


def save_card(card: Path | str, doc: dict[str, Any]) -> None:
    path = Path(card)
    card_file = path if path.name == "card.json" else path / "card.json"
    doc["updated_at"] = now_iso()
    write_json_atomic(card_file, doc)


def transition_card(doc: dict[str, Any], target: str, *, reason: str, card_path: Path) -> dict[str, Any]:
    current = doc.get("state", "draft")
    if target not in _TRANSITIONS.get(current, set()):
        raise CardFactoryError(f"非法卡状态迁移: {current} -> {target}")
    doc["state"] = target
    doc["updated_at"] = now_iso()
    doc["blockers"] = []
    save_card(card_path, doc)
    append_audit(card_path / "audit.jsonl", "card_state_change", state=target, reason=reason)
    return doc


def _draft_skeleton(book: str, author: str, card_type: str, scene: str | None) -> str:
    if card_type == "scene":
        return (
            "---\n"
            "卡类型: 场景专卡（文风蒸馏·场景写法）\n"
            f"场景: {scene}\n"
            "取样书单:\n"
            f"  - 《{book}》{author}（待补充章节）\n"
            "一句话指纹: 待补充\n"
            f"蒸馏日期: {now_iso()[:10]}\n"
            "验证状态: 未验证\n"
            "锚点验证: 待验证\n"
            "卡源: 场景蒸馏\n"
            "---\n\n"
            f"# {scene}·场景专卡\n\n"
            "## 〇、跨书共识（3-5 条）\n\n"
            "## 一、分书样本\n\n"
            f"### 《{book}》\n\n"
            "**观察**\n- \n\n"
            "**可执行规则**\n- \n\n"
            "**锚点原句**\n- \n\n"
            "## 二、场景节奏\n\n"
            "## 三、不可写（≥5 条二元禁令）\n\n"
            "## 四、诚实边界\n\n"
        )
    return (
        "---\n"
        f"书名: 《{book}》\n"
        f"作者: {author}\n"
        f"蒸馏日期: {now_iso()[:10]}\n"
        "卡类型: 网文风格卡（文风蒸馏·如何写）\n"
        "取样章节清单:\n"
        "  - 开篇钩子: 待补充\n"
        "  - 打脸/金句宣言: 待补充\n"
        "样本规模: 待补充\n"
        "验证状态: 未验证\n"
        "锚点验证: 待验证\n"
        "卡源: 标准蒸馏\n"
        "场景强项: 待判官验证\n"
        "---\n\n"
        f"# 《{book}》网文风格卡\n\n"
        "> 本卡只写'怎么写'，不写'写了什么'。\n\n"
        "## 〇、母题意象（可选）\n\n"
        "## 一、9 轴\n\n"
        "### 轴 1：句子节奏\n"
        "- **观察**：\n- **可执行规则**：\n- **锚点原句**：\n\n"
        "### 轴 2：段落长度\n"
        "- **观察**：\n- **可执行规则**：\n- **锚点原句**：\n\n"
        "### 轴 3：词汇\n"
        "- **观察**：\n- **可执行规则**：\n- **锚点原句**：\n\n"
        "### 轴 4：语气腔调\n"
        "- **观察**：\n- **可执行规则**：\n- **锚点原句**：\n\n"
        "### 轴 5：比喻运用\n"
        "- **观察**：\n- **可执行规则**：\n- **锚点原句**：\n\n"
        "### 轴 6：情绪强度\n"
        "- **观察**：\n- **可执行规则**：\n- **锚点原句**：\n\n"
        "### 轴 7：开头与收尾\n"
        "- **观察**：\n- **可执行规则**：\n- **锚点原句**：\n\n"
        "### 轴 8：雷区\n"
        "- **不可写**：\n\n"
        "### 轴 9：叙事推进\n"
        "- **观察**：\n- **可执行规则**：\n- **锚点原句**：\n\n"
        "## 二、8 场景技法\n\n"
        "### 打脸碾压\n- \n\n"
        "### 升级突破\n- \n\n"
        "### 战斗对决\n- \n\n"
        "### 围观震撼\n- \n\n"
        "### 金句霸气\n- \n\n"
        "### 复仇归来\n- \n\n"
        "### 心境意境\n- \n\n"
        "### 日常过渡\n- \n\n"
        "## 三、不可写（≥5 条二元禁令）\n\n"
        "## 四、诚实边界\n\n"
    )



# ── 取样 ─────────────────────────────────────────────────────────────


def collect_samples(card: Path | str) -> dict[str, Any]:
    """从索引库章节索引定位原文，生成 samples.md（含原始摘录片段）。"""
    doc = load_card(card)
    card_path = Path(card)
    if card_path.name != "card.json":
        card_path = card_path / "card.json"
    root = card_path.parent

    lib = Path(doc["lib_path"])
    try:
        from ..enhance.index_lib import IndexLibrary

        index_lib = IndexLibrary(lib)
        index_lib.scan()
    except Exception as exc:  # noqa: BLE001
        raise CardFactoryError(f"索引库不可用: {exc}") from exc

    book = doc["book"]
    candidates = []
    if doc["card_type"] == "scene" and doc.get("scene"):
        try:
            candidates = index_lib.search_tags(
                [doc["scene"]], kinds=["index_chapter"], limit=6
            )
        except Exception:  # noqa: BLE001
            candidates = []
    if not candidates:
        candidates = [
            r for r in index_lib.assets("index_chapter")
            if book in (r.book or "") or (r.book or "") in book
        ][:6]

    if not candidates:
        raise CardFactoryError("索引库中未找到该书章节索引，无法取样")

    lines: list[str] = []
    used_any = False
    for record in candidates[:4]:
        source_rel = record.metadata.get("源文件")
        if not source_rel:
            continue
        try:
            sections = index_lib.get_chapter_index(record.rel_path)
        except Exception:  # noqa: BLE001
            continue
        line_range = sections[0].line_range if sections else None
        src_path = lib / str(source_rel)
        if not src_path.is_file():
            continue
        text = src_path.read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        if line_range and 1 <= line_range[0] <= line_range[1] <= len(all_lines):
            excerpt = "\n".join(all_lines[line_range[0] - 1:line_range[1]])
        else:
            excerpt = text
        excerpt = excerpt.strip()
        if not excerpt:
            continue
        if len(excerpt) > 1500:
            excerpt = excerpt[:1500] + "…"
        lines.append(
            f"### {record.rel_path}\n\n"
            f"- 源文件: {source_rel}\n"
            f"- 行号: {line_range}\n"
            f"- 场景: {doc.get('scene') or '风格通用'}\n\n"
            f"{excerpt}\n"
        )
        used_any = True

    if not used_any:
        raise CardFactoryError("未能从索引库定位可读原文样本")

    samples_text = "# 取样清单\n\n" + "\n".join(lines)
    write_text_atomic(root / "samples.md", samples_text)
    transition_card(doc, "sampled", reason="card_samples", card_path=root)
    append_audit(root / "audit.jsonl", "card_samples", count=len(lines))
    return doc


# ── 锚点验证 ─────────────────────────────────────────────────────────


def _extract_anchors(draft_text: str) -> list[str]:
    anchors: list[str] = []
    in_anchor = False
    for line in draft_text.splitlines():
        line = line.strip()
        if ("锚点" in line) and (line.startswith("-") or line.startswith("**")):
            in_anchor = True
            anchors.extend(re.findall(r'[""“”「」](.+?)[""“”「」]', line))
            continue
        if in_anchor:
            if line.startswith("-") or re.match(r"^\d+[.、]", line):
                anchors.extend(re.findall(r'[""“”「」](.+?)[""“”「」]', line))
            elif line.startswith("###") or line.startswith("##"):
                in_anchor = False
    return anchors


def verify_anchors(card: Path | str) -> dict[str, Any]:
    """验证 draft.md 中锚点是否能在对应书原文中找到（逐字去空白匹配）。"""
    doc = load_card(card)
    card_path = Path(card)
    root = card_path if card_path.name == "card.json" else card_path / "card.json"
    root = root.parent
    draft_text = (root / "draft.md").read_text(encoding="utf-8")
    anchors = _extract_anchors(draft_text)
    if not anchors:
        raise CardFactoryError("draft.md 中未找到任何锚点，请先补充锚点")

    lib = Path(doc["lib_path"])
    original_root = lib / "小说原文"
    book = doc["book"]
    search_dirs = [d for d in original_root.iterdir() if d.is_dir() and (book in d.name or d.name in book)] if original_root.is_dir() else []
    if not search_dirs:
        raise CardFactoryError("索引库小说原文目录中未找到对应书目录")

    def normalize(s: str) -> str:
        return re.sub(r"\s+", "", s)

    results = []
    missing = []
    for anchor in anchors:
        found = False
        needle = normalize(anchor)
        if not needle:
            continue
        for d in search_dirs:
            for f in d.rglob("*.md"):
                try:
                    if needle in normalize(f.read_text(encoding="utf-8", errors="replace")):
                        found = True
                        break
                except OSError:
                    continue
            if found:
                break
        results.append({"anchor": anchor, "hit": found})
        if not found:
            missing.append(anchor)

    total = len(results)
    missing_count = len(missing)
    verified = missing_count <= max(1, total // 3)
    anchor_doc = {
        "total": total,
        "hit": total - missing_count,
        "missing": missing,
        "verified": verified,
    }
    write_json_atomic(root / "anchors.json", anchor_doc)
    if verified:
        transition_card(doc, "anchored", reason="card_verify_anchors", card_path=root)
    else:
        doc["blockers"] = [f"锚点查无 {missing_count}/{total}，需返工"]
        save_card(root, doc)
        append_audit(root / "audit.jsonl", "card_anchors_failed", missing_count=missing_count)
    append_audit(root / "audit.jsonl", "card_anchors", total=total, hit=total - missing_count)
    return anchor_doc


# ── 判官验证 ─────────────────────────────────────────────────────────


def run_judge(card: Path | str, *, phase: str) -> dict[str, Any]:
    """写手判官或评审判官。依赖 LLM 配置；无 key 直接拒绝。"""
    doc = load_card(card)
    card_path = Path(card)
    root = card_path if card_path.name == "card.json" else card_path / "card.json"
    root = root.parent

    from .llm import call_llm, load_config

    config = load_config()
    if config.dry_run or not config.enabled or not config.api_key:
        raise CardFactoryError("判官验证需要真实 LLM：请配置 DEEPSEEK_API_KEY 且关闭 dry-run")

    draft_text = (root / "draft.md").read_text(encoding="utf-8")
    judge_path = root / "judge.json"
    judge_doc = read_json(judge_path) if judge_path.is_file() else {}

    if phase == "writer":
        system = "你是网文风格卡写手判官。只按卡的可执行规则写样本，不得引用原文锚点句。"
        user = (
            f"以下是风格卡草稿：\n{draft_text[:6000]}\n\n"
            "请写 3 段 200~300 字样本，场景分别为：打脸碾压、围观震撼、金句宣言。"
            "每段之间用空行分隔。"
        )
        raw = call_llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            config=config,
            audit_path=root / "audit.jsonl",
            chapter_id=doc["slug"],
        )
        samples = [p.strip() for p in re.split(r"\n\s*\n", raw.strip()) if p.strip()]
        judge_doc["writer_samples"] = samples[:3]
        judge_doc["writer_output"] = raw
        write_json_atomic(judge_path, judge_doc)
        append_audit(root / "audit.jsonl", "card_judge_writer", samples=len(samples))
        return judge_doc

    if phase == "reviewer":
        samples = judge_doc.get("writer_samples") or []
        if not samples:
            raise CardFactoryError("请先运行 judge --phase writer")
        system = "你是网文风格卡评审判官。按中国网文风格基准逐轴评分，只输出分数行。"
        user = (
            f"样本：\n{chr(10).join(samples)}\n\n"
            "请逐轴评分（句子节奏/段落/词汇/语气腔调/情绪强度/开头收尾/网文味整体感），"
            "每行格式：轴名:分数。最后一行：平均分:分数。分数范围1-5。"
        )
        raw = call_llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            config=config,
            audit_path=root / "audit.jsonl",
            chapter_id=doc["slug"],
        )
        scores = {}
        for line in raw.splitlines():
            m = re.match(r"^(.+?)[:：]\s*(\d+(?:\.\d+)?)\s*$", line.strip())
            if m:
                scores[m.group(1).strip()] = float(m.group(2))
        avg = None
        if "平均分" in scores:
            avg = scores["平均分"]
        elif scores:
            avg = sum(scores.values()) / len(scores)
        passed = avg is not None and avg >= 4.0 and all(v > 2 for k, v in scores.items() if k != "平均分")
        judge_doc["reviewer_scores"] = scores
        judge_doc["reviewer_output"] = raw
        judge_doc["average"] = avg
        judge_doc["passed"] = passed
        write_json_atomic(judge_path, judge_doc)
        if passed:
            transition_card(doc, "judged", reason="card_judge_reviewer", card_path=root)
        else:
            doc["blockers"] = [f"判官平均分 {avg}，未达 4.0"]
            save_card(root, doc)
            append_audit(root / "audit.jsonl", "card_judge_failed", average=avg)
        append_audit(root / "audit.jsonl", "card_judge_reviewer", average=avg, passed=passed)
        return judge_doc

    raise CardFactoryError("phase 必须是 writer 或 reviewer")


# ── 注册 ─────────────────────────────────────────────────────────────


def register_card(card: Path | str) -> Path:
    """验证通过后复制 draft.md 到索引库文风蒸馏卡，并刷新卡库索引。"""
    doc = load_card(card)
    card_path = Path(card)
    root = card_path if card_path.name == "card.json" else card_path / "card.json"
    root = root.parent

    if doc["state"] != "judged":
        raise CardFactoryError("卡未通过判官验证，禁止注册")
    anchors_path = root / "anchors.json"
    judge_path = root / "judge.json"
    if not anchors_path.is_file() or not judge_path.is_file():
        raise CardFactoryError("缺少 anchors.json 或 judge.json")
    anchors = read_json(anchors_path)
    judge = read_json(judge_path)
    if not anchors.get("verified"):
        raise CardFactoryError("锚点验证未通过，禁止注册")
    if not judge.get("passed"):
        raise CardFactoryError("判官验证未通过，禁止注册")

    lib = Path(doc["lib_path"])
    target_dir = lib / "文风蒸馏卡"
    target_dir.mkdir(parents=True, exist_ok=True)
    if doc["card_type"] == "scene" and doc.get("scene"):
        filename = f"{doc['scene']}-场景卡.md"
    else:
        filename = f"{doc['book']}-{doc['author']}-风格卡.md"
    target = target_dir / filename
    if target.exists():
        raise CardFactoryError(f"卡已存在，不覆盖: {target}")

    draft_text = (root / "draft.md").read_text(encoding="utf-8")
    draft_text = draft_text.replace("验证状态: 未验证", "验证状态: 已验证")
    draft_text = draft_text.replace("锚点验证: 待验证", "锚点验证: 通过")
    write_text_atomic(target, draft_text)
    transition_card(doc, "registered", reason="card_register", card_path=root)
    append_audit(root / "audit.jsonl", "card_register", target=str(target))
    try:
        from .card_index import write_card_index

        write_card_index(lib)
    except Exception:  # noqa: BLE001 - 索引刷新失败不影响注册本身
        pass
    return target
