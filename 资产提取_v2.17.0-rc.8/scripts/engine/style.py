#!/usr/bin/env python3
"""Style library parsing, style packs (ADR-0076) and style locking."""
import re
from pathlib import Path
from typing import Sequence

SKILL_DIR = Path(__file__).resolve().parent.parent.parent
STYLE_LIB = SKILL_DIR / "references" / "画风库.md"
STYLE_MAP = SKILL_DIR / "references" / "题材-画风映射.md"
STYLE_PACK_DIR = SKILL_DIR / "references" / "stylepacks"
# 视频规则目录（规格 2.10）：静态分辨器**不读取**此处；只有视频执行器会加载。
# 常量在此仅用于"静态不得引用"的守卫与测试断言，本模块不提供任何读取入口。
VIDEO_STYLE_DIR = SKILL_DIR / "references" / "video_styles"

DEFAULT_STYLE_ID = "A1"

# 分层行可含的层名（3D 与 2D 介质通用，ADR-0076）
RENDER_LAYER_KEYS = ("建模层", "渲染层", "场景/背景", "镜头")
DEFINE_LAYER_KEYS = ("介质层", "材质层", "线条层", "上色层")
LOCK_LAYER_KEYS = RENDER_LAYER_KEYS + DEFINE_LAYER_KEYS
# 各资产类型在渲染锁定检查中跳过的层名关键词
SKIP_LAYERS_BY_TYPE = {
    "道具": ("场景", "背景", "镜头", "构图", "角色", "视图"),
    "场景": ("角色",),
}

_pack_cache: dict[str, dict | None] = {}
_warned_free = False

MATERIAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "皮肤": ("皮肤", "肤色", "肤质"),
    "头发": ("发丝", "毛发", "鬓", "刘海", "发髻", "发型", "发束"),
    "布料": ("丝绸", "布", "织物", "绸", "缎", "纱", "锦", "麻", "袍", "裙", "披风", "斗篷", "旌旗", "衣"),
    "皮革": ("皮革", "皮甲", "皮靴", "皮质", "皮衣", "皮具"),
    "金属": ("金属", "铠甲", "甲胄", "甲片", "铁", "铜", "白银", "鎏金", "钢", "宝剑", "长剑", "刀", "矛", "盾", "首饰", "镯", "锁链", "头盔"),
    "玻璃": ("玻璃", "琉璃", "水晶", "宝石", "玉石", "钻石", "珍珠"),
    "塑料": ("塑料", "树脂"),
    "木材": ("木制", "木质", "木箱", "木桌", "竹简", "木", "藤"),
    "石材": ("石材", "石雕", "岩石", "砖", "石碑", "石"),
    "陶瓷": ("陶瓷", "瓷器", "青瓷", "陶器", "釉"),
    "纸": ("纸张", "卷轴", "书页", "符纸", "纸"),
    "液体": ("水面", "雨水", "雾", "河流", "河水", "河面", "河", "江",
           "湖水", "海水", "溪", "血", "酒", "茶", "泉", "瀑布"),
    "自发光": ("发光", "光效", "霓虹", "荧光", "法阵", "灵光", "全息", "灯火", "烛光"),
    "橡胶": ("橡胶", "轮胎"),
    "妆面": ("妆面", "妆容", "唇色", "胭脂", "眼影", "腮红"),
    "植被": ("树木", "竹林", "树叶", "花", "草地", "竹", "植被", "苔", "蕨", "森林"),
}
DEFAULT_MATERIALS = {
    "人物": ("皮肤", "头发", "布料"),
    "场景": ("石材", "木材", "植被"),
    "道具": ("金属", "木材"),
}
MAX_ROUTED_MATERIALS = 8

NEG_CORE_WORDS = (
    "文字", "水印", "标识", "字幕", "变形", "肢体", "畸形", "分身", "镜像", "噪点", "颗粒",
    "压缩", "低模", "模糊", "过曝", "死白", "写实", "照片", "渲染", "赛璐璐", "平涂", "卡通",
    "二维", "三维", "厚涂", "美颜", "蜡质", "塑料", "现代", "科技", "元素",
    "色彩", "配色", "色调", "构图", "节奏", "氛围", "光线", "都市", "运动", "明暗",
)
NEG_TYPE_WORDS = {
    "人物": ("脸", "面容", "发", "妆", "表情", "姿态", "人偶", "解剖", "身体", "肤色", "换脸",
           "幼态", "网红", "眼", "服装", "古装", "服饰", "鞋", "裙", "铠甲", "背景", "云雾", "贴图"),
    "场景": ("场景", "背景", "环境", "景深", "植被", "水面", "地面", "建筑", "天空", "道路", "光斑"),
    "道具": ("材质", "纹理", "物件", "反光", "镜面", "划痕", "磨损", "镀", "道具"),
}


def _read(path: str | Path) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def _strip_title(text: str) -> str:
    """Drop markdown headings and blank lines from a stylepack fragment."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(l for l in lines if not l.startswith("#"))


def re_split(s: str | None) -> list[str]:
    """Split compound genres while preserving slashes inside parentheses."""
    parts, buf, depth = [], [], 0
    for ch in s or "":
        if ch in "（(":
            depth += 1
        elif ch in "）)" and depth:
            depth -= 1
        if depth == 0 and ch in "/、,，":
            item = "".join(buf).strip()
            if item:
                parts.append(item)
            buf = []
        else:
            buf.append(ch)
    item = "".join(buf).strip()
    if item:
        parts.append(item)
    return parts


def normalize_style_name(s: str | None) -> str:
    """Normalize a style name/number for robust card matching (3D↔三维, spaces, case)."""
    s = (s or "").strip().lower().replace(" ", "").replace("　", "")
    return s.replace("3d", "三维")


def _field_text(block: str, marker: str) -> str:
    """Extract a field value from a style card block."""
    lines = block.splitlines()
    out, collecting = [], False
    for line in lines:
        s = line.strip()
        if s.startswith(marker):
            collecting = True
            rest = re.sub(r"^[^：:]*[：:]\s*", "", s[len(marker):]).strip()
            if rest:
                out.append(rest)
            continue
        if collecting:
            if s.startswith("- **") or s.startswith("### ") or s.startswith(">") or s == "---":
                break
            if s:
                out.append(s)
    return "\n".join(out)


def load_style_map() -> dict[str, str]:
    """Parse 题材-画风映射.md → {genre: style_id}."""
    out = {}
    for line in _read(STYLE_MAP).splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| 题材"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 3 and cells[0] and cells[2]:
            sid = cells[2].strip().split("/", 1)[0].strip()
            for g in re_split(cells[0]):
                out[g] = sid
    return out


def parse_style_card(sid: str) -> dict[str, str | list[str]] | None:
    """Parse a single style card from the style library."""
    text = _read(STYLE_LIB)
    key = f"### {sid}."
    pos = text.find(key)
    if pos == -1:
        if sid != DEFAULT_STYLE_ID:
            return parse_style_card(DEFAULT_STYLE_ID)
        return None
    end = text.find("\n### ", pos + len(key))
    block = text[pos:end if end != -1 else len(text)]
    card: dict[str, str | list[str]] = {"id": sid, "name": "", "zh": "", "en": "", "negative": "", "layers": [], "pack": "", "aliases": []}
    title = block.splitlines()[0] if block.splitlines() else ""
    if "." in title:
        card["name"] = title.split(".", 1)[1].strip()
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("- **中文定性") or line.startswith("- **中文") or line.startswith("- **定性"):
            prefix = line.split("：", 1)[0].split(":", 1)[0].strip()
            card["zh"] = _field_text(block, prefix)
        elif line.startswith("- **英文标签块") or line.startswith("- **英文"):
            prefix = line.split("：", 1)[0].split(":", 1)[0].strip()
            card["en"] = _field_text(block, prefix)
        elif line.startswith("- **负面"):
            card["negative"] = _field_text(block, "- **负面")
        elif line.startswith("- **风格包"):
            rest = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            m = re.match(r"[A-H]\d+", rest)
            card["pack"] = m.group(0) if m else rest
        elif line.startswith("- **别名"):
            rest = line.split("：", 1)[-1].split(":", 1)[-1]
            card["aliases"] = [a.strip() for a in re.split(r"[、,，]", rest) if a.strip()]
    if not card["zh"]:
        body_lines = []
        for l in block.splitlines()[1:]:
            s = l.strip()
            if not s or s.startswith(">") or s == "---" or s.startswith("## "):
                continue
            if s.startswith("- **"):
                rest = s.split("：", 1)[-1].split(":", 1)[-1].strip()
                if rest and not rest.startswith("**"):
                    body_lines.append(rest)
            else:
                body_lines.append(s)
        card["zh"] = "\n".join(body_lines)
    layers = []
    for line in str(card["zh"]).splitlines():
        s = line.strip()
        if s and s[0].isdigit() and ". " in s[:4]:
            layers.append(s)
    card["layers"] = layers
    return card


def find_card_by_name(name: str) -> dict[str, str | list[str]] | None:
    """Find a style card by exact or prefix name match (normalized, ADR-0076)."""
    name = (name or "").strip()
    if not name:
        return None
    text = _read(STYLE_LIB)
    nname = normalize_style_name(name)
    for line in text.splitlines():
        if not line.startswith("### "):
            continue
        sid = line.split(".", 1)[0].replace("### ", "").strip()
        title = line.split(".", 1)[1].strip() if "." in line else ""
        if name == sid or nname == normalize_style_name(sid):
            # 支持直接传卡片编号（如 H7/A2），避免用户把编号当自由描述而丢锁
            return parse_style_card(sid)
        ntitle = normalize_style_name(title)
        if nname == ntitle:
            return parse_style_card(sid)
        if ntitle.startswith(nname) and len(ntitle) > len(nname):
            nxt = title[len(name)] if len(name) < len(title) else ""
            # ASCII boundary keeps "自由描述 测试" from swallowing cards;
            # Chinese shorthand like 仙侠奇幻风 must still match 仙侠奇幻三维渲染.
            if not nxt or not nxt.isascii() or not nxt.isalnum():
                return parse_style_card(sid)
    # 标题未命中 → 按卡片「别名」行匹配（旧名/俗称，ADR-0076 直白命名兼容层）
    for sid in re.findall(r"^### ([A-H]\d+)\.", text, re.M):
        card = parse_style_card(sid)
        for alias in card.get("aliases") or []:
            na = normalize_style_name(alias)
            if not na:
                continue
            if nname == na or na.startswith(nname) or nname.startswith(na):
                longer = na if len(na) >= len(nname) else nname
                shorter = nname if len(na) >= len(nname) else na
                nxt = longer[len(shorter)] if len(longer) > len(shorter) else ""
                if not nxt or not nxt.isascii() or not nxt.isalnum():
                    return card
    return None


def style_equivalent(a: str | None, b: str | None) -> bool:
    """两个画风标识是否同一画风（编号/卡名/别名等价，E1 签发门防绕过）。

    命中同一张画风卡（卡片编号相同）即等价；两边都未命中卡片时按归一化文本比较；
    一边命中卡片、一边是自由描述一律不相等（自由描述不得冒充卡片画风）。
    """
    na, nb = normalize_style_name(a), normalize_style_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    card_a, card_b = find_card_by_name(a or ""), find_card_by_name(b or "")
    if card_a and card_b:
        return str(card_a.get("id", "")) == str(card_b.get("id", ""))
    return False


def resolve_card_for_name(name: str) -> dict[str, str | list[str]] | None:
    """中文正式名 / 别名 / 编号 → 画风卡（规格 5：登记表优先，兼容层兜底）。

    登记表命中时把卡片显示名替换为中文正式名（用户界面不再暴露 A1/H7 等编号）；
    未命中登记表则退回 find_card_by_name 的既有模糊匹配（前缀/简写/自由描述边界）。
    别名冲突由登记表抛 StyleAliasConflict——禁止「第一个命中」。
    """
    text = (name or "").strip()
    if not text:
        return None
    try:
        from engine import style_registry as registry_lib
    except ImportError:
        try:
            from . import style_registry as registry_lib
        except ImportError:
            registry_lib = None
    if registry_lib is not None:
        record = registry_lib.resolve_style_record(text)
        if record is not None:
            card = parse_style_card(record.id)
            if card is not None:
                card = dict(card)
                card["name"] = record.name
                return card
    return find_card_by_name(text)


def resolve_style(genre: str | None, style_arg: str | None = None) -> tuple[dict[str, str | list[str]] | None, str]:
    """Return (card, mode). mode is 'card' or 'free'."""
    global _warned_free
    if style_arg:
        card = resolve_card_for_name(style_arg.strip())
        if card:
            return card, "card"
        if not _warned_free:
            print(
                f"⚠️ --style「{style_arg.strip()}」未命中画风库卡片，已按自由描述处理"
                f"（无分层/无风格包/无负面并集）。建议核对画风库卡片名，或直接传卡片编号（如 H7）。"
            )
            _warned_free = True
        return None, "free"
    sid = load_style_map().get((genre or "").strip()) or DEFAULT_STYLE_ID
    # 映射表可用中文正式名（如 仙侠国风电影CG）或编号；统一经登记表解析（规格 5）
    card = resolve_card_for_name(sid)
    if card is None:
        print(f"⚠️ 画风库缺失卡片 {sid}（含默认 A1），已降级自由描述模式——请检查 references/画风库.md")
        return None, "free"
    return card, "card"


def load_stylepack(sid: str | None) -> dict | None:
    """Load a style pack (ADR-0076): views/materials/invariants/negative, pure Chinese."""
    if not sid:
        return None
    if sid in _pack_cache:
        return _pack_cache[sid]
    base = STYLE_PACK_DIR / sid
    pack: dict | None = None
    if base.is_dir():
        pack = {"views": {}, "materials": {}, "invariants": "", "negative": ""}
        for key, fname in (("脸部", "视图-脸部.md"), ("全身", "视图-全身.md"),
                           ("环境", "视图-环境.md"), ("物件", "视图-物件.md")):
            p = base / fname
            if p.exists():
                pack["views"][key] = _strip_title(p.read_text(encoding="utf-8"))
        mdir = base / "材质"
        if mdir.is_dir():
            for p in sorted(mdir.glob("*.md")):
                pack["materials"][p.stem] = _strip_title(p.read_text(encoding="utf-8"))
        inv = base / "不变量.md"
        if inv.exists():
            pack["invariants"] = inv.read_text(encoding="utf-8").strip()
        neg = base / "负面词.md"
        if neg.exists():
            pack["negative"] = _strip_title(neg.read_text(encoding="utf-8"))
    _pack_cache[sid] = pack
    return pack


def build_pack_invariants_block(pack: dict | None) -> str:
    """Full invariants text appended to the style lock block (verbatim keep)."""
    if not pack or not pack.get("invariants"):
        return ""
    return f"【共享风格不变量（逐字保留，全资产通用）】\n{pack['invariants']}"


def route_materials(pack: dict | None, type_key: str, text: str | None) -> list[str]:
    """Pick stylepack materials actually present in the asset text (material routing)."""
    if not pack or not pack.get("materials"):
        return []
    hay = text or ""
    picked: list[str] = []
    for name in DEFAULT_MATERIALS.get(type_key, ()):
        if name in pack["materials"]:
            picked.append(name)
    for name, kws in MATERIAL_KEYWORDS.items():
        if len(picked) >= MAX_ROUTED_MATERIALS:
            break
        if name in picked or name not in pack["materials"]:
            continue
        if any(k in hay for k in kws):
            picked.append(name)
    return picked


def build_material_block(pack: dict | None, type_key: str, text: str | None) -> str:
    """Routed material qualitative block for the prompt (empty when no pack)."""
    names = route_materials(pack, type_key, text)
    if not names:
        return ""
    rows = [f"- {name}：{pack['materials'][name]}" for name in names]
    return "【材质定性（按画面内容路由，逐条融入材质四件套，不写画面中不存在的材质）】\n" + "\n".join(rows)


def _filter_negative(text: str | None, type_key: str) -> list[str]:
    items = [x.strip() for x in re.split(r"[、，,;；\n]", str(text or "")) if x.strip()]
    out, seen = [], set()
    for it in items:
        core = it[1:] if it.startswith("无") else it
        if core in seen:
            continue
        if any(w in core for w in NEG_CORE_WORDS) or any(w in core for w in NEG_TYPE_WORDS.get(type_key, ())):
            seen.add(core)
            out.append(f"无{core}")
    return out


VIEW_FILES_BY_TYPE = {
    "人物": ("脸部", "全身"),
    "场景": ("环境",),
    "道具": ("物件",),
}


def build_view_block(pack: dict | None, type_key: str) -> str:
    """Routed view qualitative block (ADR-0077): 脸部/全身 for 人物, 环境 for 场景, 物件 for 道具."""
    if not pack or not pack.get("views"):
        return ""
    keys = VIEW_FILES_BY_TYPE.get(type_key, ())
    texts = [pack["views"][k] for k in keys if pack["views"].get(k)]
    if not texts:
        return ""
    label = {"人物": "人物视图定性", "场景": "环境视图定性", "道具": "物件视图定性"}.get(type_key, "视图定性")
    return f"【{label}（逐字保留，融入画面描写）】\n" + "\n".join(texts)


def build_negative_block(card: dict | None, pack: dict | None, type_key: str) -> str:
    """Type-filtered union of card + stylepack negatives (ADR-0035/0063/0076).

    过滤后为空但卡片本身有负面时，整条保留卡片负面（负面词宁多勿失，
    冒烟回归：A3-A6/C2/C4 的氛围类负面曾被过滤器整条丢光）。
    """
    card_items = _filter_negative(card.get("negative") if card else "", type_key)
    parts = card_items
    parts += [p for p in _filter_negative(pack.get("negative") if pack else "", type_key) if p not in parts]
    if not parts and card and str(card.get("negative", "")).strip():
        raw = [x.strip() for x in re.split(r"[、，,;；\n]", str(card["negative"])) if x.strip()]
        parts = [x if x.startswith("无") else f"无{x}" for x in raw]
    return "、".join(parts)


def build_style_block(card: dict[str, str | list[str]] | None, free_desc: str | None = None) -> str:
    """Full style lock block for scenes/derivatives/enrichment (pure Chinese)."""
    if free_desc:
        desc = free_desc.strip()
        return (
            f"【风格锁定】本条提示词必须保持以下画风不变（用户指定描述）：\n"
            f"{desc}\n"
            f"风格锁定句：所有镜头必须保持上述画风不变。"
        )
    zh = str(card["zh"]) if card else ""
    return (
        f"【风格锁定】本条及后续所有变体提示词必须保持以下画风不变：\n"
        f"{zh}\n"
        f"风格锁定句：所有镜头必须保持上述画风不变。\n"
        f"禁止在后续提示词中改变画风、色调、渲染方式或镜头语言基线的描述。"
    )


def content_lock_sentence(card: dict[str, str | list[str]] | None) -> str:
    """Return the qualitative content-language line to inject verbatim (ADR-0069)."""
    if not card:
        return ""
    lines = [l.strip() for l in str(card.get("zh", "")).splitlines() if l.strip()]
    return lines[0] if lines else ""


def render_lock_sentences(card: dict[str, str | list[str]] | None) -> list[str]:
    """Return the lockable layer sentences to inject verbatim (ADR-0069/0076)."""
    if not card:
        return []
    out = []
    for l in card.get("layers") or []:
        if any(k in l for k in LOCK_LAYER_KEYS):
            out.append(l.strip())
    return out


def build_render_lock_text(card: dict[str, str | list[str]] | None) -> str:
    """Deterministic render-lock text assembled by code (ADR-0076).

    代码按卡片分层逐字拼装，输出时作为第一段「渲染锁定：」原样写入，
    不再依赖模型从画风锁定块自行抄写。
    """
    if not card:
        return ""
    layers = render_lock_sentences(card)
    if layers:
        return "\n".join(layers)
    return content_lock_sentence(card)


def build_render_block(card: dict[str, str | list[str]] | None) -> str:
    """Render-only lock block for character/prop costume shots (pure Chinese)."""
    if not card:
        return ""
    layers = card.get("layers") or []
    keep = [l for l in layers if any(k in l for k in ("建模层", "渲染层", "介质层", "材质层", "线条层", "上色层"))]
    if keep:
        zh_part = "\n".join(keep)
    else:
        # 无编号分层的老卡（如 A2）：渲染层块回退注入中文定性第一段，
        # 不能让道具模板只拿到一个空标题而丢画风（ADR-0069 兜底口径）。
        zh_part = content_lock_sentence(card) or str(card.get("zh", "")).strip()
    if not zh_part:
        return ""
    return f"渲染基调（锁定，定妆中性要求不变）：\n{zh_part}".strip()


def confirm_style(
    genre: str,
    card: dict[str, str | list[str]] | None,
    mode: str,
    free_desc: str | None,
    yes: bool = False,
) -> tuple[dict[str, str | list[str]] | None, str, str | None] | None:
    """Interactive style confirmation before generation."""
    if yes:
        label = card["id"] if card else "自由描述"
        extra = f"（风格包 {card['pack']}）" if card and card.get("pack") else ""
        print(f"ℹ️  --yes 跳过确认: 题材「{genre}」→ 画风「{label}」{extra}")
        if mode == "card" and genre and genre not in load_style_map():
            print(f"⚠️  题材「{genre}」无专属映射，当前为 {DEFAULT_STYLE_ID} 兜底画风；建议补充映射表或指定 --style")
        return card, mode, free_desc
    try:
        if mode == "free":
            print(f"🎨 本次画风（--style 指定）: {free_desc}")
            ans = input("确认使用该画风？[回车=确认 / 输入新画风名或自由描述覆盖 / n=取消] > ").strip()
            if ans.lower() in ("n", "no", "q", "quit", "cancel"):
                print("🛑 已取消（未生成任何提示词）")
                return None
            if ans:
                return resolve_style(genre, ans)
            return card, mode, free_desc
        print("\n" + "=" * 60)
        print("🎨 画风确认")
        print(f"   题材: {genre}")
        print(f"   默认画风: {card['id']} {card['name']}")
        if card and card.get("pack"):
            print(f"   风格包: {card['pack']}（视图/材质/不变量/负面词按画面内容路由注入）")
        if genre and genre not in load_style_map():
            print(f"   ⚠️  该题材无专属映射，当前为 {DEFAULT_STYLE_ID} 兜底画风；建议补充映射表或指定 --style")
        zh_first = str(card["zh"]).splitlines()[0] if card and card.get("zh") else ""
        print(f"   定性: {zh_first}")
        print("=" * 60)
        ans = input("确认使用该画风？[回车=确认 / 输入新画风名或自由描述覆盖 / n=取消] > ").strip()
        if ans.lower() in ("n", "no", "q", "quit", "cancel"):
            print("🛑 已取消（未生成任何提示词）")
            return None
        if ans:
            return resolve_style(genre, ans)
        return card, mode, free_desc
    except EOFError:
        print("❌ 无法交互确认画风（stdin 无输入）。自动化场景请显式传 --style 并加 --yes。")
        return None


# ---------------------------------------------------------------------------
# 静态风格上下文（规格 2.10 / ADR-0085）：静态图片规则必须与视频规则彻底分离
# ---------------------------------------------------------------------------

class StaticStyleError(ValueError):
    """静态风格上下文混入视频规则（运镜/时长/节奏），必须显式失败而非静默输出。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}：{detail}" if detail else code)


STATIC_VIDEO_LEAK = "STATIC_VIDEO_LEAK"
# 静态上下文出现任一即视为视频规则泄漏
VIDEO_TOKENS = ("运镜", "镜头语言", "帧率", "英尺每秒", "钩子", "每4s", "每4秒")
VIDEO_TOKEN_RE = re.compile(r"每\s*\d+\s*[sS秒]")


def find_video_leaks(text: str | None) -> list[str]:
    """返回静态文本中出现的视频规则词（空列表表示干净）。"""
    body = text or ""
    leaks = [tok for tok in VIDEO_TOKENS if tok in body]
    leaks += [m.group(0) for m in VIDEO_TOKEN_RE.finditer(body)]
    return sorted(set(leaks))


def build_static_style_context(record, subject_type: str,
                               materials: Sequence[str] | None = None) -> str:
    """构建静态资产的风格上下文（纯中文，绝不含视频规则）。

    组成：风格基线（正式中文名 + 中文定性首行）→ 渲染锁定层 → 主体视图定性 →
    实际材质定性 → 类型过滤后的禁用项。材质只取调用方声明的画面中真实存在的项，
    不写画面里没有的材质。
    """
    if record is None:
        raise StaticStyleError("STYLE_RECORD_REQUIRED", "静态风格上下文必须由风格登记项驱动")
    card = parse_style_card(record.id)
    if card is None:
        raise StaticStyleError("STYLE_CARD_MISSING",
                               f"画风库缺少卡片 {record.id}（{record.name}）")
    parts: list[str] = [f"【风格基线】{record.user_label}"]
    zh_first = str(card.get("zh", "")).splitlines()
    if zh_first:
        parts.append(zh_first[0].strip())
    layers = render_lock_sentences(card)
    if layers:
        parts.append("【渲染锁定】\n" + "\n".join(layers))

    pack = load_stylepack(str(card.get("pack", "") or "")) if card.get("pack") else None
    view = build_view_block(pack, subject_type)
    if view:
        parts.append(view)

    if pack and materials:
        available = pack.get("materials", {})
        wanted: list[str] = []
        for name in materials:
            key = str(name).strip()
            if key and key in available and key not in wanted:
                wanted.append(key)
        if wanted:
            rows = [f"- {name}：{available[name]}" for name in wanted]
            parts.append("【材质定性（仅画面中真实存在的材质）】\n" + "\n".join(rows))

    negative = build_negative_block(card, pack, subject_type)
    if negative:
        parts.append(f"【禁用项】{negative}")

    context = "\n".join(parts)
    leaks = find_video_leaks(context)
    if leaks:
        raise StaticStyleError(
            STATIC_VIDEO_LEAK,
            f"静态风格上下文出现视频规则词 {leaks}（静态图片规则与视频规则必须分离，"
            f"视频约束请放入 {VIDEO_STYLE_DIR.name}/）")
    return context
