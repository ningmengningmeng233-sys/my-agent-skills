#!/usr/bin/env python3
"""Shared utilities used by extract_segments, scan_registry, generate_prompts and check_schedule."""
import hashlib
import os
import re
from pathlib import Path


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def file_sha256(path: str | Path) -> str | None:
    """文件内容指纹（前 16 位十六进制）；不可读返回 None。

    状态机与依赖图都要按内容判失效，二者不得各写一份哈希实现。
    """
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def read_text_auto(path: str | Path) -> str:
    """Read text with encoding fallback utf-8 → GB18030 → GBK."""
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    return Path(path).read_text(encoding="utf-8", errors="replace")


def load_asset_types(lib) -> dict[str, str]:
    """资产索引 → {名称/别名: 类型}：**类型判定的唯一权威来源**（索引缺失/不可读返回空）。

    模型在「衍生需求汇总」「调度清单」里写的类型只是提示，实测会把服装写成「道具」，
    错类型一路流到衍生步骤与两道检查器。四处消费者必须查同一份映射。
    """
    import json
    path = Path(lib) / ".process" / "资产索引.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for a in data.get("assets", []) or []:
        if not isinstance(a, dict):
            continue
        kind = str(a.get("type") or "")
        names = [str(a.get("name") or "")]
        names += re.split(r"[、/，,]", str(a.get("alias_display") or ""))
        for n in names:
            n = n.strip()
            if n and kind and n not in out:
                out[n] = kind
    return out


def env_candidates(source=None) -> list[Path]:
    """统一配置来源（顺序即优先级）：ASSET_ENV_PATH → 技能目录 .env → ~/.asset-extract/.env。"""
    env = os.environ if source is None else source
    skill_dir = Path(__file__).resolve().parent.parent.parent
    out = [skill_dir / ".env", Path.home() / ".asset-extract" / ".env"]
    if env.get("ASSET_ENV_PATH"):
        out.insert(0, Path(env["ASSET_ENV_PATH"]))
    return out


def loadable_candidates(source=None) -> list[Path]:
    """离线模式（ASSET_OFFLINE 置真）下的可加载子集：只认显式 `ASSET_ENV_PATH`。

    这是问题清单 B8 的根因拦截点：包外隐式回退（技能目录 `.env` /
    `~/.asset-extract/.env`）会往进程注入密钥与 `GOOGLE_GEMINI_BASE_URL`
    （本机中转池），使「unset 密钥 = 不外呼」的前提从不成立。
    离线时禁用隐式回退，密钥只可能来自显式指定的文件或进程环境。
    """
    env = os.environ if source is None else source
    cands = env_candidates(env)
    try:
        from .offline import offline_requested
    except ImportError:      # 兼容 `scripts/engine` 直接入 sys.path 的扁平导入（同 toml 垫片口径）
        from offline import offline_requested      # type: ignore[no-redef]

    if not offline_requested(env):
        return cands
    explicit = str(env.get("ASSET_ENV_PATH") or "").strip()
    if not explicit:
        return []
    return [p for p in cands if p == Path(explicit)]


def load_env() -> str | None:
    """Load .env from ASSET_ENV_PATH, skill dir or ~/.asset-extract/.env.

    离线模式（`ASSET_OFFLINE`）置真时跳过隐式回退，仅加载显式 `ASSET_ENV_PATH`。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return None
    for p in loadable_candidates():
        if p.is_file():
            load_dotenv(p)
            return str(p)
    return None


def env_values(source=None) -> tuple[dict[str, str], Path | None]:
    """只读配置视图：{**文件值, **进程环境} 与命中的文件路径（未命中为 None）。

    与 load_env() 同源同序，但**不调用 load_dotenv**（不写进程环境），供体检/状态这类
    只读路径使用。进程环境优先，与 load_dotenv 的 override=False 一致。
    """
    env = dict(os.environ if source is None else source)
    try:
        from dotenv import dotenv_values
    except ImportError:
        dotenv_values = None      # 缺依赖由体检的「依赖包」项点名，这里不另造口径
    values: dict[str, str] = {}
    hit: Path | None = None
    # 与 load_env() 同源：离线模式下同样屏蔽隐式包外文件，保证体检口径与执行链一致。
    for path in loadable_candidates(env):
        if not path.is_file() or dotenv_values is None:
            continue
        try:
            values = {k: v for k, v in dotenv_values(path).items() if v is not None}
            hit = path
        # UnicodeDecodeError 是 ValueError 子类，**必须单独点出来**：.env 存成 GB18030/UTF-16
        # （中文用户从 .env.example 另存很容易发生）时它会逃出去，让体检与 status 整体崩。
        # 旧实现用 read_text(errors="replace") 只读乱码不崩，这里不能比它更脆。
        except (OSError, UnicodeDecodeError):
            continue
        break
    merged = dict(values)
    merged.update(env)
    return merged, hit


def toml_module():
    """TOML 解析模块：3.11+ 用标准库 `tomllib`，3.10 退到 `tomli`。

    **新代码请只用本函数**，不要再自带 try/except（缺陷 F33）。
    事实是：本仓另有 8 个生产模块各自抄了一段同样的 try/except（已由守卫测试允许，
    因为它们确实处理了 tomli），而测试码曾裸写 `import tomllib` 共 5 处、根本没有回退，
    于是"支持 3.10"这句话从来没成立过——真 3.10 上测试套件 5 红。
    守卫见 `测试/unit/test_py310_compat.py::test_no_file_uses_tomllib_without_handling_tomli`；
    把这 8 处也收敛到本函数是已登记的待办——它不是机械替换：那 8 处同时支持
    `engine.x` 与扁平 `x` 两种导入写法（顶层 try/except 正是为此存在），
    收敛必须连带改写那层导入兼容垫片，需单独一单配真 3.10 回归。
    """
    try:
        import tomllib
        return tomllib
    except ModuleNotFoundError:
        import tomli
        return tomli


def toml_loads(text: str) -> dict:
    """TOML 文本 → dict（走 `toml_module` 的兼容口径）。"""
    return toml_module().loads(text)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse YAML-ish frontmatter and return (meta, body)."""
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[3:end]
            body = text[end + 4:].lstrip("\n")
            for line in fm.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"')
    return meta, body


def number_lines(text: str) -> str:
    """Prefix each line with a 5-digit line number."""
    return "\n".join(f"{i+1:05d}: {line}" for i, line in enumerate(text.splitlines()))


def split_template(path: str | Path) -> tuple[str, str]:
    """Split a template into system prompt and user template sections."""
    parts = read_text(path).split("## 系统提示\n", 1)
    if len(parts) != 2:
        raise ValueError(f"模板格式错误（需含 ## 系统提示 与 ## 用户输入模板 分节）: {path}")
    system = parts[1].split("\n## 用户输入模板\n", 1)[0].strip()
    user_tmpl = parts[1].split("\n## 用户输入模板\n", 1)[1].strip()
    return system, user_tmpl


def range_label(path: str | Path) -> str:
    """Extract a chapter-range label from a segment filename."""
    p = Path(path)
    m = re.search(r"(第[^\-]+(?:~第[^\-]+)?|第\d+万字~第\d+万字)", p.stem)
    return m.group(1) if m else p.name


_SAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(s: str | None) -> str:
    """Sanitize a string for use as a filename."""
    return _SAFE_RE.sub("_", (s or "").strip())[:60] or "未命名"
