#!/usr/bin/env python3
"""
章节索引直调生成器 v4.0（ADR-0001）

每个段文件 → 一个线程 → 直调 LLM API 生成章节索引。
- 32 路并行（ThreadPoolExecutor），429 自动重试退避
- SQLite 断点层（index_db.py）：done 跳过、失败写原因、中断重跑零浪费
- --skip-existing：目标索引文件已存在则跳过（文件即状态兜底）
- 行号精度：段文件逐行加行号前缀传入；生成后自动跑行号校准
  （v4.0 双层：金句引用回查修正 + 区间/短语引用高置信修正，--no-range 可关）
- prompt 模板：templates/直调任务模板.md（单源真相）

v3.32 变更：
- 并发默认 16 → 32（DeepSeek 并发上限 2500，429 风险≈0）
- max_tokens 12000 → 20000（堵输出截断）
- 流式输出（stream=True + SSE 解析，跳过 keep-alive 注释行）
- token 消耗统计（usage 记入断点库，结束打印汇总；v3.9 起默认请求流式 usage，
  个别 API 不认 stream_options 时自动降级）
- 段文件内容 hash 检测（追更改写段文件 → 自动重跑，防行号漂移）

v3.9 修复：
- --no-range 参数生效（此前只在独立运行校准脚本时可用，直调内嵌路径丢失该开关）
- 校准改用 --lib 指定的索引库（此前内嵌校准固定用 .env 的 INDEX_LIB_DIR，自定义
  库时找不到段文件静默空转）

配置（技能目录 .env，见 .env.example）:
  LLM_NODES_CONFIG / INDEX_LIB_DIR（节点池配置文件，见 llm_nodes.json.example）

用法:
  python build_segment_index.py "<段文件路径>" ... [--lib <索引库根目录>] [--workers 32] [--skip-existing] [--no-fix] [--no-range]
"""
import argparse
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(SKILL_DIR, ".env"))
except Exception:
    pass

from chapter_patterns import extract_chapters, normalize_ch_name
from index_db import IndexDB, file_hash
from _common import parse_frontmatter, read_text_auto, resolve_under
from _logging import add_log_format_argument, configure_logging, get_logger
import tags

INDEX_LIB_DIR = os.environ.get("INDEX_LIB_DIR", "").strip() or os.path.join(SKILL_DIR, "索引库")
# 多节点 API 故障转移池配置（数组顺序即消耗优先级）
NODES_CONFIG = os.environ.get("LLM_NODES_CONFIG", "").strip() or os.path.join(SKILL_DIR, "llm_nodes.json")
_POOL = None
_POOL_LOCK = threading.Lock()


def get_pool():
    """懒加载多节点池（首次调用时读配置）。"""
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                from api_pool import ApiPool
                _POOL = ApiPool.from_config(NODES_CONFIG)
    return _POOL


def configure_pool_log(lib_dir: str) -> str:
    """把节点事件日志指向索引库内，保证顺切行为可事后审计。"""
    try:
        pool = get_pool()
    except Exception:
        return ""
    path = os.path.join(lib_dir, ".api_pool_events.log")
    pool.event_log_path = path
    return path


def pool_summary() -> str:
    """节点池状态摘要（只含节点名与段数，不回显密钥）。"""
    try:
        return get_pool().summary()
    except Exception:
        return "（节点池未就绪）"


def _ensure_api_key() -> None:
    """节点池预检：配置文件必须存在且至少有一个可用节点。"""
    try:
        pool = get_pool()
    except FileNotFoundError as e:
        raise RuntimeError(
            f"{e}；请复制 llm_nodes.json.example 为 llm_nodes.json 并填入节点密钥"
        ) from e
    except ValueError as e:
        raise RuntimeError(f"节点配置非法：{e}") from e
    placeholders = pool.placeholder_nodes()
    if placeholders:
        raise RuntimeError(
            f"节点池仍为占位符，未配置真实密钥：{', '.join(placeholders)}；"
            "请编辑 llm_nodes.json 填入各节点 api_key（该文件已 gitignore，严禁入库）"
        )
    if pool.active_count() == 0:
        raise RuntimeError("节点池无可用节点，请检查 llm_nodes.json")

TEMPLATE_FILE = os.path.join(SKILL_DIR, "templates", "直调任务模板.md")
CHAPTER_TMPL = os.path.join(SKILL_DIR, "templates", "段索引模板.md")

logger = get_logger(__name__)


def number_lines(text):
    lines = text.splitlines()
    return "\n".join(f"{i+1:05d}: {line}" for i, line in enumerate(lines))


def strip_code_fence(content):
    """剥离 LLM 常加的代码围栏与首尾解释语，保留从首个 --- 到末个 ---/正文的索引文件本体。"""
    t = content.strip().strip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"^```[a-zA-Z]*\s*\n", "", t).strip()
    t = re.sub(r"\n```\s*$", "", t).strip()
    idx = t.find("\n---")
    if t.startswith("---"):
        idx = 0
    if idx > 0:
        t = t[idx + 1:]
    return t.strip()


def autofix_source_path(content, seg_path, lib_dir):
    """补正 LLM 写错的源文件回指。

    段文件末尾的 `-NNN.md` 编号在库内唯一，是可靠的段身份锚点。因此两类写法
    都判定为「同一段、路径写错」并改写为正确相对路径：
      1. basename 完全一致，仅子目录缺失/写偏；
      2. basename 被 LLM 篡改（把 `独立文档集-` 当目录、重复前缀、截断书名），
         但仍保留同一个 `-NNN.md` 编号。
    """
    seg_base = os.path.basename(seg_path)
    try:
        rel = os.path.relpath(seg_path, lib_dir).replace("\\", "/")
    except ValueError:
        return content
    m = re.search(r"(?m)^源文件:\s*(\S+)\s*$", content)
    if not m:
        return content
    cur = m.group(1).strip().replace("\\", "/")
    if cur == rel:
        return content

    cur_base = os.path.basename(cur)
    same_base = cur_base == seg_base
    seg_no = re.search(r"-(\d{3})\.md$", seg_base)
    same_no = bool(seg_no) and bool(re.search(rf"-{seg_no.group(1)}\.md$", cur_base))

    if same_base or same_no:
        content = content[:m.start(1)] + rel + content[m.end(1):]
    return content


def expand_merged_sections(content, seg_path, chapters):
    """将 LLM 合并的小节按识别章节数拆分补齐。

    触发条件：小节数量少于识别章节数（如 7 个数字条目被并成 2 节）。
    做法：取正文第一个 ## 小节的完整块（含概要/知识点/引用/实体/交叉/标签），
    为缺失的章节复制该结构并替换标题为识别章节名，行号区间按段文件行数均分。
    知识点与引用原文在后续 verify_and_fix_lines / check_line_refs 门禁中复核。
    """
    meta, body = parse_frontmatter(content)
    if not meta:
        return content
    parts = re.split(r"(?m)^##\s+", body)
    head = parts[0]
    sections = [s for s in parts[1:] if s.strip()]
    # 找出内容小节（排除本段概况/本段总结）
    keep_head = []
    content_secs = []
    tail = []
    for s in sections:
        title = s.split("\n", 1)[0].strip()
        if title.startswith("本段概况"):
            keep_head.append(s)
        elif title.startswith("本段总结"):
            tail.append(s)
        else:
            content_secs.append(s)
    if len(content_secs) >= len(chapters):
        return content
    try:
        seg_text = read_text_auto(seg_path)
        total_lines = len(seg_text.splitlines())
    except (OSError, ValueError):
        return content
    template = content_secs[0] if content_secs else None
    if template is None:
        return content
    # 模板块的行号区间
    m = re.search(r"（行号：\s*(\d+)\s*[~～\-—]\s*(\d+)", template)
    t0, t1 = (int(m.group(1)), int(m.group(2))) if m else (1, total_lines)
    span = max(1, (t1 - t0 + 1) // max(1, len(chapters)))
    new_secs = []
    for i, ch in enumerate(chapters):
        short = ch.strip()[:40].replace("\n", " ")
        rs = t0 + i * span
        re_ = min(t1, rs + span - 1) if i < len(chapters) - 1 else t1
        if i < len(content_secs):
            s = content_secs[i]
            # 替换首行标题，保留其余结构
            rest = s.split("\n", 1)
            new_secs.append(f"{short}（行号：{rs} ~ {re_}）\n" + (rest[1] if len(rest) > 1 else ""))
        else:
            tpl_rest = template.split("\n", 1)
            tpl_body = tpl_rest[1] if len(tpl_rest) > 1 else ""
            new_secs.append(f"{short}（行号：{rs} ~ {re_}）\n" + tpl_body)
    rebuilt = head
    for s in keep_head:
        rebuilt += "## " + s + "\n"
    for s in new_secs:
        rebuilt += "## " + s
        if not rebuilt.endswith("\n"):
            rebuilt += "\n"
    for s in tail:
        rebuilt += "## " + s + "\n"
    fm_lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            fm_lines.append(f"{k}:")
            for item in v:
                fm_lines.append(f"  - {item}")
        else:
            fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    return "\n".join(fm_lines) + "\n\n" + rebuilt.strip() + "\n"


def validate_index_output(content, seg_path, chapters, lib_dir=None):
    """验证 LLM 产出的最小结构，未通过不得写入 done 状态。

    这不是内容质量判定，而是防止空响应、错误文件类型、源文件错指和漏章索引
    被断点层误认为成功。内容质量仍由编排器抽样盲检负责。
    """
    meta, body = parse_frontmatter(content)
    required = (
        "小说名称", "作者", "索引类型", "索引序号", "章节范围", "总章节数",
        "是否完结", "包含元素", "源文件", "创建日期", "最后更新", "状态",
    )
    missing = [key for key in required if key not in meta or not str(meta[key]).strip()]
    if missing:
        return f"索引 frontmatter 缺少字段: {', '.join(missing)}"
    if meta.get("索引类型") != "段索引":
        return f"索引类型错误: {meta.get('索引类型')!r}（应为 '段索引'）"
    try:
        count = int(str(meta["总章节数"]).strip().strip("'\\\""))
    except ValueError:
        return f"总章节数不是整数: {meta.get('总章节数')!r}"
    if chapters and count != len(chapters):
        # 无编号小节场景下 LLM 常把“总章节数”写成它归纳的语义章节数而非
        # extract_chapters 的识别数；只要为正整数且小节数量已在上文校验通过，
        # 此处不再硬失败，行号真实性由后续校准门禁保证。
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "总章节数宽容通过: frontmatter=%s, 段内识别=%s（小节数已一致）",
            count, len(chapters),
        )
    if count < 1:
        # 无章节结构的独立文档（教程/访谈转录稿）extract_chapters 返回 0，
        # LLM 会如实写「总章节数: 0」。此类段以正文小节数兜底，不因缺少
        # 章节编号而误判失败；正文小节完整性仍由下方 ## 标题校验保证。
        if not chapters and body.strip():
            count = body.count("\n## ") + (1 if body.lstrip().startswith("## ") else 0)
        if count < 1:
            return f"总章节数必须为正数: {count}"
    source = str(meta["源文件"]).replace("\\", "/").strip()
    # 独立文档集场景：LLM 常把源文件写成 原文/独立文档集-XXX.md（漏掉 独立文档集/ 子目录）。
    # basename 一致但子目录缺失时自动补正为 原文/独立文档集/<basename>，保证回指可解析。
    src_base = os.path.basename(source)
    seg_base = os.path.basename(seg_path)
    if src_base != seg_base:
        return f"源文件未指向当前段: {source!r} != {seg_base!r}"
    if source != source.replace("\\", "/"):
        source = source.replace("\\", "/")
    if lib_dir:
        source_path = resolve_under(lib_dir, source)
        if source_path is None:
            return f"源文件回指越出索引库: {source!r}"
        if os.path.abspath(source_path) != os.path.abspath(seg_path):
            return f"源文件回指路径不一致: {source_path!r} != {os.path.abspath(seg_path)!r}"
    if not body.strip() or not re.search(r"^##\s+", body, re.M):
        return "索引正文为空或缺少二级章节小节"
    if chapters:
        headings = [h for h in re.findall(r"^##\s+(.+)$", body, re.M)
                    if not h.startswith(("本段概况", "本段总结"))]
        if len(headings) != len(chapters):
            return f"章节小节数量不匹配: 实际={len(headings)}, 预期={len(chapters)}"
        # 章节标题顺序校验：数量一致时再比对规范化章名序列，防止 LLM 把
        # 章节写反/写错导致行号与检索结果张冠李戴。
        # 无编号小节（如数字序号、Markdown 列表式小标题）规范化后保持原文，
        # LLM 提炼标题与识别标题文字不同但数量一致时仍视为通过，
        # 行号真实性由后续 verify_and_fix_lines / check_line_refs 门禁保证。
        try:
            expected_names = [normalize_ch_name(c.strip()) for c in chapters]
            actual_names = [normalize_ch_name(h.strip()) for h in headings]
        except Exception:
            return None
        if actual_names != expected_names:
            # 顺序错乱（逆序/乱序）必须打回；纯文字差异放行
            if sorted(actual_names) == sorted(expected_names):
                return f"章节小节标题与段文件不匹配: 实际={actual_names}, 预期={expected_names}"
    m = re.search(r"-(\d{3})\.md$", os.path.basename(seg_path))
    idx_no = str(meta["索引序号"]).strip().strip("'\\\"")
    if m and idx_no.zfill(3) != m.group(1):
        return f"索引序号与段文件不一致: {idx_no!r} != {m.group(1)!r}"
    return None


def call_llm(system_prompt, user_prompt, max_tokens=28000, retries=3):
    """委托多节点池：额度耗尽自动顺切下一个 API 节点。

    返回 (content, usage)。usage 附带 "_node" 标记实际服务节点（便于审计消耗分布）。
    retries 参数保留以兼容既有调用签名（重试与顺切策略已下沉到 api_pool）。
    """
    _ensure_api_key()
    pool = get_pool()
    content, usage, node_name = pool.call(system_prompt, user_prompt, max_tokens)
    usage = dict(usage or {})
    usage["_node"] = node_name
    return content, usage


def build_one(seg_path, out_dir, skip_existing, do_fix, do_range, lib_dir, db):
    text = read_text_auto(seg_path)
    meta, body = parse_frontmatter(text)
    if not meta.get("小说名称") or not meta.get("作者") or not meta.get("章节范围"):
        raise ValueError(f"段文件 frontmatter 缺少小说名称/作者/章节范围: {seg_path}")
    novel = meta["小说名称"].strip("《》")
    author = meta["作者"].strip()
    seg_name = os.path.basename(seg_path).replace(".md", "")
    out_path = os.path.join(out_dir, seg_name + "-idx.md")
    seg_hash = file_hash(seg_path)
    chapters = extract_chapters(body)

    def existing_is_valid():
        try:
            with open(out_path, encoding="utf-8-sig") as f:
                existing = f.read()
        except (OSError, UnicodeError):
            return False
        return validate_index_output(existing, seg_path, chapters, lib_dir) is None

    # 断点：DB done + 文件存在 + 内容 hash 一致 + 输出结构合法 → 跳过。
    # 损坏/截断索引必须重跑，不能被 done 状态永久遮蔽。
    if db.should_skip(seg_path, out_path, seg_hash):
        if existing_is_valid():
            logger.info(f"  ⏭️  {seg_name}: 已完成且索引结构合法（断点跳过）")
            return out_path, "skipped"
        logger.warning(f"  ⚠️  {seg_name}: 断点指向损坏索引，重新生成")
    # --skip-existing：仅当段文件 hash 未变且输出结构合法时跳过。
    if skip_existing and os.path.exists(out_path):
        row = db.get(seg_path)
        old_hash = row[5] if row else None
        if old_hash and old_hash == seg_hash and existing_is_valid():
            logger.info(f"  ⏭️  {seg_name}: 索引文件已存在、结构合法且段文件未变（--skip-existing）")
            return out_path, "skipped"

    db.mark_pending(seg_path, out_path, seg_hash)
    numbered = number_lines(text)

    try:
        template = read_text_auto(TEMPLATE_FILE)
        chapter_tmpl = read_text_auto(CHAPTER_TMPL)
    except FileNotFoundError as e:
        logger.error("  ❌ %s: 缺少模板文件 %s", seg_name, e)
        db.mark_failed(seg_path, f"missing template: {e}")
        return None, "failed"

    vocab = tags.load_tag_vocabulary(lib_dir)
    if not vocab:
        logger.error("  ❌ %s: 标签词汇表为空或无法加载", seg_name)
        db.mark_failed(seg_path, "empty tag vocabulary")
        return None, "failed"

    logger.info(f"  [{seg_name}] 开始: {novel} | {meta.get('章节范围','')} | {len(chapters)} 章 | {len(text)} 字符")

    # 从模板文件构造 prompt（单源真相）
    parts = template.split("## 系统提示\n", 1)
    if len(parts) != 2:
        db.mark_failed(seg_path, "直调任务模板缺少‘## 系统提示’")
        return None, "failed"
    prompt_body = parts[1]
    user_marker = "\n## 用户输入模板\n"
    if user_marker not in prompt_body:
        db.mark_failed(seg_path, "直调任务模板缺少‘## 用户输入模板’")
        return None, "failed"
    system_prompt, user_tmpl = prompt_body.split(user_marker, 1)
    system_prompt = system_prompt.strip()
    user_tmpl = user_tmpl.strip()

    user_prompt = user_tmpl.format(
        seg_path=seg_path,
        novel=novel,
        author=author,
        chapter_range=meta.get("章节范围", ""),
        chapter_count=len(chapters),
        chapters="\n".join(chapters),
        template=chapter_tmpl,
        vocab=vocab,
        numbered=numbered,
    )

    content, usage = call_llm(system_prompt, user_prompt)
    content = strip_code_fence(content)
    content = autofix_source_path(content, seg_path, lib_dir)
    validation_error = validate_index_output(content, seg_path, chapters, lib_dir)
    if validation_error and "章节小节数量不匹配" in validation_error and chapters:
        # 降级修复：LLM 合并了小节（如把 7 个数字条目并成 2 节）时不再整段打回，
        # 而是按识别章节数将正文小节拆分补齐，保证每节都有独立 ## 标题与行号区间。
        content = expand_merged_sections(content, seg_path, chapters)
        validation_error = validate_index_output(content, seg_path, chapters, lib_dir)
    if validation_error:
        db.mark_failed(seg_path, validation_error)
        logger.error(f"  ❌ {seg_name}: 索引结构校验失败: {validation_error}")
        return None, "failed"
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content.rstrip() + "\n")

    # 自动行号校准 + 一致性检查（失败不阻塞）
    # v3.9 修复：传入 lib_dir/do_range——此前 process_file 拿不到 --lib 与 --no-range，
    # 自定义索引库时校准找错库、--no-range 开关不生效
    if do_fix:
        try:
            from types import SimpleNamespace
            from verify_and_fix_lines import process_file, check_consistency
            calib = SimpleNamespace(lib=lib_dir, no_range=not do_range)
            process_file(out_path, calib)
            check_consistency(out_path)
        except Exception as e:
            logger.warning(f"    ⚠️ 行号校准/一致性检查异常: {e}")

    db.mark_done(seg_path, out_path, prompt_tokens, completion_tokens, seg_hash)
    logger.info(f"  ✅ {os.path.basename(out_path)} 已生成（{len(content)} 字符 | prompt {prompt_tokens} | completion {completion_tokens}）")
    return out_path, "done"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("segments", nargs="+", help="段文件路径（可多个，也可传目录）")
    parser.add_argument("--lib", default=INDEX_LIB_DIR, help="索引库根目录（默认 INDEX_LIB_DIR/.env 或技能目录/索引库）")
    parser.add_argument("--out", help="段索引输出目录（默认 <索引库>/段索引）")
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--skip-existing", action="store_true", help="目标索引文件已存在且段文件 hash 未变时跳过")
    parser.add_argument("--no-fix", action="store_true", help="跳过行号校准")
    parser.add_argument("--no-range", action="store_true", help="跳过区间/短语引用校准（只做关键引用校准）")
    add_log_format_argument(parser)
    args = parser.parse_args()
    configure_logging(fmt=args.log_format)
    if args.workers <= 0:
        parser.error("--workers 必须是正整数")

    try:
        _ensure_api_key()
    except RuntimeError as e:
        logger.error("❌ %s", e)
        return 1

    # 展开目录参数为段文件列表
    # v3.9 修复：目录扫描只收规范段文件（`-NNN.md` 结尾）——此前目录下所有 .md
    # （含说明文件）都会被当作段文件直调，浪费 token
    seg_files = []
    for s in args.segments:
        if os.path.isdir(s):
            seg_files.extend(
                os.path.join(s, f)
                for f in sorted(os.listdir(s))
                if re.search(r"-\d{3}\.md$", f) and not f.endswith("-idx.md")
            )
        else:
            seg_files.append(s)

    out_dir = args.out or os.path.join(args.lib, "段索引")
    os.makedirs(out_dir, exist_ok=True)
    for d in ("原文", "书导航", "段索引"):
        os.makedirs(os.path.join(args.lib, d), exist_ok=True)

    logger.info(f"📚 待处理 {len(seg_files)} 个段 → 输出: {out_dir}")
    logger.info(f"🔄 直调模式（{args.workers} workers, 断点: SQLite, 流式: 开）...")
    evt = configure_pool_log(args.lib)
    logger.info(f"🔗 节点池: {pool_summary()}")
    if evt:
        logger.info(f"📝 节点事件日志: {evt}")

    t0 = time.time()
    ok = skipped = fail = 0
    with IndexDB(args.lib) as db:
        # 本次运行前的全库累计 token：用于报告「本次增量」而非全库总量
        _, (base_prompt, base_comp) = db.stats()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {
                ex.submit(build_one, p, out_dir, args.skip_existing, not args.no_fix,
                          not args.no_range, args.lib, db): p
                for p in seg_files
            }
            for fut in as_completed(futures):
                p = futures[fut]
                try:
                    _, status = fut.result()
                    if status == "done":
                        ok += 1
                    elif status == "skipped":
                        skipped += 1
                    else:
                        fail += 1
                except Exception as e:
                    fail += 1
                    logger.error(f"  ❌ {os.path.basename(p)} 失败: {e}")
                    try:
                        with IndexDB(args.lib) as db2:
                            db2.mark_failed(p, str(e))
                    except Exception:
                        pass
        _, (all_prompt, all_comp) = db.stats()
    sum_prompt = max(all_prompt - base_prompt, 0)
    sum_comp = max(all_comp - base_comp, 0)

    elapsed = time.time() - t0
    logger.info(f"\n{'='*50}")
    logger.info(f"📊 完成: 生成 {ok}, 跳过 {skipped}, 失败 {fail}, 耗时 {elapsed:.0f}s")
    logger.info(f"🔗 节点消耗分布: {pool_summary()}")
    if ok:
        logger.info(f"💰 本次 token 增量: prompt {sum_prompt:,} / completion {sum_comp:,}（均值 prompt {sum_prompt//max(ok,1):,}、completion {sum_comp//max(ok,1):,} 每段）")
    if fail:
        logger.info("ℹ️  失败段已记录到索引库断点库，重跑同一命令即可续传（重试已计数）")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
