#!/usr/bin/env python3
"""Prompt generation engine: base + derivative, one LLM call per task."""
import argparse
import hashlib
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
ASSET_LIB_DIR = Path(os.environ.get("ASSET_LIB_DIR", "").strip() or SKILL_DIR / "资产库")
TMPL_DIR = SKILL_DIR / "templates"

sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))
sys.path.insert(0, str(SKILL_DIR))

from engine import prompt_rules
from engine.common import safe_name, load_asset_types
from engine.prompt_rules import DERIV_CHANNEL_TYPES
from engine.llm import call_llm, log, WORKERS, MAX_TOKENS
from engine import style as style_lib
if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def parse_asset_table(text: str, name: str) -> list[list[str]]:
    """Parse an asset table from the final reverse-derived file."""
    rows = []
    in_table = False
    for line in text.splitlines():
        if line.startswith("## ") and name in line:
            in_table = True
            continue
        if not in_table:
            continue
        if line.startswith("## ") or line.startswith("# "):
            break
        if line.startswith("|") and not line.startswith("|---"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 4 and not cells[0].startswith("资产ID"):
                rows.append(cells)
    return rows


def load_asset_index(lib: Path) -> dict:
    """读取规范化资产索引（ADR-0084），缺失时返回空索引（旧库兼容）。"""
    import json
    path = lib / ".process" / "资产索引.json"
    if not path.is_file():
        return {"schema_version": 1, "source_fingerprint": "", "assets": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": 1, "source_fingerprint": "", "assets": []}


def index_by_asset_id(index: dict) -> dict[str, dict]:
    """资产索引按旧展示 ID 建立映射。"""
    out: dict[str, dict] = {}
    for a in index.get("assets", []):
        aid = a.get("asset_id", "")
        if aid and aid not in out:
            out[aid] = a
    return out


def format_skip_entry(aid: str, type_key: str, raw: str, reason: str) -> str:
    """跳过清单行必须以资产ID开头：状态机与 check_schedule 按 ID 机器匹配合法排除。"""
    return f"{aid} {type_key}「{raw}」{reason}"


def should_generate_base(asset: dict | None) -> tuple[bool, str]:
    """基础提示词生成判定（工单 4：正式资产永不因优先级/事实等级缺失）。

    - 显式「非资产 / 用户明确取消」→ 不生成，返回排除原因（进跳过清单，唯一合法排除）。
    - 待定冲突 → 生成「带阻断标识的草案」（返回 True，note 含「阻断」），不无声缺失。
    - 其余（含 P0–P3、单次出现）一律生成基础提示词。
    旧库无索引时默认生成（保持兼容）。
    """
    if not asset:
        return True, ""
    excl = str(asset.get("exclude_reason", "") or "").strip()
    if excl and any(k in excl for k in ("非资产", "用户取消", "取消")):
        return False, f"{excl}：不生成基础提示词"
    if asset.get("fact_level") == "待定冲突":
        return True, "阻断-待定冲突：产出带阻断标识的草案，待人工裁决后转正"
    return True, ""


def format_prompt_header(asset: dict | None, extra: dict | None = None) -> str:
    """提示词节头机读元数据（ADR-0084）：ID/stable_key/事实等级/依赖/证据。

    正文仍为纯中文可复制提示词；元数据行使用中文键，避免触发纯中文检查。
    """
    if not asset:
        return ""
    parts = [
        f"资产编号：{asset.get('asset_id', '')}",
        f"稳定键：{asset.get('stable_key', '')}",
        f"事实等级：{asset.get('fact_level', '')}",
        f"生产优先级：{asset.get('priority', '')}",
    ]
    deps = asset.get("dependencies", []) or []
    if deps:
        parts.append(f"依赖编号：{'、'.join(deps)}")
    ev = asset.get("evidence", []) or []
    if ev:
        first = ev[0]
        parts.append(f"原文证据：{first.get('chapter', '')}{first.get('segment', '')}行{first.get('line', '')}")
    if extra:
        for k, v in extra.items():
            if v:
                parts.append(f"{k}：{v}")
    return "【" + "｜".join(parts) + "】"


def parse_schedule_list(text: str) -> list[dict[str, str]]:
    """Parse 调度清单.md → task items."""
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| 类型"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 4 and cells[0] in ("人物", "场景", "道具"):
            item = {"类型": cells[0], "资产": cells[1], "变体": cells[2], "备注": cells[3]}
            if len(cells) >= 5 and cells[4]:
                item["画风"] = cells[4]
            # ADR-0084 新增列（可选，向后兼容）：母版编号 / 仅允许变化
            if len(cells) >= 6 and cells[5]:
                item["母版编号"] = cells[5]
            if len(cells) >= 7 and cells[6]:
                item["仅允许变化"] = cells[6]
            items.append(item)
    return items


def load_template(name: str) -> tuple[str, str]:
    """Split a template into system prompt and user template sections."""
    text = (TMPL_DIR / name).read_text(encoding="utf-8")
    parts = text.split("## 系统提示\n", 1)
    if len(parts) != 2:
        raise ValueError(f"{name} 模板格式错误（需含 ## 系统提示 与 ## 用户输入模板 分节）")
    system = parts[1].split("\n## 用户输入模板\n", 1)[0].strip()
    user_tmpl = parts[1].split("\n## 用户输入模板\n", 1)[1].strip()
    # 工单C：衍生模板缺 master_block 占位符时自动追加（旧模板兼容）
    if name == "衍生提示词模板.md" and "{master_block}" not in user_tmpl:
        user_tmpl = user_tmpl.rstrip() + "\n场景母版约束: {master_block}\n"
    return system, user_tmpl


def _build_scene_master_block(lib: Path, job: dict) -> str:
    """场景派生母版注入块：验证母版 ID → 已确认锚点 + 未变约束 + 允许变化。

    未知字段不要求 LLM 虚构；母版缺失/ID 不存在时抛错阻断（不静默降级）。
    """
    from engine.continuity import parse_master_sections

    master_path = lib / "连续性" / "场景母版.md"
    asset_name = job.get("资产", job.get("asset", ""))
    master_id = (job.get("母版编号", "") or "").strip() or f"SC_{asset_name}"
    if not master_path.is_file():
        # 旧单测/旧库无母版时降级为空约束（新流程聚合必写母版，缺失仅兼容）
        allow_empty = (job.get("仅允许变化", "") or "").strip() or "无（仅允许变体维度变化）"
        return (f"母版编号：{master_id}\n已确认锚点（派生必须继承，不得改写）：\n"
                f"- （母版暂无已确认锚点）\n未变约束：除变体“{job.get('变体', '')}”外，其余维度沿用基础原值\n"
                f"仅允许变化：{allow_empty}\n未知项按“未说明”处理，禁止虚构门把手方向等细节")
    try:
        sections = parse_master_sections(master_path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ValueError(f"场景母版不可读: {e}")
    master_id = (job.get("母版编号", "") or "").strip() or f"SC_{asset_name}"
    sec = sections.get(master_id)
    if sec is None:
        # 兼容母版节标题 “## SC_x 名称” 与裸资产名两种写法
        alt = f"SC_{asset_name}"
        sec = sections.get(alt)
        master_id = alt if sec else master_id
    if sec is None:
        # 旧库/旧单测无对应母版节：降级为空约束（新流程聚合必写全场景节）
        sec = {}
    confirmed = []
    for key in ("空间结构", "固定锚点", "入口出口", "材质与光源", "连接点"):
        val = (sec.get(key, "") or "").strip()
        if val and val != "未说明":
            confirmed.append(f"{key}：{val}")
    allow = (job.get("仅允许变化", "") or "").strip() or "无（仅允许变体维度变化）"
    lines = [f"母版编号：{master_id}", "已确认锚点（派生必须继承，不得改写）："]
    lines += [f"- {c}" for c in confirmed] if confirmed else ["- （母版暂无已确认锚点）"]
    lines.append(f"未变约束：除变体“{job.get('变体', '')}”外，其余维度沿用基础原值")
    lines.append(f"仅允许变化：{allow}")
    lines.append("未知项按“未说明”处理，禁止虚构门把手方向等细节")
    return "\n".join(lines)


def parse_deriv_needs(text: str) -> list[tuple[str, str, str, str]]:
    """Parse the '衍生需求汇总' section."""
    items = []
    in_table = False
    for line in text.splitlines():
        if line.startswith("## ") and "衍生需求" in line:
            in_table = True
            continue
        if not in_table:
            continue
        if line.startswith("## ") or line.startswith("# "):
            break
        if line.startswith("|") and not line.startswith("|---"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 4 and cells[0] in ("场景", "道具"):
                items.append((cells[0], cells[1], cells[2], cells[3]))
    return items


# 「显式占位符」：语义唯一、不可能是人名，全/半角与裸写三种写法都收。
# **只有这一族**可以用于 `build_costume_map` 那种"误判即静默"的查表。
_PLACEHOLDER_BINDINGS = ("（未绑定）", "(未绑定)", "未绑定")

# 「绑定人物」列的宽泛哨兵：模型用来表示"这件造型没有明确的人"。
# 与 aggregate.py 的 WEARS 口径一致（悬空绑定进 unresolved，不猜人），
# 并沿用 check_schedule.split_names 既有的占位词表。空值同样算未绑定。
# ⚠️ 这一族**只用于写调度行**（误判会留下痕），不得用于查表——见 build_costume_map 的说明。
# 实测的两处消费方刻意不同口径：调度跳过 `待确认` 等词，查表保留它们当键；
# 后者无害（键只会被人物表里的真名查到），前者是实测缺陷的修复点。
_UNBOUND_BINDING_MARKERS = frozenset({
    *_PLACEHOLDER_BINDINGS,
    "待确认", "未知", "未指明", "不详",
})


def _is_unbound_binding(bind: str) -> bool:
    return not bind or bind in _UNBOUND_BINDING_MARKERS


def build_default_schedule(text: str, genre: str, lib: Path | None = None) -> str:
    """Generate the default derivative schedule from the final file.

    `lib` 给出时，行内类型按库内**资产索引**归一（索引是类型的唯一权威来源）；
    不传则保持纯文本行为（旧库/单元测试兼容）。
    """
    person_rows: list[str] = []
    skipped: list[str] = []
    for row in parse_asset_table(text, "服装妆造"):
        if len(row) >= 4 and len(row) > 2:
            bind = row[2].strip()
            name = row[1].strip()
            if _is_unbound_binding(bind):
                # 只要跳过就留痕（对抗评审 B2）：造型名本身是占位符时以资产 ID 兜底，
                # 并保留原文名（若不为空），不留"既不出行、也不说明"的不可见路径。
                # 注：`len(row) >= 4` 由 parse_asset_table 自身保证（它只收 >=4 列且跳过表头），
                # 故 row[0] 必为资产 ID，不会越界。
                label = name if not _is_unbound_binding(name) else (
                    f"{row[0].strip() or '（无资产ID）'}"
                    + (f"（造型名={name}）" if name else "")
                )
                skipped.append(f"{label}（绑定列={bind or '空'}）")
                continue
            person_rows.append(f"| 人物 | {bind} | {name} | 服装造型衍生 | | | |")
    # 衍生需求行的类型**以资产索引为准**（索引缺失时退回行内声明）：模型把服装写成「道具」时，
    # 调度清单照抄就会让衍生步骤按「道具」找父资产 → 抛错停链。无衍生通道的类型不入表，但必须点名。
    deriv_rows: list[str] = []
    skipped_no_channel: list[str] = []
    type_by_name = load_asset_types(lib) if lib is not None else {}
    for t, asset, need, chs in parse_deriv_needs(text):
        kind = type_by_name.get(asset.strip(), "") or t
        if kind not in DERIV_CHANNEL_TYPES:
            skipped_no_channel.append(f"{asset}（{kind}）")
            continue
        # 工单C：场景行默认填母版 ID（SC_资产名），非视角变体也继承空间结构
        master_id = f"SC_{asset}" if kind == "场景" else ""
        allow = need if kind == "场景" else ""
        deriv_rows.append(f"| {kind} | {asset} | {need} | 剧本驱动（{chs}） | | {master_id} | {allow} |")
    lines = [
        "# 衍生调度清单（脚本默认生成，可编辑补充）",
        "",
        "> 每行一个衍生变体任务。场景/道具行来自反推文件的'衍生需求汇总'（剧本驱动，可删改）；",
        "> 人物行来自服装妆造表（绑定人物×造型）。可自行增删行：场景时段/天候/角度、道具特殊状态。",
        "> 格式: | 类型 | 资产名称 | 变体标识 | 备注 | 画风 | 母版编号 | 仅允许变化 |（后两列可选：场景派生继承母版锚点）",
    ]
    if skipped:
        lines.append("> 未绑定人物的造型已跳过（绑定列是占位符，脚本不猜人）；"
                     "确知穿着者时自行补一行即可：" + "、".join(skipped))
    if skipped_no_channel:
        lines.append("> 以下衍生需求**不在 `--deriv` 通道**（该类型没有衍生模板），已略过："
                     + "、".join(skipped_no_channel) + "；服装变体请走服饰设计线（`服饰设计/SKILL.md`）。")
    lines += [
        "",
        "| 类型 | 资产 | 变体 | 备注 | 画风 | 母版编号 | 仅允许变化 |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += person_rows
    lines += deriv_rows
    return "\n".join(lines)


MERGED_PERSON_FILE = "人物提示词.md"
CACHE_HEADER = "CACHE_TITLE: "
REJECT_DIR_NAME = ".不合格产出"


# 合并文档分节解析：实现只有 prompt_rules 一份（此前本文件与 check_schedule 各抄一份
# 逐字节相同的实现）。此处**原样再导出**，生成端与检查器读写同一份节名规则。
from engine.prompt_rules import parse_merged_sections  # noqa: F401


def render_merged_sections(title: str, note: str, sections: list[tuple[str, str]]) -> str:
    """Render ordered sections into one merged prompt document."""
    lines = [f"# {title}", "", note, ""]
    for i, (name, content) in enumerate(sections, 1):
        lines += [f"## {i:03d}. {name}", "", content, "", "---", ""]
    return "\n".join(lines).rstrip() + "\n"


def read_merged_sections(path: Path) -> dict[str, str]:
    """Read merged sections, tolerant of a missing file."""
    return parse_merged_sections(path.read_text(encoding="utf-8")) if path.exists() else {}


def find_section(path: Path, name: str) -> str:
    """Return one section content from a merged document, or empty string."""
    return read_merged_sections(path).get(name, "")


def recover_merged_cache(merged: Path, cache_dir: Path, known: set[str]) -> tuple[dict[str, str], bool]:
    """Recover successfully generated cache files into the merged document."""
    sections = read_merged_sections(merged)
    changed = False
    if cache_dir.is_dir():
        leftover: list[Path] = []
        for f in sorted(cache_dir.glob("*.md")):
            raw = f.read_text(encoding="utf-8")
            if not raw.startswith(CACHE_HEADER):
                leftover.append(f)
                continue
            nl = raw.index("\n")
            name = raw[len(CACHE_HEADER):nl].strip()
            content = raw[nl + 1:].strip()
            if content and name in known and name not in sections:
                sections[name] = content
                changed = True
            f.unlink()
        for f in leftover:
            f.unlink()
        try:
            cache_dir.rmdir()
        except OSError:
            pass
    return sections, changed


def write_merged(merged: Path, title: str, note: str, ordered: list[str], sections: dict[str, str]) -> None:
    """Write merged document in the given name order."""
    items = [(n, sections[n]) for n in ordered if n in sections and sections[n]]
    merged.write_text(render_merged_sections(title, note, items), encoding="utf-8")


def _write_cache(cache_dir: Path, name: str, content: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{id(name):x}.md").write_text(
        f"{CACHE_HEADER}{name}\n\n{content}\n", encoding="utf-8"
    )


def _write_rejected(out_dir: Path, name: str, content: str) -> None:
    """把修复后仍不合格的产出单独留档，供人工排查。

    刻意**不写进生成缓存**，因为缓存有两条会把闸门架空的路：
    - 下次运行 `recover_merged_cache` 会把它无条件提升进合并文档，
      等于没通过合规闸的内容靠"再跑一次"静默转正；
    - 本轮收尾的 `cache_dir.glob("*.md")` 会把它连同成功项一起删掉，
      声称的"不丢内容"并不成立。
    （两条均为对抗评审 B3/B4 实测复现。）
    """
    d = out_dir / REJECT_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    stem = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    (d / f"{stem}.md").write_text(f"{CACHE_HEADER}{name}\n\n{content}\n", encoding="utf-8")


def _repair_incompliant(content: str, asset_type: str) -> tuple[str, int]:
    """对尚未落盘的产出跑机械化修复（规则与落盘路径同一份，见 engine.autofix）。

    修复本身出问题不得反过来打断主流程：失败即原样返回，让合规闸照常计失败。
    但**必须留痕**——原实现把异常静默吞掉，一个 TypeError 就让修复长期失效而无人察觉。
    """
    try:
        from engine import autofix
        return autofix.repair_text(content, asset_type)
    except Exception as exc:  # noqa: BLE001 - 修复是补救，不是主路径
        log(f"  ⚠ 自动修复跳过：{type(exc).__name__}: {exc}")
        return content, 0


def _compliance_checker(step: str):
    """按产物类型选生成侧自检函数；返回 None 表示该步骤不做合规拦。

    - **提示词产物**（基础/衍生）→ `check_compliance`：身份锁 / 镜头体系 / 三视图 / 负面锁 / 顿号
    - **描述产物**（`--enrich`）→ `check_desc_compliance`：负面限制 + 各类型的必填字段。
      描述模板要求输出「负面限制」段、校验器 `check_schedule` 也查它，此前唯独生成时不检查
      ——实测 35 项漏 1 项，白跑一整趟到 verify 才发现（工单 T3）。
    """
    if step == "描述补全":
        return prompt_rules.check_desc_compliance
    if step in ("基础提示词", "衍生提示词"):
        return prompt_rules.check_compliance
    return None


def _taste_correction(hits: list[str]) -> str:
    """抽象空洞词的纠正提示：把命中的词点名，并说清为什么（这些词渲染不出任何东西）。"""
    return ("\n\n【用词纠正】你上一轮的输出含抽象空洞词：" + "、".join(hits)
            + "。它们传达不了可渲染的视觉信息，请换成具体的材质、形制、光比写法后重新输出完整内容。")


def _struct_correction(missing: list[str]) -> str:
    """结构性标记缺失的纠正提示（软判据：重试一次，仍缺不丢产出）。"""
    return ("\n\n【结构标记纠正】你上一轮的输出缺少这些必备标记：" + "、".join(missing)
            + "。它们必须原样出现在对应段落里（不得改写、省略或合并），请补齐后重新输出完整内容。")


def _lock_correction(missing: list[str]) -> str:
    """画风锁定分层的纠正提示：点名缺哪一层，并要求**逐字**抄回锁定段。"""
    return ("\n\n【锁定段纠正】你上一轮的输出缺少画风锁定分层：" + "、".join(missing)
            + "。画风锁定段必须**逐字**保留（不得改写、合并或省略任何一层），"
              "请补齐后重新输出完整内容。")


def _lock_layer_missing(content: str, checks: list[tuple]) -> list[str]:
    """按画风卡逐层逐字比对，返回缺失的层标签（与校验器同一份判据）。"""
    return [tag for tag, tokens in (checks or [])
            if not any(prompt_rules.token_satisfied(t, content) for t in tokens)]


def _run_merged_jobs(
    jobs: list[dict],
    out_dir: Path,
    filename: str,
    doc_title: str,
    note: str,
    force: bool,
    legacy_name,
    make_system,
    make_user,
    fail_label,
    header_for: dict[str, str] | None = None,
    step: str = "未命名",
    style_lock_checks: dict[str, list[tuple]] | None = None,
    lib: Path | None = None,
) -> tuple[int, int, int]:
    """Run one LLM task per job, then merge all results into one numbered document."""
    merged = out_dir / filename
    cache_dir = out_dir / f".{filename}缓存"
    titles = [str(j["title"]) for j in jobs]
    known = set(titles)
    sections, recovered = recover_merged_cache(merged, cache_dir, known)

    migrated = False
    for job in jobs:
        title = str(job["title"])
        if title in sections:
            continue
        legacy = out_dir / legacy_name(job)
        if _should_skip(legacy, False):
            sections[title] = legacy.read_text(encoding="utf-8").strip()
            legacy.unlink()
            migrated = True
    if migrated or recovered:
        write_merged(merged, doc_title, note, titles, sections)

    if force:
        # --force 语义 = 真正重生成：先移除本次任务已有的旧内容，
        # 失败任务留空并计入失败，绝不把旧内容伪装成"已覆盖"。
        for title in titles:
            sections.pop(title, None)
        write_merged(merged, doc_title, note, titles, sections)

    results: dict[str, str] = {}

    def _event(event: str, node: str, detail: str) -> None:
        # 库根优先用调用参数：官方步骤表用的是 `--lib <库>`，并不导出 ASSET_LIB/ASSET_LIB_DIR，
        # 只认环境变量会让重试与「保留产出」事件一条都不落（留痕形同不存在）。
        lib_for_events = lib or os.environ.get("ASSET_LIB") or os.environ.get("ASSET_LIB_DIR")
        if lib_for_events:
            try:
                from engine.llm_pool import append_event
                append_event(lib_for_events, event, node, detail)
            except Exception:
                pass

    def run(job: dict) -> tuple[str, str | None, str]:
        title = str(job["title"])
        if not force and title in sections:
            return title, None, "skipped"
        asset_type = str(job.get("type") or "人物")
        extra = prompt_rules.generation_requirements(asset_type)
        # 按产物类型选自检：描述文档的判据（负面限制 + 各类型必填字段）与提示词的不同。
        checker = _compliance_checker(step)
        is_prompt = checker is prompt_rules.check_compliance
        content, _ = call_llm(make_system(job), make_user(job), MAX_TOKENS, min_len=80,
                              step=step, extra_requirements=extra, role="judgement")
        # 生成侧合规自检（工单 T11 提示词 / T3 描述）：与检查器同一份规则；
        # 不合格带纠正提示重试一次，不到位才走各自的下场（见下）。
        ok, missing = checker(content, asset_type) if checker else (True, [])
        if not ok:
            _event("generation_retry", title, "; ".join(missing[:4]))
            correction_prefix = "\n\n【合规纠正】你上一轮的输出不合规："
            correction = (correction_prefix + "；".join(missing)
                          + "。请严格按系统提示中的硬要求重新输出完整内容。")
            content, _ = call_llm(make_system(job), make_user(job) + correction, MAX_TOKENS,
                                  min_len=80, step=step, extra_requirements=extra, role="judgement")
            ok, missing = checker(content, asset_type) if checker else (True, [])
            if not ok and not is_prompt:
                # 描述类产物**没有**机械化修复规则可用——autofix 的五条规则都是提示词专用的
                # （例如往正文里插「负面提示词：」段），套到描述上只会写坏内容。
                # 故此处**保留产出**并大声记事件：描述整个丢掉比缺一个标记更糟，
                # 缺项由 verify 的 check_schedule 记名，不是静默放过。
                _event("generation_incompliant", title,
                       "描述缺项（产出已保留）: " + "; ".join(missing[:4]))
                log(f"  ⚠️ 描述缺项（产出已保留）：{title}（{'; '.join(missing[:3])}）")
                _write_cache(cache_dir, title, content)
                return title, content, "done"
            if not ok:
                # 闸门不再直接丢弃产出：先跑一遍机械化修复再复检。
                # 修好即采用；修不好才计失败，但内容仍落盘——避免「拦下即丢、
                # 而 autofix 只能修已落盘产出」的死锁（实测曾把整条链卡在第八步）。
                repaired, fixed_n = _repair_incompliant(content, asset_type)
                ok2, missing2 = prompt_rules.check_compliance(repaired, asset_type)
                if ok2:
                    _event("generation_repaired", title,
                           f"机械化修复 {fixed_n} 处后合规（原缺：{'; '.join(missing[:3])}）")
                    content = repaired
                    # 回写 ok：否则后面 `if ok and …` 恒假，用词检查在这条路径上整段被跳过
                    # （实测：机械修复成功的产物里，「极致」直达交付物而无人拦）。
                    ok, missing = ok2, missing2
                else:
                    _write_rejected(out_dir, title, repaired)
                    _event("generation_incompliant", title,
                           "GENERATION_INCOMPLIANT: " + "; ".join(missing2[:4]))
                    log(f"  ❌ GENERATION_INCOMPLIANT: {title}（{'; '.join(missing2[:3])}）")
                    return title, repaired, "failed"

        def _hard_ok(candidate: str) -> bool:
            """重试结果是否**硬判据也过关**；不过关就宁可留上一版（与用词检查同一口径）。"""
            return checker(candidate, asset_type)[0] if checker else True

        # 画风锁定分层逐字必查（工单 F11）：判据来自画风卡，此处与校验器共用同一份实现。
        # 缺层点名重试一次；仍缺**保留产出**并留痕——一个锁定层不该让整条资产消失
        # （validate 会如实记名），更不该把覆盖率一起带崩。
        lock_checks = (style_lock_checks or {}).get(asset_type) or []
        lock_missing = _lock_layer_missing(content, lock_checks)
        if lock_missing:
            _event("generation_style_lock", title, "缺画风锁定分层：" + "、".join(lock_missing[:3]))
            retried, _ = call_llm(make_system(job), make_user(job) + _lock_correction(lock_missing),
                                  MAX_TOKENS, min_len=80, step=step,
                                  extra_requirements=extra, role="judgement")
            if not _lock_layer_missing(retried, lock_checks) and _hard_ok(retried):
                content = retried
            else:
                _event("generation_style_lock_kept", title,
                       "重试后仍缺分层，产出已保留：" + "、".join(lock_missing[:3]))
                log(f"  ⚠️ 画风锁定分层缺失（产出已保留）：{title}（{'、'.join(lock_missing[:3])}）")
        # 结构性标记的软判据（优化项）：来源是**校验器自己的常量**（BASE_CHECKS/DERIV_CHECKS），
        # 生成侧不再手抄第二份清单——校验器新增规则时这里自动跟上，避免"规则分叉"复发。
        # 只作用于**提示词**产物：这些标记（三格图板/素灰背景…）是提示词模板的要素，
        # 描述文档另有其判据（check_desc_compliance），混用会把描述误判成缺标记。
        # 缺项点名重试一次；仍缺保留产出并留痕（与锁定分层同一策略）。
        struct_missing = (prompt_rules.extra_token_missing(content, asset_type, deriv=step == "衍生提示词")
                          if is_prompt else [])
        if struct_missing:
            _event("generation_struct", title, "缺结构标记：" + "、".join(struct_missing[:3]))
            retried, _ = call_llm(make_system(job), make_user(job) + _struct_correction(struct_missing),
                                  MAX_TOKENS, min_len=80, step=step,
                                  extra_requirements=extra, role="judgement")
            if not prompt_rules.extra_token_missing(retried, asset_type, deriv=step == "衍生提示词") \
                    and _hard_ok(retried):
                content = retried
            else:
                _event("generation_struct_kept", title,
                       "重试后仍缺，产出已保留：" + "、".join(struct_missing[:3]))
                log(f"  ⚠️ 结构标记缺失（产出已保留）：{title}（{'、'.join(struct_missing[:3])}）")
        # 用词审查（工单 F6②）：抽象空洞词在硬判据之外，命中先点名重试一次。
        # 仍命中**不判失败**——一个填充词升级成「丢掉整条资产」会把 verify 的覆盖率一起带崩
        # （本函数上方那条注释记的正是这个死锁的代价）；改为保留产出 + 大声留痕，由 verify 记名。
        if ok and checker is not None:
            taste = prompt_rules.abstract_word_hits(content)
            if taste:
                _event("generation_taste", title, "命中抽象空洞词：" + "、".join(taste[:3]))
                retried, _ = call_llm(
                    make_system(job), make_user(job) + _taste_correction(taste), MAX_TOKENS,
                    min_len=80, step=step, extra_requirements=extra, role="judgement")
                # 只接受**硬判据也过关、且不再命中**的重试结果：否则宁可留上一版
                if _hard_ok(retried) and not prompt_rules.abstract_word_hits(retried):
                    content = retried
                else:
                    _event("generation_taste_kept", title,
                           "重试后仍命中，产出已保留：" + "、".join(taste[:3]))
                    log(f"  ⚠️ 抽象空洞词未消除（产出已保留）：{title}（{'、'.join(taste[:3])}）")
        _write_cache(cache_dir, title, content)
        return title, content, "done"

    done = skip = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(run, j): j for j in jobs}
        for fut in as_completed(futs):
            try:
                title, content, st = fut.result()
            except Exception as e:
                job = futs[fut]
                log(f"  ❌ {fail_label} {job.get('label', '')} 失败: {e}")
                continue
            if st == "done":
                done += 1
                results[title] = content or ""
            elif st == "skipped":
                skip += 1

    if results:
        for title, content in list(results.items()):
            header = (header_for or {}).get(title, "")
            if header and header not in content:
                results[title] = header + "\n" + content
        sections.update(results)
        write_merged(merged, doc_title, note, titles, sections)
        for f in cache_dir.glob("*.md"):
            f.unlink()
        try:
            cache_dir.rmdir()
        except OSError:
            pass
    return done, skip, len(jobs) - done - skip


def _should_skip(out: Path, force: bool) -> bool:
    return out.exists() and out.stat().st_size > 100 and not force


def generate_enrich(
    rows: list[dict[str, str]],
    genre: str,
    lib: Path,
    force: bool,
    style_block: str = "",
) -> int:
    """Generate visual descriptions; each asset type merges into one numbered document."""
    out_dir = lib / "描述"
    out_dir.mkdir(parents=True, exist_ok=True)
    system_tmpl, user_tmpl = load_template("描述补全模板.md")
    system = system_tmpl.format(genre=genre, style_block=style_block)

    jobs: list[dict] = []
    for row in rows:
        jobs.append({
            "title": row["name"],
            "type": row["type"],
            "name": row["name"],
            "level": row["level"],
            "note": row.get("note", ""),
            "label": row["name"],
        })

    total_ok = total_skip = total_fail = 0
    print(f"📝 描述补全: {len(jobs)} 个任务，按类型合并为 人物/场景/道具 三个文档 → {out_dir}")
    for type_key, doc_title in (("人物", "人物描述（按序号）"), ("场景", "场景描述（按序号）"), ("道具", "道具描述（按序号）")):
        typed = [j for j in jobs if j["type"] == type_key]
        if not typed:
            continue

        def legacy_name(job: dict, _t: str = type_key) -> str:
            return f"{_t}_{safe_name(job['name'])}.md"

        def make_system(job: dict) -> str:
            return system

        def make_user(job: dict) -> str:
            return user_tmpl.format(
                genre=genre, type=job["type"], name=job["name"],
                level=job["level"], note=job["note"], emphasis=read_emphasis(lib),
            )

        note = f"> 每个{type_key}资产的视觉描述；序号与反推文件{type_key}资产表顺序一致。"
        ok, skip, fail = _run_merged_jobs(
            typed, out_dir, f"{type_key}描述.md", doc_title, note, force,
            legacy_name, make_system, make_user, f"{type_key}描述", step="描述补全", lib=lib,
        )
        total_ok += ok
        total_skip += skip
        total_fail += fail
        print(f"  {type_key}: 生成 {ok}, 跳过 {skip}, 失败 {fail}")

    print(f"✅ 描述补全: 生成 {total_ok}, 跳过 {total_skip}, 失败 {total_fail}")
    return total_fail


def read_emphasis(lib: Path) -> str:
    """Read optional 强调清单.md and return machine-consumable text for prompts."""
    path = lib / "强调清单.md"
    if not path.exists():
        return "无额外强调（技能将根据剧本自动推测）"
    blocks = {"强化": [], "避免": []}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line == "## 必须强化":
            current = "强化"
            continue
        if line == "## 必须避免":
            current = "避免"
            continue
        if line.startswith("## "):
            current = None
            continue
        if current and line.startswith("- "):
            blocks[current].append(line)
    out = []
    if blocks["强化"]:
        out.append("## 必须强化")
        out.extend(blocks["强化"])
    if blocks["避免"]:
        out.append("## 必须避免")
        out.extend(blocks["避免"])
    return "\n".join(out) if out else "无额外强调（技能将根据剧本自动推测）"


def build_costume_map(text: str) -> dict[str, list[str]]:
    """Build character → costume names map from the costume table.

    **只挡显式占位符**（`_PLACEHOLDER_BINDINGS`），刻意**不**用 `_UNBOUND_BINDING_MARKERS`
    （对抗评审 B1）：这一处是**查表**，键就是角色名；一旦把合法人名误判成未绑定，
    该角色的服装会**静默绑不上**且没有任何留痕。实测缺陷只出在 `build_default_schedule`
    （写调度行，误判会留痕）那边，这里放宽没有任何证据支持，只有静默退化的风险。

    保留 `待确认`/`未知` 这类键是**有意为之**：它们只会被人物表里的真名查到，
    而人物表里不会有这些名字 → 无害；反过来若在此处过滤，就会退回 B1 的静默丢失。
    """
    costumes: dict[str, list[str]] = {}
    for cells in parse_asset_table(text, "服装妆造"):
        if len(cells) >= 3 and cells[2].strip() and cells[2].strip() not in _PLACEHOLDER_BINDINGS:
            costumes.setdefault(cells[2].strip(), []).append(cells[1].strip())
    return costumes


def generate_base(
    rows: list[tuple[str, list[str]]],
    genre: str,
    lib: Path,
    force: bool,
    style_block: str = "",
    render_block: str = "",
    text: str = "",
    view_mode: str = "full",
    pack: dict | None = None,
    render_lock_block: str = "",
    negative_blocks: dict[str, str] | None = None,
    view_blocks: dict[str, str] | None = None,
    style_lock_checks: dict[str, list[tuple]] | None = None,
) -> int:
    """Generate base prompts; each asset type merges into one numbered document."""
    out_dir = lib / "提示词" / "基础"
    out_dir.mkdir(parents=True, exist_ok=True)
    costumes = build_costume_map(text) if text else {}
    tmpl_map = {
        "人物": "基础提示词模板-人物.md",
        "场景": "基础提示词模板-场景.md",
        "道具": "基础提示词模板-道具.md",
    }

    jobs: list[dict] = []
    asset_index = index_by_asset_id(load_asset_index(lib))
    prefix_map = {"人物": "CH_", "场景": "SC_", "道具": "PR_"}
    skipped_p3: list[str] = []
    for type_key, cells in rows:
        raw = cells[1].strip()
        aid = f"{prefix_map.get(type_key, '')}{raw}"
        asset = asset_index.get(aid)
        ok, reason = should_generate_base(asset)
        if not ok:
            skipped_p3.append(format_skip_entry(aid, type_key, raw, reason))
            continue
        jobs.append({
            "title": cells[1].strip(),
            "type": type_key,
            "name": safe_name(cells[1]),
            "raw_name": cells[1].strip(),
            "level": cells[-2] if len(cells) >= 2 else "",
            "label": cells[1].strip(),
        })
    # 服装不与人物同表：它走「绑定人物 × 造型」注入人物提示词，故没有自己的基础提示词文件。
    # 绑定悬空（待确认/未知/空）时既绑不上人、又不进跳过清单 → 覆盖率门报
    # 「既无产物又无原因的资产：CO_猩猩战袍」而阻断签发（实测）。这里按调度行同款口径留痕：
    # 写进跳过清单（理由含「待确认」，覆盖率门认它，check_schedule 的 legal_skip_ids 不认——
    # 服装本就不在那三张表的覆盖率检查里，两边都不放水）。
    for cells in parse_asset_table(text, "服装妆造") if text else []:
        if len(cells) >= 3 and _is_unbound_binding(cells[2].strip()):
            name = cells[1].strip()
            aid = cells[0].strip() if re.match(r"^[A-Z]{2}_", cells[0].strip()) else f"CO_{name}"
            skipped_p3.append(format_skip_entry(
                aid, "服装", name,
                f"绑定待确认（绑定列：{cells[2].strip() or '空'}）：暂无可挂载的人物，不产出绑定型服装提示词"))
    if skipped_p3:
        skip_path = out_dir / "跳过清单.md"
        skip_path.write_text(
            "# 基础提示词跳过清单（ADR-0084）\n\n"
            + "\n".join(f"- {s}" for s in skipped_p3) + "\n",
            encoding="utf-8",
        )
        print(f"⏭️ 明确排除（非资产/用户取消/绑定待确认）{len(skipped_p3)} 项（见 跳过清单.md）")

    total_ok = total_skip = total_fail = 0
    person_file = "人物提示词.md" if view_mode == "full" else "人物提示词_裁头版.md"
    view_desc = "两格全身均保留头部（不裁头）；左面特写与右面正视图必须是同一张脸、同一发型、同一妆面" if view_mode == "full" else "两格全身头部与面部一律裁除不入画（裁头版），脸的唯一真相是左面正面特写；三格共用同一身份锁，右面两格无面部"
    print(f"🎨 基础提示词: {len(jobs)} 个任务，按类型合并为 人物/场景/道具 三个文档 → {out_dir}（人物视图：{view_desc}）")
    person_doc_title = "人物基础提示词（按序号）" if view_mode == "full" else "人物基础提示词-裁头版（按序号）"
    prefix_map2 = {"人物": "CH_", "场景": "SC_", "道具": "PR_"}
    asset_index2 = index_by_asset_id(load_asset_index(lib))
    for type_key, doc_title in (("人物", person_doc_title), ("场景", "场景基础提示词（按序号）"), ("道具", "道具基础提示词（按序号）")):
        typed = [j for j in jobs if j["type"] == type_key]
        if not typed:
            continue
        tmpl_name = tmpl_map[type_key]
        system_tmpl, user_tmpl = load_template(tmpl_name)
        block = render_block if type_key == "道具" else style_block

        def make_system(job: dict) -> str:
            desc = find_section(lib / "描述" / f"{job['type']}描述.md", job["raw_name"]) or ""
            hay = desc
            if job["type"] == "人物":
                hay += "；".join(costumes.get(str(job["raw_name"]), []))
            mblock = style_lib.build_material_block(pack, type_key, hay) if pack else ""
            neg = (negative_blocks or {}).get(type_key, "")
            vblock = (view_blocks or {}).get(type_key, "")
            if type_key == "人物":
                return system_tmpl.format(
                    genre=genre, style_block=block, render_block=block, view_mode=view_desc,
                    render_lock_block=render_lock_block, material_block=mblock, negative_block=neg,
                    view_block=vblock)
            return system_tmpl.format(
                genre=genre, style_block=block, render_block=block,
                render_lock_block=render_lock_block, material_block=mblock, negative_block=neg,
                view_block=vblock)

        def legacy_name(job: dict, _t: str = type_key) -> str:
            if _t == "人物" and view_mode == "crop":
                # 裁头版是新产物，不迁移旧版单文件（旧文件只归入不裁头版）
                return f"人物_裁头版_{safe_name(job['raw_name'])}.md"
            return f"{_t}_{safe_name(job['raw_name'])}.md"

        def make_user(job: dict) -> str:
            desc = find_section(lib / "描述" / f"{job['type']}描述.md", job["raw_name"])
            if not desc:
                legacy_desc = lib / "描述" / f"{job['type']}_{job['name']}.md"
                if legacy_desc.exists():
                    desc = legacy_desc.read_text(encoding="utf-8").strip()
            bound = "；".join(costumes.get(str(job["raw_name"]), [])) or "无"
            return user_tmpl.format(
                genre=genre, name=job["raw_name"], level=job["level"],
                description=desc, bound_costumes=bound, emphasis=read_emphasis(lib),
            )

        note = f"> 每个{type_key}资产的基础提示词；序号与反推文件{type_key}资产表顺序一致。"
        merged_name = person_file if type_key == "人物" else f"{type_key}提示词.md"
        header_for = {}
        for j in typed:
            aid = f"{prefix_map2.get(type_key, '')}{j['raw_name']}"
            a = asset_index2.get(aid)
            extra = None
            if a and a.get("fact_level") == "待定冲突":
                extra = {"生成状态": "阻断-待定冲突：草案，需人工裁决后转正"}
            header_for[j["title"]] = format_prompt_header(a, extra=extra)
        ok, skip, fail = _run_merged_jobs(
            typed, out_dir, merged_name, doc_title, note, force,
            legacy_name, make_system, make_user, f"{type_key}基础", step="基础提示词", lib=lib,
            header_for=header_for, style_lock_checks=style_lock_checks,
        )
        total_ok += ok
        total_skip += skip
        total_fail += fail
        print(f"  {type_key}: 生成 {ok}, 跳过 {skip}, 失败 {fail}")

    print(f"✅ 基础提示词: 生成 {total_ok}, 跳过 {total_skip}, 失败 {total_fail}")
    return total_fail


def generate_deriv(
    items: list[dict[str, str]],
    genre: str,
    lib: Path,
    force: bool,
    style_block: str = "",
    render_block: str = "",
    view_mode: str = "full",
    card: dict | None = None,
    mode: str = "card",
    pack: dict | None = None,
    render_lock_block: str = "",
    negative_blocks: dict[str, str] | None = None,
    view_blocks: dict[str, str] | None = None,
    style_lock_checks: dict[str, list[tuple]] | None = None,
) -> int:
    """Generate derivative prompts; each asset type merges into one numbered document."""
    out_dir = lib / "提示词" / "衍生"
    out_dir.mkdir(parents=True, exist_ok=True)
    system_tmpl, user_tmpl = load_template("衍生提示词模板.md")
    base_dir = lib / "提示词" / "基础"

    jobs: list[dict] = []
    no_channel: list[str] = []
    type_by_name = load_asset_types(lib)
    for item in items:
        # 防御（手改调度清单 / 旧库遗留）：类型以索引为准，无衍生通道的类型记名略过，
        # 不得让"服装被写成道具"这种类型错一路走到 make_user 里抛错停链。
        kind = type_by_name.get(str(item["资产"]).strip(), "") or item["类型"]
        if kind not in DERIV_CHANNEL_TYPES:
            no_channel.append(f"{item['资产']}（{kind}）")
            continue
        jobs.append({
            "title": f"{item['资产']}（{item['变体']}）",
            "type": kind,
            "asset": item["资产"],
            "variant": item["变体"],
            "note": item.get("备注", ""),
            "style_override": item.get("画风", ""),
            "母版编号": item.get("母版编号", ""),
            "仅允许变化": item.get("仅允许变化", ""),
            "label": f"{item['资产']}_{item['变体']}",
        })
    if no_channel:
        msg = "、".join(no_channel)
        print(f"  ℹ️ 略过 {len(no_channel)} 条无衍生通道的需求（该类型没有衍生模板，"
              f"服装变体请走服饰设计线）：{msg}")
        try:
            from engine.llm_pool import append_event
            append_event(lib, "deriv_skipped_no_channel", "-", msg)
        except Exception:      # noqa: BLE001 — 留痕失败不得反过来打断生成
            pass

    total_ok = total_skip = total_fail = 0
    person_file = "人物提示词.md" if view_mode == "full" else "人物提示词_裁头版.md"
    view_desc = "两格全身均保留头部（不裁头）；左面特写与右面正视图必须是同一张脸、同一发型、同一妆面" if view_mode == "full" else "两格全身头部与面部一律裁除不入画（裁头版），脸的唯一真相是左面正面特写；三格共用同一身份锁，右面两格无面部"
    print(f"🎨 衍生提示词: {len(jobs)} 个变体，按类型合并为 人物/场景/道具 三个文档 → {out_dir}（人物视图：{view_desc}）")
    person_doc_title = "人物衍生提示词（按序号）" if view_mode == "full" else "人物衍生提示词-裁头版（按序号）"
    for type_key, doc_title in (("人物", person_doc_title), ("场景", "场景衍生提示词（按序号）"), ("道具", "道具衍生提示词（按序号）")):
        typed = [j for j in jobs if j["type"] == type_key]
        if not typed:
            continue

        def legacy_name(job: dict, _t: str = type_key) -> str:
            return f"{_t}_{safe_name(job['asset'])}_{safe_name(job['variant'])}.md"

        def make_system(job: dict) -> str:
            use_style, use_render, use_rlb = style_block, render_block, render_lock_block
            use_card, use_pack = card, pack
            if job.get("style_override"):
                c2, m2 = style_lib.resolve_style(genre, job["style_override"])
                if c2 and m2 == "card":
                    use_card = c2
                    use_pack = style_lib.load_stylepack(str(c2.get("pack", "")))
                    use_style = style_lib.build_style_block(c2)
                    if use_pack and use_pack.get("invariants"):
                        use_style += "\n\n" + style_lib.build_pack_invariants_block(use_pack)
                    use_render = style_lib.build_render_block(c2)
                    use_rlb = style_lib.build_render_lock_text(c2)
            block = use_render if job["type"] == "道具" else use_style
            mblock = style_lib.build_material_block(
                use_pack, job["type"], f"{job.get('asset', '')}；{job.get('note', '')}"
            ) if use_pack else ""
            vblock = style_lib.build_view_block(use_pack, job["type"]) if use_pack else (view_blocks or {}).get(job["type"], "")
            neg = style_lib.build_negative_block(use_card, use_pack, job["type"]) if use_card else (negative_blocks or {}).get(job["type"], "")
            # 衍生模板为三类型共用（含 {view_mode} 占位符），全类型都要传入
            return system_tmpl.format(
                genre=genre, style_block=block, render_block=block, view_mode=view_desc,
                render_lock_block=use_rlb, material_block=mblock, negative_block=neg,
                view_block=vblock)

        def make_user(job: dict) -> str:
            base_prompt = "（未提供）"
            base_file = person_file if job["type"] == "人物" else f"{job['type']}提示词.md"
            base_prompt = find_section(base_dir / base_file, job["asset"])
            if not base_prompt and job["type"] == "人物" and person_file != MERGED_PERSON_FILE:
                # 裁头版基础缺失时，回退不裁头版基础提示词（身份锁同源）
                base_prompt = find_section(base_dir / MERGED_PERSON_FILE, job["asset"])
            if not base_prompt:
                legacy = base_dir / f"{job['type']}_{safe_name(job['asset'])}.md"
                if legacy.exists():
                    base_prompt = legacy.read_text(encoding="utf-8").strip()
            if not base_prompt:
                # 人物身份锁、场景环境基线、道具材质身份的唯一来源都是基础提示词原文；
                # 任何类型缺失都禁止让模型自由发挥，直接失败并提示先跑 --base。
                raise ValueError(f"{job['type']}「{job['asset']}」基础提示词缺失，请先运行 --base（--force 重跑基础）")
            # 工单C：场景派生必须先验证母版 ID，再将其已确认字段与“仅允许变化”注入请求
            master_block = ""
            if job["type"] == "场景":
                master_block = _build_scene_master_block(lib, job)
            return user_tmpl.format(
                genre=genre, type=job["type"], asset=job["asset"],
                variant=job["variant"], note=job["note"], base_prompt=base_prompt,
                emphasis=read_emphasis(lib),
                master_block=master_block,
            )

        note = f"> 每个{type_key}资产变体的衍生提示词；序号与调度清单{type_key}行顺序一致。"
        merged_name = person_file if type_key == "人物" else f"{type_key}提示词.md"
        # ADR-0084：待定冲突只阻断受影响任务；场景派生继承母版锚点（仅提示，不改写正文）
        deriv_index = index_by_asset_id(load_asset_index(lib))
        deriv_prefix = {"人物": "CH_", "场景": "SC_", "道具": "PR_"}
        blocked: list[str] = []
        allowed: list[dict] = []
        for j in typed:
            aid = f"{deriv_prefix.get(type_key, '')}{j['asset']}"
            asset = deriv_index.get(aid)
            if asset and asset.get("fact_level") == "待定冲突":
                blocked.append(f"{type_key}「{j['asset']}」待定冲突：跳过变体 {j['variant']}")
                continue
            allowed.append(j)
        if blocked:
            print(f"⏭️ 待定冲突阻断 {len(blocked)} 个衍生任务（仅受影响任务）")
            for b in blocked[:5]:
                print(f"     - {b}")
        header_for_deriv = {}
        for j in allowed:
            aid = f"{deriv_prefix.get(type_key, '')}{j['asset']}"
            extra = {}
            if j.get("母版编号"):
                extra["母版编号"] = j["母版编号"]
            if j.get("仅允许变化"):
                extra["仅允许变化"] = j["仅允许变化"]
            header_for_deriv[j["title"]] = format_prompt_header(deriv_index.get(aid), extra or None)
        ok, skip, fail = _run_merged_jobs(
            allowed, out_dir, merged_name, doc_title, note, force,
            legacy_name, make_system, make_user, f"{type_key}衍生", step="衍生提示词", lib=lib,
            header_for=header_for_deriv, style_lock_checks=style_lock_checks,
        )
        total_ok += ok
        total_skip += skip
        total_fail += fail
        print(f"  {type_key}: 生成 {ok}, 跳过 {skip}, 失败 {fail}")

    print(f"✅ 衍生提示词: 生成 {total_ok}, 跳过 {total_skip}, 失败 {total_fail}")
    return total_fail


def main() -> int:
    parser = argparse.ArgumentParser(description="提示词生成引擎")
    parser.add_argument("--base", help="反推_最终.md 路径（基础生成）")
    parser.add_argument("--enrich", help="反推_最终.md 路径（仅描述补全）")
    parser.add_argument("--deriv", action="store_true", help="衍生生成（读 调度清单.md）")
    parser.add_argument("--shadow", action="store_true",
                        help="shadow 模式：同一总账产新旧两套提示词并写差异报告，不覆盖正式产物")
    parser.add_argument("--init-schedule", help="从反推_最终.md 生成默认调度清单")
    parser.add_argument("--genre", help="题材（古风/现代/民国/仙侠...）；--yes 自动化时必须显式传入")
    parser.add_argument("--style", help="画风：画风库内名称/编号（如 H7/A2）或自由描述；不传则按 --genre 查映射表默认画风")
    parser.add_argument("--yes", action="store_true", help="跳过画风确认（自动化场景用）")
    parser.add_argument("--lib", default=str(ASSET_LIB_DIR))
    parser.add_argument("--force", action="store_true", help="覆盖已存在输出")
    parser.add_argument("--append", action="store_true", help="仅对 --init-schedule：已存在清单时只追加默认新行，不覆盖手工编辑")
    parser.add_argument("--view-mode", choices=("full", "crop"), default="full", help="人物视图版：full=不裁头（默认），crop=裁头版")
    args = parser.parse_args()

    actions = []
    if args.init_schedule:
        actions.append("--init-schedule")
    if args.enrich:
        actions.append("--enrich")
    if args.base:
        actions.append("--base")
    if args.deriv:
        actions.append("--deriv")
    if args.shadow:
        actions.append("--shadow")
    if len(actions) > 1:
        print(f"❌ 一次只能执行一个生成动作：{', '.join(actions)}（避免半跑和静默忽略参数）")
        return 1
    if args.view_mode == "crop" and not (args.base or args.deriv):
        print("❌ --view-mode crop 只对 --base / --deriv 生效（--enrich/--init-schedule 不生成人物视图）")
        return 1
    if args.append and not args.init_schedule:
        print("❌ --append 只允许与 --init-schedule 搭配使用")
        return 1

    lib = Path(args.lib)

    if args.shadow:
        from engine.prompt_compiler import run_shadow
        report = run_shadow(lib)
        print(f"🧪 shadow 差异报告: {report}\n   正式产物未被改写（shadow 只写差异报告，不覆盖正式）")
        return 0

    if args.init_schedule:
        text = Path(args.init_schedule).read_text(encoding="utf-8")
        schedule = build_default_schedule(text, args.genre, lib=lib)
        sched_dir = lib / "提示词"
        sched_dir.mkdir(parents=True, exist_ok=True)
        path = sched_dir / "调度清单.md"
        if path.exists() and not args.force:
            if args.append:
                existing = {(it["类型"], it["资产"], it["变体"]) for it in parse_schedule_list(path.read_text(encoding="utf-8"))}
                new_items = parse_schedule_list(schedule)
                added = [it for it in new_items if (it["类型"], it["资产"], it["变体"]) not in existing]
                if not added:
                    print(f"ℹ️  调度清单已存在且无新增默认行: {path}")
                    return 0
                existing_text = path.read_text(encoding="utf-8")
                with path.open("a", encoding="utf-8") as f:
                    if existing_text and not existing_text.endswith("\n"):
                        f.write("\n")
                    for it in added:
                        f.write(f"| {it['类型']} | {it['资产']} | {it['变体']} | {it.get('备注', '')} | {it.get('画风', '')} |\n")
                print(f"📋 调度清单已追加 {len(added)} 行: {path}")
                return 0
            print(f"ℹ️  调度清单已存在: {path}\n   保留现有内容（可能含手工编辑）；如需重建默认清单请加 --force。")
            return 0
        path.write_text(schedule, encoding="utf-8")
        print(f"📋 调度清单: {path}\n请检查/编辑后运行 --deriv 生成")
        return 0

    if args.view_mode == "crop" and (args.base or args.deriv):
        full_person = lib / "提示词" / "基础" / MERGED_PERSON_FILE
        if not full_person.exists():
            print(f"❌ 裁头版是补充版：未检测到默认不裁头版 {full_person}；请先运行 --base --view-mode full 生成 full 版，再补 crop 版。")
            return 1

    if args.yes and not args.style:
        print("❌ --yes 用于自动化/管道场景时必须显式传 --style（画风必须明确指定，禁止静默默认）")
        return 1
    if args.yes and not args.genre:
        print("❌ --yes 用于自动化/管道场景时必须显式传 --genre（题材必须明确指定，禁止静默默认古风）")
        return 1

    card, mode = style_lib.resolve_style(args.genre, args.style)
    free_desc = args.style if mode == "free" else None
    confirmed = style_lib.confirm_style(args.genre, card, mode, free_desc, yes=args.yes)
    if confirmed is None:
        return 1
    card, mode, free_desc = confirmed
    pack = style_lib.load_stylepack(str(card.get("pack", ""))) if (mode == "card" and card) else None
    style_block = style_lib.build_style_block(card, free_desc if mode == "free" else None)
    if pack and pack.get("invariants"):
        style_block += "\n\n" + style_lib.build_pack_invariants_block(pack)
    render_block = style_lib.build_render_block(card) if mode == "card" else style_block
    render_lock_block = style_lib.build_render_lock_text(card) if mode == "card" else ""
    # 画风锁定分层逐字必查：判据与校验器同源（engine.prompt_rules.style_lock_layer_checks），
    # 生成时就比对、缺层就地重试——不再等 verify 才发现模型漏抄了一层
    style_lock_checks = (
        {t: prompt_rules.style_lock_layer_checks(card, t) for t in ("人物", "场景", "道具")}
        if mode == "card" and card else {}
    )
    negative_blocks = (
        {t: style_lib.build_negative_block(card, pack, t) for t in ("人物", "场景", "道具")}
        if mode == "card" else {t: "" for t in ("人物", "场景", "道具")}
    )
    view_blocks = (
        {t: style_lib.build_view_block(pack, t) for t in ("人物", "场景", "道具")}
        if mode == "card" else {t: "" for t in ("人物", "场景", "道具")}
    )
    if mode == "card":
        pack_note = f"，风格包 {card['pack']}" if card and card.get("pack") else ""
        print(f"🎨 画风锁定: {card['id']} {card['name']}{pack_note}（已注入所有生成模板）")
    else:
        print("🎨 画风锁定: 自由描述（已原样注入）")

    if args.enrich or args.base:
        target = args.enrich or args.base
        text = Path(target).read_text(encoding="utf-8")
        raw_rows: list[tuple[str, list[str]]] = []
        for t in ("人物", "场景", "道具"):
            for cells in parse_asset_table(text, f"{t}资产"):
                raw_rows.append((t, cells))
        if not raw_rows:
            print(f"❌ 反推文件未解析到任何资产表（人物/场景/道具）: {target}")
            print("   请确认是步骤 3 聚合产物（含『人物资产提取表』等模块），或检查文件路径。")
            return 1
        if args.enrich:
            rows = [
                {"type": t, "name": cells[1],
                 "level": cells[-2] if len(cells) >= 2 else "", "note": ""}
                for t, cells in raw_rows
            ]
            return 1 if generate_enrich(rows, args.genre, lib, args.force, style_block=style_block) else 0
        return 1 if generate_base(
            raw_rows, args.genre, lib, args.force,
            style_block=style_block, render_block=render_block, text=text,
            view_mode=args.view_mode,
            pack=pack, render_lock_block=render_lock_block, negative_blocks=negative_blocks,
            view_blocks=view_blocks, style_lock_checks=style_lock_checks,
        ) else 0

    if args.deriv:
        sched_path = lib / "提示词" / "调度清单.md"
        if not sched_path.exists():
            print(f"❌ 调度清单不存在: {sched_path}（先运行 --init-schedule）")
            return 1
        items = parse_schedule_list(sched_path.read_text(encoding="utf-8"))
        return 1 if generate_deriv(
            items, args.genre, lib, args.force,
            style_block=style_block, render_block=render_block,
            view_mode=args.view_mode,
            card=card, mode=mode, pack=pack,
            render_lock_block=render_lock_block, negative_blocks=negative_blocks,
            view_blocks=view_blocks, style_lock_checks=style_lock_checks,
        ) else 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
