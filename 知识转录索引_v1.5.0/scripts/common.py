# -*- coding: utf-8 -*-
"""转录索引 · 公共模块

配置加载 / DeepSeek 调用封装 / 台账(manifest) / 源目录扫描。
ADR-0005：产物(索引库/校对版/向量库)存放于源目录，本模块统一从这里解析路径。
ADR-0007：台账以内容 hash 为主键，文件名仅为映射。
"""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

# Windows 控制台默认 GBK，统一输出 UTF-8 避免 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests
from dotenv import load_dotenv

SKILL_DIR = Path(__file__).resolve().parent.parent
# 配置优先级：进程环境变量 > 用户私有配置 > 技能目录（仅兼容旧安装，不应保存密钥）。
# 优先采用跨平台的用户目录，兼容 Windows 旧版 APPDATA 配置位置。
_CONFIG_CANDIDATES = [
    Path(os.getenv("TRANSCRIPT_INDEX_CONFIG_DIR", "")) if os.getenv("TRANSCRIPT_INDEX_CONFIG_DIR") else None,
    Path.home() / ".config" / "转录索引",
    Path(os.getenv("APPDATA", "")) / "转录索引" if os.getenv("APPDATA") else None,
]
USER_CONFIG_DIR = next((p for p in _CONFIG_CANDIDATES if p and (p / ".env").exists()), _CONFIG_CANDIDATES[0] or Path.home() / ".config" / "转录索引")
USER_ENV = USER_CONFIG_DIR / ".env"
if USER_ENV.exists():
    load_dotenv(USER_ENV, override=False)
load_dotenv(SKILL_DIR / ".env", override=False)

# ---------------- 配置 ----------------
_raw_src = os.getenv("SRC_DIR", "").strip()
if not _raw_src:
    raise RuntimeError("转录索引未配置 SRC_DIR；请编辑用户私有配置 %s" % USER_ENV)
SRC_DIR = Path(_raw_src).expanduser().resolve()          # 源目录（Obsidian vault）
if not SRC_DIR.is_dir():
    raise RuntimeError("转录索引源目录不存在：%s；请检查 SRC_DIR" % SRC_DIR)
API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
API_URL = os.getenv("SUMMARY_API_URL", "https://api.deepseek.com/v1/chat/completions")
API_MODEL = os.getenv("SUMMARY_API_MODEL", "deepseek-chat")
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-small-zh-v1.5").strip()
EMBED_MODEL_DIR = Path(os.getenv("EMBED_MODEL_DIR", "")).expanduser() if os.getenv("EMBED_MODEL_DIR") else None
STRICT_BILIBILI = os.getenv("STRICT_BILIBILI", "1").strip().lower() not in {"0", "false", "no"}
ALLOW_SOURCE_WRITE = os.getenv("ALLOW_SOURCE_WRITE", "0").strip().lower() in {"1", "true", "yes"}
AUTO_TERM = os.getenv("AUTO_TERM", "0").strip().lower() in {"1", "true", "yes"}
CONCURRENCY = int(os.getenv("CONCURRENCY", "8"))
MAX_RETRY = int(os.getenv("MAX_RETRY", "3"))
API_TIMEOUT = int(os.getenv("API_TIMEOUT", "240"))

# 产物目录（ADR-0005：在源目录内）
IDX_DIR = SRC_DIR / "索引库"
PROOF_DIR = SRC_DIR / "校对版"
VEC_DIR = SRC_DIR / "向量库"
MANIFEST = IDX_DIR / "manifest.json"
VEC_META = VEC_DIR / "metadata.json"
STATE_DIR = SRC_DIR / "索引配置"
TERM_TABLE = Path(os.getenv("TERM_TABLE", str(STATE_DIR / "术语表.md"))).expanduser()
TAG_TABLE = Path(os.getenv("TAG_TABLE", str(STATE_DIR / "标签词表.md"))).expanduser()
SNAPSHOT_DEST = Path(os.getenv("SNAPSHOT_DEST", "")).expanduser() if os.getenv("SNAPSHOT_DEST") else None

# 排除：产物目录 + 测试残留 + 非 txt
# 2026-08-16 修复：原正则含 [_\-]t[_\-\d]（IGNORECASE 下误杀 _T8star 等正常作者名，致 Krea2-Textfusion 漏索引），
# 仅保留精确测试特征（BV_TEST/BVFULL/BVPATH/端到端验证/测试作者），误杀归零。
EXCLUDE_DIRS = {"索引库", "校对版", "向量库", "索引配置", "索引"}
TEST_RESIDUE = re.compile(r"BV_TEST|BVFULL|BVPATH|端到端验证|测试作者", re.IGNORECASE)


def ensure_dirs():
    for d in (IDX_DIR, PROOF_DIR, VEC_DIR):
        d.mkdir(parents=True, exist_ok=True)


def artifact_key(rel, digest):
    """为源文件生成稳定且不冲突的产物键。

    旧版本只用 stem，递归目录中同名文件会覆盖彼此。新键保留可读 stem，
    再附加内容 hash 前缀；同内容移动时由 manifest 复用原键，内容变化时自然生成新键。
    """
    stem = Path(rel).stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .") or "untitled"
    stem = stem[:120]
    return f"{stem}--{str(digest)[:12]}"


def artifact_paths(key):
    """返回索引和校对产物路径；key 只来自内部 frontmatter/manifest。"""
    return IDX_DIR / f"{key}-idx.md", PROOF_DIR / f"{key}_校对版.txt"


def assign_artifact_keys(scans, manifest):
    """给扫描结果补充产物键，兼容旧版 stem 命名并为新文件使用 hash 键。"""
    files = manifest.get("files", {})
    for scan in scans:
        rec = files.get(scan.get("hash"), {})
        scan["artifact_key"] = (
            rec.get("artifact_key")
            or (Path(rec.get("rel")).stem if rec.get("rel") else None)
            or artifact_key(scan["rel"], scan["hash"])
        )
    return scans


def _atomic_write_text(path, text):
    """以同目录临时文件原子替换文本文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def refresh_moved(moved, manifest):
    """移动/改名同步索引与向量元数据，不重新调用 LLM。"""
    files = manifest.setdefault("files", {})
    vector_entries_path = VEC_DIR / "entries.json"
    vector_entries = None
    if vector_entries_path.exists():
        try:
            vector_entries = json.loads(vector_entries_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            vector_entries = None
    updated = 0
    for scan in moved:
        old_rel = files.get(scan["hash"], {}).get("rel")
        key = scan.get("artifact_key") or artifact_key(scan["rel"], scan["hash"])
        idx_path, _ = artifact_paths(key)
        if not idx_path.exists():
            continue
        text = idx_path.read_text(encoding="utf-8")
        source_line = f"源文件: {scan['rel']}"
        if re.search(r"^源文件: .+$", text, re.M):
            text = re.sub(r"^源文件: .+$", source_line, text, count=1, flags=re.M)
        else:
            text = source_line + "\n" + text
        if not re.search(r"^产物键: .+$", text, re.M):
            text = text.replace(source_line, source_line + f"\n产物键: {key}", 1)
        _atomic_write_text(idx_path, text)
        rec = files.get(scan["hash"], {})
        rec.update({"rel": scan["rel"], "name": scan["name"], "bv": scan.get("bv"),
                    "artifact_key": key, "ok": True})
        files[scan["hash"]] = rec
        if vector_entries is not None:
            for entry in vector_entries:
                if entry.get("artifact_key") == key or entry.get("file") == old_rel:
                    entry["file"] = scan["rel"]
                    entry["source_rel"] = scan["rel"]
                    entry["artifact_key"] = key
            _atomic_write_text(vector_entries_path, json.dumps(vector_entries, ensure_ascii=False, indent=1))
        updated += 1
    return updated


def remove_artifacts(key):
    """删除一个产物键对应的文件（不存在时幂等）。"""
    if not key:
        return
    for path in artifact_paths(key):
        if path.exists():
            path.unlink()


# ---------------- DeepSeek 调用 ----------------
def chat(messages, max_tokens=8000, temperature=0.3, timeout=None):
    """单次 chat 调用，指数退避重试。返回 content 文本。"""
    if not API_KEY:
        raise RuntimeError(f"未配置 OPENAI_API_KEY，请填写用户私有配置：{USER_ENV}")
    timeout = timeout or API_TIMEOUT
    last_err = None
    for attempt in range(MAX_RETRY):
        try:
            r = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {API_KEY}"},
                json={
                    "model": API_MODEL,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "stream": False,
                },
                timeout=timeout,
            )
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]
        except Exception as e:  # 网络错误/限流/超时
            last_err = e
            if attempt < MAX_RETRY - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"DeepSeek 调用失败({MAX_RETRY} 次): {last_err}")


# ---------------- 术语判定 ----------------
# 通用英文词黑名单：多义词被音译误报后全局替换会误伤其它语境（2026-08-14 污染治理）
TERM_BLACKLIST = {
    "cloud", "scale", "gate", "test", "taste", "style", "id", "easy", "issue",
    "starship", "stack", "slack", "close", "chrome", "inter", "non", "none",
    "prime", "template", "temperature", "sonnet", "opus", "lm", "git", "trade",
    "fb", "fp", "da", "ada", "plumb", "plum", "plug", "prompt", "jason", "json",
    "invali", "invalid", "embedd", "md", "mb", "cmb", "gmo", "b0", "es", "esm",
    "insize", "country", "dw", "acoust", "weboot", "web", "pdl", "px", "trip",
    "in", "op", "os", "ps", "ic", "rp", "top", "ed", "af",
    # 普通英文词不能作为全局转录纠错源词，避免把正文语义替换成专名。
    "client", "capacity", "wild", "tiger", "strip", "pause", "sell", "steal",
    "clean", "twin", "losing", "react", "codes", "menace", "chip", "manage",
    "compile", "mapper", "nexus", "request", "compile", "unit", "normal", "colors",
}
# 人工确认的中文错字白名单（全局替换无歧义）
CN_WHITELIST = {
    ("洛拉", "LoRA"), ("过你河", "过拟合"), ("欠你河", "欠拟合"),
    ("1万部", "1万步"), ("2000到3000部", "2000到3000步"),
}


def is_acceptable_term(wrong, right):
    """术语表收录判定（清洗 + 自动沉淀共用同一套）。
    返回 (bool, 原因)。只收高置信专名替换，宁缺毋滥。"""
    w, r = wrong.strip().lower(), right.strip()
    if w in TERM_BLACKLIST or r.lower() in TERM_BLACKLIST:
        return False, "通用词黑名单"
    if re.search(r"[一-鿿]", wrong) or re.search(r"[一-鿿]", right):
        if (wrong.strip(), right.strip()) in CN_WHITELIST:
            return True, ""
        return False, "含中文且非白名单"
    # 纯英文大小写变体或普通词源会造成全局替换误伤；仅接受明显的音译乱码/数字/混合专名。
    if wrong.isalpha() and right.isalpha() and wrong == right.lower():
        return False, "纯英文大小写变体"
    if re.fullmatch(r"[a-z]+", wrong) and wrong in TERM_BLACKLIST:
        return False, "普通英文词黑名单"
    if re.fullmatch(r"[A-Za-z]+", wrong) and wrong.lower() in {
        "client", "capacity", "wild", "tiger", "strip", "pause", "sell", "steal",
        "clean", "twin", "losing", "react", "codes", "menace", "chip", "manage",
    }:
        return False, "普通英文词黑名单"
    # 长度：ASCII 字母+数字合计（GPD3/FB16 含数字也算专名特征）
    wa = len(re.findall(r"[A-Za-z0-9]", wrong))
    ra = len(re.findall(r"[A-Za-z0-9]", right))
    if wa < 4 or ra < 4:
        return False, "字符数不足（源或目标 <4）"
    # 目标专名形态：含大写/数字/点号，或含空格命令形态且源词带大写（Gitclon→git clone）
    target_ok = bool(re.search(r"[A-Z0-9.]", right)) or (
        " " in right and bool(re.search(r"[A-Z]", wrong)))
    if not target_ok:
        return False, "目标词非专名形态（无大写/数字/点号）"
    return True, ""


# ---------------- 文件读取 ----------------
def read_lines(path):
    """读文本并按真实行拆行。转录文件存在 `\r\r\n` 双回车污染（2164/2210 行实测），
    必须先统一行尾再 splitlines，否则会出现假空行、行号错位。"""
    raw = path.read_text(encoding="utf-8", errors="replace")
    raw = raw.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    return raw.splitlines()


# ---------------- 台账 ----------------
def load_manifest():
    """读台账；不存在则返回空结构。"""
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {"version": 1, "files": {}, "bv_map": {}}


def save_manifest(mf):
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(mf, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(MANIFEST)


def file_hash(path):
    """sha256，读取前 4MB 即可（转录文件 <200KB，直接全读）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bv_from_name(name):
    """从文件名提取 BV 号，无则 None。"""
    m = re.search(r"(BV[0-9A-Za-z]{8,})", name)
    return m.group(1) if m else None


# ---------------- 源目录扫描 ----------------
def redirect_src(src_dir):
    """把产物目录重定向到 src_dir（体检 --src 用，防止 MANIFEST 等派生常量错位）。
    注意：只对本进程内 common 的模块级变量生效，已 from-import 的绑定不会更新，
    调用方须通过返回值或重新导入取最新路径。"""
    global SRC_DIR, IDX_DIR, PROOF_DIR, VEC_DIR, MANIFEST, VEC_META
    src = Path(src_dir).resolve()
    SRC_DIR = src
    IDX_DIR = src / "索引库"
    PROOF_DIR = src / "校对版"
    VEC_DIR = src / "向量库"
    MANIFEST = IDX_DIR / "manifest.json"
    VEC_META = VEC_DIR / "metadata.json"


def scan_sources():
    """递归扫描合规的 B 站转录 TXT，跳过不符合边界的普通文本。

    返回列表中的每个对象都含 `skip_reason` 为空；不合规文件不会送入 LLM，
    由 `scan_skips()` 提供可审计的跳过原因。
    """
    out = []
    for root, dirs, files in os.walk(SRC_DIR):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if not f.lower().endswith(".txt") or TEST_RESIDUE.search(f):
                continue
            abs_path = Path(root) / f
            rel = abs_path.relative_to(SRC_DIR).as_posix()
            bv = bv_from_name(f)
            if STRICT_BILIBILI and not bv:
                continue
            out.append({
                "rel": rel, "abs": abs_path, "name": f,
                "hash": file_hash(abs_path), "bv": bv,
            })
    out.sort(key=lambda x: x["rel"])
    return out


def scan_skips():
    """返回因严格 B 站边界而跳过的 TXT 路径及原因。"""
    if not STRICT_BILIBILI:
        return []
    skipped = []
    for root, dirs, files in os.walk(SRC_DIR):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if f.lower().endswith(".txt") and not TEST_RESIDUE.search(f) and not bv_from_name(f):
                path = (Path(root) / f).relative_to(SRC_DIR).as_posix()
                skipped.append({"rel": path, "reason": "文件名未找到合法 BV 号"})
    return sorted(skipped, key=lambda x: x["rel"])


def retire_replaced_versions(manifest, scans):
    """清理同一相对路径的旧 hash 版本，保留当前成功版本和新待处理版本。"""
    current_by_rel = {s["rel"]: s["hash"] for s in scans}
    files = manifest.setdefault("files", {})
    retired = []
    for digest, rec in list(files.items()):
        rel = rec.get("rel")
        current = current_by_rel.get(rel)
        current_rec = files.get(current, {}) if current else {}
        if current and current != digest and current_rec.get("ok") is True:
            retired.append((digest, rec))
            del files[digest]
    for digest, rec in retired:
        key = rec.get("artifact_key") or Path(rec.get("rel", rec.get("name", ""))).stem
        remove_artifacts(key)
    for bv, values in list(manifest.setdefault("bv_map", {}).items()):
        kept = [d for d in values if d in files]
        if kept:
            manifest["bv_map"][bv] = list(dict.fromkeys(kept))
        else:
            del manifest["bv_map"][bv]
    return retired


def classify_new(manifest, scans):
    """对照台账分类：new(新增) / skip(已索引未变) / moved(改名或移动) / changed(内容变更)。
    返回 (分类 dict, 更新后的 manifest)。ADR-0007：按 hash 识别，改名/移动仅更新映射。
    ADR-0009：同 hash 多路径只索引排序第一份，其余跳过并提示（防交替重切死循环）；
    台账 rel 路径仍在扫描中时，其它路径的副本一律 skip 而非 moved。
    """
    files = manifest.setdefault("files", {})
    cls = {"new": [], "skip": [], "moved": [], "changed": [], "dup_hash": [], "dup_bv": []}
    bv_seen = {}
    rels = {s["rel"] for s in scans}
    by_hash = {}
    for s in scans:
        by_hash.setdefault(s["hash"], []).append(s)
    for s in scans:
        rec = files.get(s["hash"])
        if rec is None:
            # 同一路径已有旧 hash 时属于内容变更：新版本成功前保留旧产物，成功后再退役。
            replaced = [
                (digest, old) for digest, old in files.items()
                if old.get("rel") == s["rel"] and digest != s["hash"]
            ]
            # 多路径：只索引排序后的第一份（scan_sources 已排序，顺序确定）。
            if by_hash[s["hash"]][0] is not s:
                cls["dup_hash"].append(s)
                continue
            s["artifact_key"] = artifact_key(s["rel"], s["hash"])
            if replaced:
                s["replaced_hashes"] = [digest for digest, _ in replaced]
                cls["changed"].append(s)
            else:
                cls["new"].append(s)
            bv_seen.setdefault(s["bv"], []).append(s["name"])
        elif rec.get("rel") != s["rel"]:
            if rec.get("rel") in rels:
                cls["skip"].append(s)       # 台账 rel 仍在 → 本文件是重复副本（ADR-0009）
            else:
                # 兼容旧版扁平 stem 产物；新文件使用 hash 后缀，避免同名覆盖。
                s["artifact_key"] = rec.get("artifact_key") or Path(rec.get("rel", s["rel"])).stem
                cls["moved"].append(s)      # 真移动/改名：hash 相同，映射更新
        elif rec.get("ok"):
            s["artifact_key"] = rec.get("artifact_key") or Path(s["rel"]).stem
            cls["skip"].append(s)
        else:
            s["artifact_key"] = rec.get("artifact_key") or artifact_key(s["rel"], s["hash"])
            cls["changed"].append(s)        # 上次失败未索引成功
    # 同 stem 不同内容会覆盖旧版扁平产物；新实现统一使用 hash 键，因此允许并存。
    # 同 BV 重复提示（不自动处理，ADR-0007）
    for bv, names in bv_seen.items():
        if bv and len(names) > 1:
            cls["dup_bv"].append((bv, names))
    return cls, manifest
