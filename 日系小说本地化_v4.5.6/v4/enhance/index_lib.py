"""v4 Enhance · 用户索引库只读适配器。

解析顺序（优先级从高到低）：
    CLI 参数 --index-lib > 工作区配置 > 环境/`.env` INDEX_LIB_DIR > 默认可发现路径 > 不启用

职责：
- 扫描索引库目录，把 章节索引/小说索引/文风卡/原文/判官验证 归为资产记录；
- 每条资产记录包含归一化相对路径、绝对路径、SHA-256、大小、读取时间、解析元数据；
- 按标签/场景检索章节索引、小说索引和文风卡，返回受限片段与位置指针；
- 全程只读：不修改索引库任何文件，不建状态文件。

约束：
- 返回的文本一律使用受限片段（见 _util.bounded），不把长句原样透传；
- 索引库只作风格/结构/节奏参考，不参与 Core 事实门槛判定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ._util import (
    bounded,
    dotenv_values,
    flatten_tags,
    looks_like_index_lib,
    mtime_iso,
    normalize_relpath,
    now_iso,
    parse_frontmatter,
    read_first_lines,
    read_frontmatter_block,
    split_pipe_tags,
)

KIND_DIRS: dict[str, str] = {
    "index_chapter": "章节索引",
    "index_novel": "小说索引",
    "style_card": "文风蒸馏卡",
    "index_original": "小说原文",
    "judge": "判官验证",
}

_CHAPTER_HEADING = re.compile(r"^##\s+(.+?)(?:（行号[:：]\s*(\d+)\s*~\s*(\d+)\s*）)?$")
_SECTION_HEADING = re.compile(r"^###\s+(.+)$")
_BULLET = re.compile(r"^[-*]\s+(.*)$")

_FRONTMATTER_FENCE = "---"
#: 原文文件里不属于正文的行（书名/作者等元信息、插图占位、章标题）
_PROSE_NOISE_PREFIXES = ("小说名称", "作者", "章节范围", "对应索引", "分段方式", "来源", "备注")
_PROSE_NOISE_RE = re.compile(
    r"^\s*(?:插图\s*p?\d*|image\s*\d*|第[一二三四五六七八九十百零\d]+[章卷节][^\n]{0,40})$",
    re.IGNORECASE,
)


def _strip_frontmatter(text: str) -> str:
    """去掉原文文件头部的 frontmatter 块（``---`` 包起来的一段）。"""
    stripped = text.lstrip()
    if not stripped.startswith(_FRONTMATTER_FENCE):
        return text
    end = stripped.find("\n" + _FRONTMATTER_FENCE, len(_FRONTMATTER_FENCE))
    if end == -1:
        return text
    return stripped[end + len(_FRONTMATTER_FENCE) + 1 :]


def _is_prose_noise(para: str) -> bool:
    """判断一段是否属于"非正文"，不得作为文风范例。"""
    if para.startswith(_PROSE_NOISE_PREFIXES):
        return True
    return bool(_PROSE_NOISE_RE.match(para))


@dataclass(frozen=True)
class AssetRecord:
    """一条索引库资产记录。"""

    kind: str
    rel_path: str                       # 归一化相对路径（POSIX）
    absolute_path: Path
    sha256: str | None = None           # 未哈希时为 None，见 hash_asset()
    size: int = 0
    modified_at: str = ""
    scanned_at: str = ""
    tags: tuple[str, ...] = ()          # 展开后的标签（含同义词）
    tag_pairs: tuple[tuple[str, tuple[str, ...]], ...] = ()
    scene: str | None = None            # 文风卡场景（场景卡才有）
    card_type: str | None = None        # 风格卡/场景卡
    book: str | None = None             # 书名（小说索引/风格卡）
    author: str | None = None
    verified: bool | None = None        # 卡是否已验证
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def asset_kind(self) -> str:
        """映射到审计 asset_kind（enhance_use 用）。"""
        return self.kind


@dataclass(frozen=True)
class ChapterSection:
    chapter_title: str
    line_range: tuple[int, int] | None
    subsections: dict[str, tuple[tuple[str, ...], ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class IndexHit:
    """检索命中：资产 + 匹配位置 + 受限片段。"""

    asset: AssetRecord
    matched_tags: list[str]
    section: str
    location: str
    snippet: str


def _env_var(key: str) -> str | None:
    import os

    return os.environ.get(key)


def _workspace_config(workspace: Path | None, key: str) -> str | None:
    if workspace is None:
        return None
    for env_name in (".enhance.env", ".env"):
        values = dotenv_values(workspace / env_name)
        if key in values:
            return values[key]
    return None


def _project_envs(key: str, base: Path) -> list[str]:
    """收集 v4 根和技能根 `.env` 中的候选值。"""
    found: list[str] = []
    for candidate in (base / "v4" / ".env", base / ".env", base.parent / ".env"):
        values = dotenv_values(candidate)
        if key in values and values[key]:
            found.append(values[key])
    return found


def resolve_index_lib(
    cli_arg: str | Path | None = None,
    *,
    workspace: Path | None = None,
    env: Mapping[str, str] | None = None,
    base: Path | None = None,
    default_candidates: Sequence[str | Path] = (),
) -> Path | None:
    """按 `CLI > 工作区 > env/.env > 默认发现` 解析索引库目录。

    返回 None 表示未配置/未发现（不启用增强）。返回的路径可能存在也可能不存在，
    存在性由调用方用 `validate_index_lib()` 判定并记录 enhance_unavailable。
    """
    if cli_arg:
        return Path(cli_arg).expanduser()
    workspace_value = _workspace_config(workspace, "INDEX_LIB_DIR")
    if workspace_value:
        return Path(workspace_value).expanduser()
    if env and env.get("INDEX_LIB_DIR"):
        return Path(env["INDEX_LIB_DIR"]).expanduser()
    base_dir = Path(base) if base else Path(__file__).resolve().parents[2]
    for value in _project_envs("INDEX_LIB_DIR", base_dir):
        if value:
            return Path(value).expanduser()
    for candidate in default_candidates:
        path = Path(candidate).expanduser()
        if looks_like_index_lib(path):
            return path
    # 已登记索引库（lib add 注册的主库）
    # 此处不得再用 looks_like_index_lib 做门禁：登记了就是用户的意图，
    # 路径失联/形态不对都必须原样交回调用方，由 validate_index_lib 记
    # enhance_unavailable 并让 require_index_lib 硬失败。若在此静默 continue，
    # 就会回落到下面的 skill 内置占位库，manifest 反而记成 enabled/used=true，
    # 用户会以为自己的知识库在工作，实际只喂进 2.3KB 通用规则。
    try:
        from ..core.library_registry import primary_library

        primary = primary_library()
        if primary and primary.get("path"):
            return Path(primary["path"]).expanduser()
    except Exception:  # noqa: BLE001 - 登记文件损坏不阻断
        pass
    # skill 自带索引库仅作兜底（用户把书放进 <skill>/索引库/ 也可用）
    for built_in in (base_dir / "索引库", base_dir.parent / "索引库"):
        if looks_like_index_lib(built_in):
            return built_in
    return None


def validate_index_lib(root: Path) -> str | None:
    """检查索引库是否可读；返回 None 表示可用，否则返回不可用原因。"""
    if not root.exists():
        return f"索引库目录不存在: {root}"
    if not root.is_dir():
        return f"索引库路径不是目录: {root}"
    if not looks_like_index_lib(root):
        return f"目录缺少索引库形态（章节索引/小说索引/文风蒸馏卡 等）: {root}"
    return None


#: 场景强项等级 → 选卡优先级（数字越小越优先）。
#:
#: 为什么必须消费等级（2026-09-17 实测）：`find_style_cards` 原先只对
#: `场景强项` 做子串匹配、**完全不读等级**，于是 12 张卡在「日常过渡」上
#: 完全并列，顺序退化为文件名排序——而 `仙逆-耳根-风格卡.md` 在 Unicode
#: 码位下恰好排第一，学园日常章因此被喂了《仙逆》第1005~1014章的修真大战
#: 当文风范例（审计实证：style_card ×8、style_exemplar ×5 全是仙逆）。
#: 「场景→推荐卡矩阵」里标的 A/B 级此前是**死数据**。
_STRENGTH_RANK: dict[str, int] = {
    "S": 0, "A+": 1, "A": 2, "A-": 3, "B+": 4, "B": 5, "B-": 6, "C": 7,
}
#: 场景专卡（frontmatter 带 `场景:`）是为该场景而建，按**严格最高档**对待：
#: 若与 A 级书卡同级，稳定排序会让文件顺序决定胜负（`甲` U+7532 < `龙` U+9F99），
#: 等于等级没起作用。故取 -1，严格压过 S 级（0）。
_SCENE_CARD_RANK = -1
#: 仅靠「取样书单/取样章节清单」命中的卡：等级未知，排在已标注等级之后。
_UNRATED_RANK = 90


def _normalize_book_name(value: Any) -> str:
    """书名归一化：去掉《》与空白，便于「文风卡.书名」与「章节索引.小说名称」对齐。"""
    text = _scalar(value) or ""
    return str(text).strip().strip("《》").strip()


def _scene_strength_rank(record: "AssetRecord", scene: str) -> int:
    """这张卡对给定场景的优先级（越小越优先）。"""
    if record.scene and scene in record.scene:
        return _SCENE_CARD_RANK
    strengths = _scalar((record.metadata or {}).get("场景强项")) or ""
    for part in str(strengths).split():
        if ":" not in part:
            continue
        name, _, level = part.partition(":")
        if name.strip() == scene:
            return _STRENGTH_RANK.get(level.strip().upper(), _UNRATED_RANK)
    return _UNRATED_RANK


#: JP-05：Windows 盘符绝对路径（跨平台显式拦截，POSIX 下反斜杠只是普通字符）。
_SOURCE_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def resolve_index_source(root: Path | str, value: str | Path) -> Path:
    """把 frontmatter「源文件:」解析为索引库内的绝对路径（JP-05 沙箱）。

    只接受库内安全相对路径；空值、绝对路径（POSIX/盘符/UNC/原生）、
    ``..`` 穿越段、NUL 字符、解析后落到库外的目标（含库内符号链接指到库外）
    一律抛 ``ValueError``，调用方不得读取。
    """
    root_path = Path(root).resolve()
    text = str(value or "").strip()
    if not text:
        raise ValueError("源文件路径为空")
    if "\x00" in text:
        raise ValueError(f"源文件路径含非法字符: {value!r}")
    if (
        text.startswith("/")
        or text.startswith("\\\\")
        or _SOURCE_DRIVE_RE.match(text)
        or Path(text).is_absolute()
    ):
        raise ValueError(f"源文件不得为绝对路径: {value!r}")
    if any(seg == ".." for seg in re.split(r"[\\/]+", text)):
        raise ValueError(f"源文件不得包含越界穿越: {value!r}")
    candidate = (root_path / text).resolve()
    if not candidate.is_relative_to(root_path):
        raise ValueError(f"源文件越出索引库: {value!r}")
    return candidate


class IndexLibrary:
    """用户索引库只读适配器。构造后调用 `scan()` 建立资产清单。"""

    def __init__(self, root: Path | str | None, *, base: Path | None = None) -> None:
        self.root = Path(root) if isinstance(root, str) else root
        self._base = base or Path(__file__).resolve().parents[2]
        self._records: dict[str, AssetRecord] = {}
        self._chapter_cache: dict[str, list[ChapterSection]] = {}
        self._scanned_at = now_iso()

    # ---------------------------------------------------------------- 解析

    def scan(self, *, compute_sha256: bool = True) -> list[AssetRecord]:
        """扫描目录，建立资产记录。

        compute_sha256=False 时只做轻量枚举与 frontmatter 解析，SHA-256 延后
        通过 `hash_asset()` 补齐（大体积原文目录场景）。
        """
        self._records.clear()
        if self.root is None or not self.root.is_dir():
            return []
        for kind, dir_name in KIND_DIRS.items():
            directory = self.root / dir_name
            if not directory.is_dir():
                continue
            for path in _walk_md(directory):
                record = self._build_record(kind, path, compute_sha256=compute_sha256)
                self._records[record.rel_path] = record
        return list(self._records.values())

    def _build_record(
        self, kind: str, path: Path, *, compute_sha256: bool
    ) -> AssetRecord:
        rel_path = normalize_relpath(self.root, path)
        size = 0
        modified_at = ""
        try:
            stat = path.stat()
            size = stat.st_size
            modified_at = mtime_iso(path)
        except OSError:
            pass
        sha256: str | None = None
        if compute_sha256:
            sha256 = _sha256_of(path)
        metadata: dict[str, Any] = {}
        tags: list[str] = []
        tag_pairs: list[tuple[str, tuple[str, ...]]] = []
        scene: str | None = None
        card_type: str | None = None
        book: str | None = None
        author: str | None = None
        verified: bool | None = None

        head = read_frontmatter_block(path)
        if not head:
            head = read_first_lines(path, limit=120)
        meta = parse_frontmatter(head)
        if meta:
            metadata = dict(meta)
            elements = meta.get("包含元素")
            if isinstance(elements, list):
                pairs = split_pipe_tags(elements)
                tag_pairs = [(c, tuple(s)) for c, s in pairs]
                tags = flatten_tags(tag_pairs)
            book = _scalar(meta.get("书名")) or _scalar(meta.get("小说名称"))
            author = _scalar(meta.get("作者"))
            scene = _scalar(meta.get("场景"))
            card_type = _scalar(meta.get("卡类型")) or _scalar(meta.get("类型"))
            status = _scalar(meta.get("验证状态"))
            if status:
                verified = "已" in status or "通过" in status or "验证" in status

        return AssetRecord(
            kind=kind,
            rel_path=rel_path,
            absolute_path=path,
            sha256=sha256,
            size=size,
            modified_at=modified_at,
            scanned_at=self._scanned_at,
            tags=tuple(tags),
            tag_pairs=tuple(tag_pairs),
            scene=scene,
            card_type=card_type,
            book=book,
            author=author,
            verified=verified,
            metadata=metadata,
        )

    def hash_asset(self, rel_path: str) -> str:
        """计算（或取缓存）单个资产文件的完整 SHA-256。"""
        record = self._records.get(rel_path)
        if record is None:
            raise KeyError(f"未扫描的资产: {rel_path}")
        if record.sha256 is not None:
            return record.sha256
        digest = _sha256_of(record.absolute_path)
        self._records[rel_path] = AssetRecord(
            kind=record.kind,
            rel_path=record.rel_path,
            absolute_path=record.absolute_path,
            sha256=digest,
            size=record.size,
            modified_at=record.modified_at,
            scanned_at=record.scanned_at,
            tags=record.tags,
            tag_pairs=record.tag_pairs,
            scene=record.scene,
            card_type=record.card_type,
            book=record.book,
            author=record.author,
            verified=record.verified,
            metadata=record.metadata,
        )
        return digest

    # ---------------------------------------------------------------- 查询

    @property
    def records(self) -> list[AssetRecord]:
        return list(self._records.values())

    def assets(self, kind: str | None = None) -> list[AssetRecord]:
        if kind is None:
            return self.records
        return [r for r in self._records.values() if r.kind == kind]

    def find_asset(self, rel_path: str) -> AssetRecord:
        try:
            return self._records[rel_path]
        except KeyError as exc:
            raise KeyError(f"索引库中不存在该资产: {rel_path}") from exc

    def search_tags(
        self,
        query_tags: str | Iterable[str],
        *,
        kinds: Iterable[str] | None = None,
        limit: int = 20,
    ) -> list[AssetRecord]:
        """按标签匹配资产（canonical 或同义词命中任一即算）。

        单个字符串按一个查询词处理，避免逐字拆开。
        """
        if isinstance(query_tags, str):
            query_tags = [query_tags]
        wanted = {tag.strip() for tag in query_tags if tag.strip()}
        allowed = set(kinds) if kinds is not None else set(KIND_DIRS)
        scored: list[tuple[int, AssetRecord]] = []
        for record in self._records.values():
            if record.kind not in allowed:
                continue
            hit = wanted & set(record.tags)
            if hit:
                scored.append((len(hit), record))
        scored.sort(key=lambda item: (-item[0], item[1].rel_path))
        return [record for _, record in scored[:limit]]

    def search_scenes(
        self,
        scene_query: str | Iterable[str],
        *,
        limit: int = 12,
    ) -> list[IndexHit]:
        """按场景/标签检索章节索引中的 爽点/看点、金句/名场面、伏笔/线索。

        返回受限片段 + 位置，不返回长句全文。命中来源：
        1) 章节索引 frontmatter 标签（含同义词）；
        2) 章节正文中与场景同名的子章节段落。
        """
        queries = {scene_query.strip()} if isinstance(scene_query, str) else {
            q.strip() for q in scene_query if q.strip()
        }
        if not queries:
            return []
        hits: list[IndexHit] = []
        for record in self._records.values():
            if record.kind != "index_chapter":
                continue
            matched = queries & set(record.tags)
            if not matched:
                # 仅 frontmatter 无命中时，跳过正文解析（正文只服务命中资产）
                continue
            try:
                sections = self.get_chapter_index(record.rel_path)
            except KeyError:
                continue
            for section in sections:
                for subsection_name, items in section.subsections.items():
                    for item in items:
                        item_text = " ".join(item)
                        # 场景通常写在三级小节标题（如“装逼打脸”），正文条目本身未必重复该词。
                        hit_tags = _match_scene_in_text(
                            f"{subsection_name}\n{item_text}", queries
                        )
                        if hit_tags:
                            hits.append(
                                IndexHit(
                                    asset=record,
                                    matched_tags=sorted(matched | hit_tags),
                                    section=f"{subsection_name} · {section.chapter_title}",
                                    location=_location_str(section),
                                    snippet=bounded(" / ".join(item), 80),
                                )
                            )
        hits.sort(key=lambda hit: -len(set(hit.matched_tags)))
        return hits[:limit]

    def find_style_cards(
        self,
        scene: str,
        *,
        verified_only: bool = False,
        limit: int = 10,
    ) -> list[AssetRecord]:
        """按场景找文风卡（场景卡 scene 字段或风格卡 场景强项/取样清单）。

        结果按场景强项等级排序（场景专卡与 A 级在前，B 级其后，未知等级最后）；
        同等级内保持文件顺序（``sort`` 稳定），因此结果确定可复现。
        等级只用于排序、不用于排除——只有 B 级卡的场景仍会返回该卡。
        """
        scene = scene.strip()
        found: list[AssetRecord] = []
        for record in self._records.values():
            if record.kind != "style_card":
                continue
            if verified_only and record.verified is not True:
                continue
            meta = record.metadata
            strengths = _scalar(meta.get("场景强项")) or ""
            samples = meta.get("取样书单") or meta.get("取样章节清单") or []
            if record.scene and scene in record.scene:
                found.append(record)
            elif scene in strengths:
                found.append(record)
            elif isinstance(samples, list) and any(
                isinstance(s, str) and scene in s for s in samples
            ):
                found.append(record)
        # 内置卡可能只有 frontmatter 标签，没有“场景强项”字段；标签应作为兼容回退。
        if not found:
            found = [
                record
                for record in self._records.values()
                if record.kind == "style_card"
                and (not verified_only or record.verified is True)
                and scene in record.tags
            ]
        found.sort(key=lambda record: _scene_strength_rank(record, scene))
        return found[:limit]

    def find_exemplar_chapter_indexes(
        self,
        scene_queries: list[str],
        *,
        limit: int = 2,
    ) -> list[AssetRecord]:
        """为「文风范例」挑章节索引：按**书**优选，而不是按场景标签。

        为什么不能按场景标签（2026-09-17 实测）：``search_tags`` 的排序是
        ``(-命中标签数, rel_path)``——标签命中数并列时按**文件名字典序**决胜。
        库内 3536 条章节索引里 **1774 条**带「日常过渡」标签，命中数几乎人人
        相同，于是字典序最小的「仙逆-耳根-…」恒定胜出（审计实证：某学园日常
        章的 3 个文风范例全是仙逆第1005~1014章修真大战）。

        更致命的是：《龙族》全部 9 条章节索引**一条都不带**「日常过渡」标签
        （标的是 CP/三角关系/上古传承），所以无论怎么改并列决胜，按标签检索
        **永远不可能**返回龙族——而龙族正是这本书的体裁近邻。

        文风范例要的是「**这本书**怎么写」，故顺序为：
        1. 场景 → ``find_style_cards`` 最高档文风卡 → 目标书（按卡等级排序）；
        2. 按序取目标书的章节索引；
        3. 目标书无索引、或场景没有对应文风卡时，退回按场景标签检索。
        """
        picked: list[AssetRecord] = []
        seen: set[str] = set()
        for book in self._preferred_exemplar_books(scene_queries):
            for record in self._chapter_indexes_of_book(book):
                if record.rel_path in seen:
                    continue
                seen.add(record.rel_path)
                picked.append(record)
                if len(picked) >= limit:
                    return picked
        if len(picked) >= limit:
            return picked[:limit]
        for record in self.search_tags(scene_queries, kinds=["index_chapter"], limit=limit):
            if record.rel_path in seen:
                continue
            seen.add(record.rel_path)
            picked.append(record)
            if len(picked) >= limit:
                break
        return picked

    def _preferred_exemplar_books(self, scene_queries: list[str]) -> list[str]:
        """按文风卡等级排序的目标书列表（去重保序，等级高者在前）。"""
        ranked: list[tuple[int, str]] = []
        for scene in scene_queries:
            try:
                records = self.find_style_cards(scene, verified_only=True, limit=4)
            except Exception:  # noqa: BLE001 - 选卡失败只影响优选，退回标签检索
                continue
            for record in records:
                name = _normalize_book_name((record.metadata or {}).get("书名"))
                if not name or any(existing == name for _rank, existing in ranked):
                    continue
                ranked.append((_scene_strength_rank(record, scene), name))
        ranked.sort(key=lambda item: item[0])
        return [name for _rank, name in ranked]

    def _chapter_indexes_of_book(self, book: str) -> list[AssetRecord]:
        """某本书的全部章节索引，按 rel_path 确定性排序。"""
        matched = [
            record
            for record in self._records.values()
            if record.kind == "index_chapter"
            and _normalize_book_name((record.metadata or {}).get("小说名称")) == book
        ]
        matched.sort(key=lambda record: record.rel_path)
        return matched

    def get_chapter_index(self, rel_path: str) -> list[ChapterSection]:
        """解析章节索引正文为章节结构（缓存）。"""
        if rel_path in self._chapter_cache:
            return self._chapter_cache[rel_path]
        record = self.find_asset(rel_path)
        text = _read_text(record.absolute_path)
        sections = _parse_chapter_body(text)
        self._chapter_cache[rel_path] = sections
        return sections

    def get_style_card(self, rel_path: str) -> dict[str, Any]:
        """解析文风卡正文为可执行技法 + 观察（受限片段）。

        兼容两种真实卡片格式：
        - 风格卡：`## 一、9 轴` → `### 1. 句子节奏` → `- **观察**` /
          `- **规则**：` 后接缩进子项 / `- **锚点**`；
        - 场景卡：`## 〇、跨书共识` 下编号条目 + `### 《书》作者` 下
          `**观察**` / `**可执行规则**` / `**锚点原句**`。

        修复点（v4.3.0 审查）：旧解析器只认「**规则**」所在行本身，
        其后的缩进子项全部丢失，导致真实索引库注入 prompt 的规则为空。
        """
        record = self.find_asset(rel_path)
        text = _read_text(record.absolute_path)
        head = read_frontmatter_block(record.absolute_path)
        meta = parse_frontmatter(head)
        axes: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        mode: str | None = None  # rules | observations | anchors | None

        def append_current() -> None:
            if current is not None and (current.get("rules") or current.get("observations")):
                axes.append(current)

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            numbered_axis = re.match(r"^###\s+\d+[.、]\s*(.+)$", stripped)
            consensus = re.match(r"^##\s+[〇一二三四五六七八九十]+\s*、\s*(.+)$", stripped)
            book_sample = re.match(r"^###\s+《(.+?)》\s*(.+)?$", stripped)

            if numbered_axis or book_sample or (
                consensus and "分书样本" not in consensus.group(1) and "样本" not in consensus.group(1)
            ):
                if current is not None:
                    append_current()
                if numbered_axis:
                    current = {"axis": numbered_axis.group(1).strip(), "rules": [], "observations": [], "anchors": []}
                elif consensus:
                    current = {"axis": f"跨书共识·{consensus.group(1).strip()}", "rules": [], "observations": [], "anchors": []}
                else:
                    book = book_sample.group(1).strip()
                    current = {"axis": f"《{book}》分书样本", "rules": [], "observations": [], "anchors": []}
                mode = None
                continue

            if current is None:
                continue

            if "锚点原句" in stripped or "**锚点**" in stripped:
                mode = "anchors"
                tail = _rule_from_line(stripped)
                if tail and tail != stripped:
                    current["anchors"].append(tail)
                continue
            if "可执行规则" in stripped or "**规则**" in stripped:
                mode = "rules"
                tail = _rule_from_line(stripped)
                if tail and tail != stripped:
                    current["rules"].append(tail)
                continue
            if "**观察**" in stripped:
                mode = "observations"
                tail = _rule_from_line(stripped)
                if tail and tail != stripped:
                    current["observations"].append(tail)
                continue

            # 跨书共识的编号条目：直接当作规则（含完整话术）
            if current.get("axis", "").startswith("跨书共识") and re.match(r"^\d+[.、]\s+", stripped):
                current.setdefault("rules", []).append(bounded(stripped, 160))
                continue

            bullet = stripped
            if bullet.startswith(("- ", "* ")):
                bullet = bullet[2:].strip()
            elif re.match(r"^\d+[.、]\s+", bullet):
                bullet = re.sub(r"^\d+[.、]\s+", "", bullet).strip()
            else:
                continue

            if mode == "rules":
                current.setdefault("rules", []).append(bounded(bullet, 140))
            elif mode == "observations":
                current.setdefault("observations", []).append(bounded(bullet, 140))
            elif mode == "anchors":
                current.setdefault("anchors", []).append(bounded(bullet, 80))

        if current is not None:
            append_current()

        return {
            "rel_path": rel_path,
            "meta": meta,
            "scene": record.scene,
            "card_type": record.card_type,
            "book": record.book,
            "verified": record.verified,
            "fingerprint": _scalar(meta.get("一句话指纹")),
            "strengths": _scalar(meta.get("场景强项")) or "",
            "axes": axes,
        }

    def original_structure_digest(
        self,
        source_rel_path: str,
        line_range: tuple[int, int] | None = None,
    ) -> str:
        """从 小说原文/ 提炼结构/节奏摘要，不返回原文句面。

        只输出：段落数、平均句长、短句占比、对话段占比、首尾段字数。
        这样既利用原文的节奏特征，又避免把台版原文直接喂给 LLM 导致照抄。
        """
        try:
            src = resolve_index_source(self.root, source_rel_path)
        except ValueError:
            return ""
        if not src.is_file():
            return ""
        try:
            text = _read_text(src)
        except Exception:  # noqa: BLE001
            return ""
        lines = text.splitlines()
        if line_range is not None and line_range[0] >= 1 and line_range[1] <= len(lines):
            text = "\n".join(lines[line_range[0] - 1:line_range[1]])
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if not paragraphs:
            return ""
        sentences = [
            s.strip() for s in re.split(r"[。！？!?]", "".join(paragraphs)) if len(s.strip()) >= 2
        ]
        avg_len = sum(len(s) for s in sentences) / len(sentences) if sentences else 0
        short_ratio = sum(1 for s in sentences if len(s) <= 10) / len(sentences) if sentences else 0
        dialogue_ratio = (
            sum(1 for p in paragraphs if any(m in p for m in ("「", "”", "“", '"'))) / len(paragraphs)
            if paragraphs
            else 0
        )
        return (
            f"段落{len(paragraphs)}；平均句长{avg_len:.1f}；短句占比{short_ratio:.0%}；"
            f"对话段占比{dialogue_ratio:.0%}；首段{len(paragraphs[0])}字；尾段{len(paragraphs[-1])}字"
        )

    def original_prose_fragment(
        self,
        source_rel_path: str,
        line_range: tuple[int, int] | None = None,
        *,
        max_chars: int = 700,
        min_para_chars: int = 16,
    ) -> str:
        """从 小说原文/ 取一段**真实正文**作为文风范例。

        与 ``original_structure_digest`` 的分工：摘要只给节奏统计（0 句面），
        范例给真句子——因为"笔触"只能从真实句子里学，从要点清单里学不到。
        注入侧必须配防抄守卫（``v4.core.exemplar_guard``），并在 prompt 中
        明示「只学质感、禁止照抄、禁止引入范例中的事实」。

        取法：跳 frontmatter 与标题/插图等噪声行，从首个"有分量"的段落起，
        按原文顺序连续取到 ``max_chars`` 为止——保持原文语序，不做摘句拼接。
        """
        try:
            src = resolve_index_source(self.root, source_rel_path)
        except ValueError:
            return ""
        if not src.is_file():
            return ""
        try:
            text = _read_text(src)
        except Exception:  # noqa: BLE001
            return ""
        text = _strip_frontmatter(text)
        lines = text.splitlines()
        if line_range is not None and line_range[0] >= 1 and line_range[1] <= len(lines):
            lines = lines[line_range[0] - 1 : line_range[1]]

        picked: list[str] = []
        total = 0
        started = False
        for raw in lines:
            para = raw.strip().strip("\u3000").strip()
            if not para or _is_prose_noise(para):
                continue
            if not started:
                if len(para) < min_para_chars:
                    continue
                started = True
            if picked and total + len(para) > max_chars:
                break
            picked.append(para)
            total += len(para)
        return "\n\n".join(picked)

    def get_novel_index(self, rel_path: str) -> dict[str, Any]:
        """解析小说索引 frontmatter + 章节/标签摘要。"""
        record = self.find_asset(rel_path)
        head = read_frontmatter_block(record.absolute_path)
        meta = parse_frontmatter(head)
        return {
            "rel_path": rel_path,
            "meta": meta,
            "book": record.book,
            "author": record.author,
            "tags": list(record.tags),
            "chapter_range": _scalar(meta.get("章节范围")),
            "total_chapters": _scalar(meta.get("总章节数")),
        }


# ---------------------------------------------------------------- 内部工具


def _walk_md(directory: Path) -> Iterable[Path]:
    """递归枚举目录下 .md 文件（跳过隐藏文件与状态库）。"""
    if not directory.is_dir():
        return
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError:
        return
    for entry in entries:
        if entry.is_dir():
            yield from _walk_md(entry)
        elif entry.is_file() and entry.suffix == ".md" and not entry.name.startswith("."):
            yield entry


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _sha256_of(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        pass
    return digest.hexdigest()


def _scalar(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list) and value and isinstance(value[0], str):
        return value[0].strip()
    return None


def _parse_chapter_body(text: str) -> list[ChapterSection]:
    sections: list[ChapterSection] = []
    current: ChapterSection | None = None
    current_sub: str | None = None
    current_items: list[tuple[str, ...]] = []
    for line in text.splitlines():
        heading = _CHAPTER_HEADING.match(line.strip())
        if heading:
            if current is not None:
                if current_sub and current_items:
                    current.subsections[current_sub] = tuple(current_items)
                sections.append(current)
            title = heading.group(1).strip()
            line_range = None
            if heading.group(2) and heading.group(3):
                line_range = (int(heading.group(2)), int(heading.group(3)))
            current = ChapterSection(chapter_title=title, line_range=line_range)
            current_sub = None
            current_items = []
            continue
        sub = _SECTION_HEADING.match(line.strip())
        if sub and current is not None:
            if current_sub and current_items:
                current.subsections[current_sub] = tuple(current_items)
            current_sub = sub.group(1).strip()
            current_items = []
            continue
        bullet = _BULLET.match(line.strip())
        if bullet and current_sub and current is not None:
            current_items.append(_split_bullet(bullet.group(1)))
    if current is not None:
        if current_sub and current_items:
            current.subsections[current_sub] = tuple(current_items)
        sections.append(current)
    return sections


def _split_bullet(text: str) -> tuple[str, ...]:
    """把 `- **智斗**：xxx` 拆成 (标题, 内容) 等片段，避免整句透传。"""
    cleaned = text.strip()
    if cleaned.startswith("**") and "**" in cleaned[2:]:
        head, _, tail = cleaned[2:].partition("**")
        return (head.strip(), tail.strip().lstrip(":： "))
    return (cleaned,)


def _match_scene_in_text(text: str, queries: set[str]) -> set[str]:
    return {q for q in queries if q in text}


def _location_str(section: ChapterSection) -> str:
    if section.line_range:
        return f"{section.chapter_title}（行号 {section.line_range[0]}~{section.line_range[1]}）"
    return section.chapter_title


def _rule_from_line(line: str) -> str:
    """从 `**规则**：...` / `- ...` 行抽取受限规则片段。"""
    for prefix in ("**规则**", "**锚点**", "**观察**"):
        if prefix in line:
            line = line.split(prefix, 1)[1].lstrip(":： ")
            break
    line = line.lstrip("- ").strip()
    return bounded(line, 90)
