# -*- coding: utf-8 -*-
"""转录索引 · LLM 切条目（校对双轨之二）

ADR-0003：LLM 读原文 → 输出规范简体条目 JSON（摘录即校对），行号区间映射原始文件。
拷问 Q7：DeepSeek 每文件一次调用；行号区间脚本校验防漂移；重试 3 次。
拷问 C2：顺带输出「纠错建议」沉淀术语表；检测摘要占位符并补全写回原文件。

用法: python 切条目.py [文件名相对源目录]   # 缺省 = 全库增量
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ALLOW_SOURCE_WRITE, AUTO_TERM, CONCURRENCY, IDX_DIR, PROOF_DIR,
                    SRC_DIR, TAG_TABLE, TERM_TABLE, artifact_key, artifact_paths,
                    assign_artifact_keys, chat, classify_new, ensure_dirs, file_hash,
                    is_acceptable_term, load_manifest, read_lines, refresh_moved,
                    retire_replaced_versions, save_manifest, scan_sources)
from 校对 import fix_terms, load_term_table, opencc_convert

PROMPT_TEMPLATE = """你是知识库索引员，把下面的视频转录文档切成「知识单元」索引条目。

## 切条目规则
1. 条目 = 能独立回答一个问题的知识单元（如「Network Rank 怎么选」）。同一主题散落多处 → 合并为一条（anchors 覆盖各处）。
2. 条目数由内容密度自然决定：教程类 5-15 条；观点类按论点分条；清单/速查/素材类（音效标签清单、快捷键表、资源列表等）必须切 1 条「速查清单」条目（name 如「XX 速查清单」）。**正文非空时禁止输出空 items**——即使内容琐碎也要至少 1 条。
3. 摘录：用规范简体忠实转写原文关键内容（100-300 字），纠正转录错字（参考纠错表），不增补原文没有的知识。
4. 标签：优先从标签词表选；词表外确有必要才新增。
5. anchors（定位锚点）：从上面正文中**逐字照抄 2-4 个连续短语**（每个 6-20 字，允许包含转录错字——原文是 Laura 就写 Laura；必须单行内连续出现，不要跨行拼接）。脚本据此精确定位行号。**不要写行号数字**。
6. 顺带输出纠错建议：**只报「英文/外文专有名词被转录错」**（如 Laura→LoRA、Coya→Kohya），不超过 10 条，只报确信的；中文同音错字不要报（会全局替换误伤其它语境）。
7. **输出 JSON 硬规则**：条目名/概要/摘录中**禁止出现英文双引号 "**（引用一律用中文「」），否则 JSON 解析失败。

## 标签词表
{tags}

## 纠错表（转录错字参考）
{terms}

## 输出格式（严格 JSON，不要任何其它文字或解释）
{{"items": [{{"name": "条目名", "summary": "一句话概要(≤40字)", "excerpt": "摘录100-300字", "tag": "标签", "anchors": ["逐字短语1", "逐字短语2", ...]}}], "term_fixes": ["错字 → 正确"]}}

## 转录文档（每行前有行号）
{body}
"""

PLACEHOLDER_RE = re.compile(r"AI待处理|待处理.*替换|占位")


def load_table(path, default=""):
    """读词表/纠错表为纯文本片段（过滤注释行）。"""
    if not path.exists():
        return default
    lines = []
    for l in path.read_text(encoding="utf-8").splitlines():
        l = l.strip()
        if not l or l.startswith("#") or l.startswith(">") or l.startswith("##"):
            continue
        lines.append(l)
    return "\n".join(lines) if lines else default


def split_body(body, max_chars=40000):
    """按行累计字符分块（行号前缀全局真实，行不拆分）。返回 [(块序号, 文本), ...]。"""
    lines = body.splitlines()
    chunks, cur, size, idx = [], [], 0, 1
    for l in lines:
        cur.append(l)
        size += len(l)
        if size >= max_chars:
            chunks.append((idx, "\n".join(cur)))
            cur, size, idx = [], 0, idx + 1
    if cur:
        chunks.append((idx, "\n".join(cur)))
    return chunks


def extract_json(text):
    """从 LLM 输出中稳健提取 JSON 对象（2026-08-14 容错链）：
    json.loads → 引号修复 → demjson3 宽松模式（容忍未转义引号/漏逗号/尾逗号）。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("输出中无 JSON 对象")
    seg = text[start:end + 1]
    try:
        return json.loads(seg)
    except json.JSONDecodeError:
        try:
            return json.loads(fix_json_quotes(seg))
        except Exception:
            import demjson3
            return demjson3.decode(seg, strict=False)


def fix_json_quotes(seg):
    """扫描 JSON 字符串状态：字符串值内部出现的裸双引号/反斜杠 → 转义。
    仅修「引号后不是结构符（,}] 空白）」的裸引号，不破坏合法结构。"""
    out, in_str, i = [], False, 0
    n = len(seg)
    while i < n:
        ch = seg[i]
        if in_str:
            if ch == "\\":
                out.append(ch); out.append(seg[i + 1] if i + 1 < n else "")
                i += 2
                continue
            if ch == '"':
                nxt = seg[i + 1] if i + 1 < n else ""
                if nxt in ",}]:\n\r\t " or nxt == "":
                    in_str = False
                    out.append(ch)
                else:
                    out.append("\\\"")      # 值内裸引号 → 转义
            else:
                out.append(ch)
            i += 1
            continue
        # 非字符串状态
        if ch == '"':
            in_str = True
        out.append(ch)
        i += 1
    return "".join(out)


def check_lines(lines_range, total_lines):
    """行号区间合法性：闭区间、在 [1, total] 内、起<=止。"""
    out = []
    for s, e in lines_range:
        if not (isinstance(s, int) and isinstance(e, int)):
            continue
        if 1 <= s <= e <= total_lines:
            out.append([s, e])
    return out


def norm_anchor(text):
    """锚点/正文行归一化：去空白 + 繁转简 + 术语替换（LLM 会修正错字/改简体，容忍后匹配）。"""
    t = re.sub(r"\s+", "", text or "")
    t = opencc_convert(t)
    t = fix_terms(t, _term_pairs)
    return t


_term_pairs = []


def build_normed_lines(body_lines):
    """正文行归一化（去空白+繁转简+术语替换），供锚点/降级定位共用。"""
    return [(idx, norm_anchor(line)) for idx, line in enumerate(body_lines)]


def to_ranges(hits, body_start, total_lines):
    """命中行 → 区间列表：±3 行；相邻 >100 行拆分（2026-08-16 收窄，原 300 行致
    长摘录 n-gram 命中行被并成大区间 → 条目区间交叉重叠，如 Krea2 条目 2/3）。"""
    hits = sorted(set(hits))
    if not hits:
        return []
    ranges, cur_s, cur_e = [], hits[0], hits[0]
    for h in hits[1:]:
        if h - cur_e > 100:
            ranges.append([max(body_start + 1, cur_s - 3), min(total_lines, cur_e + 3)])
            cur_s = h
        cur_e = h
    ranges.append([max(body_start + 1, cur_s - 3), min(total_lines, cur_e + 3)])
    return ranges


def locate_anchors(anchors, normed_lines, body_start, total_lines):
    """锚点定位（反行号漂移的可靠方案，试点教训）：
    LLM 报短语 → 脚本在正文中精确查找 → 行号由脚本计算，零漂移。
    匹配前双方做归一化（去空白+繁转简+术语替换），容忍 LLM 修正锚点。"""
    hits = []
    for a in anchors:
        na = norm_anchor(a)
        if len(na) < 4:
            continue
        for idx, nl in normed_lines:
            if na in nl:
                hits.append(body_start + 1 + idx)
                break
    return to_ranges(hits, body_start, total_lines)


def locate_by_ngram(excerpt, normed_lines, body_start, total_lines):
    """锚点全 miss 时的降级定位（对应 v3.33 fix_range_refs 思想：先再确认一轮再放弃）：
    取摘录 4 字片段在正文找独立命中行，≥3 个命中行才推区间；不足则放弃条目。"""
    seg = norm_anchor(excerpt)
    if len(seg) < 12:
        return []
    hits = []
    for i in range(0, len(seg) - 3, 4):
        frag = seg[i:i + 4]
        for idx, nl in normed_lines:
            if frag in nl:
                hits.append(body_start + 1 + idx)
                break
    if len(set(hits)) < 3:
        return []
    return to_ranges(hits, body_start, total_lines)


def check_excerpt(excerpt, lines_range, proof_segs):
    """摘录与区间文本的关键词匹配校验（防行号漂移，拷问 Q7）。
    摘录是 LLM 重述而非逐字引用（试点教训），取摘录中的英文词/数字做命中检测：
    在区间覆盖的校对段落文本中出现 ≥1 个即通过（位置大体正确）；0 命中判漂移。"""
    if not lines_range or not excerpt:
        return False, "摘录或行号区间为空"
    hit_text = ""
    for s, e in lines_range:
        for (ps, pe, ptext) in proof_segs:
            if not (pe < s or ps > e):      # 区间与段落重叠
                hit_text += ptext
    if not hit_text:
        return False, "行号区间未覆盖任何校对段落"
    tokens = re.findall(r"[A-Za-z]{2,}|\d+", excerpt)
    tokens = [t for t in tokens if len(t) >= 2]
    hits = [t for t in set(tokens) if t in hit_text]
    if hits:
        return True, None
    # 英文术语可能是 LLM 重述引入（原文无此词，如 unet），兜底：中文 4 字片段命中
    cn4 = re.findall(r"[一-鿿]{4}", excerpt)
    cn_hits = [c for c in set(cn4) if c in hit_text]
    if len(cn_hits) >= 2:
        return True, None
    return False, f"摘录在区间内 0 命中（可能行号漂移）: 英:{tokens[:4]} 中:{cn_hits[:4]}"


def parse_meta(lines):
    """提取头部元数据（标题/作者/日期/BV）。"""
    text = "\n".join(lines[:60])
    meta = {}
    m = re.search(r"视频标题[:：]\s*(.+)", text)
    if m: meta["标题"] = m.group(1).strip()
    m = re.search(r"作者[:：]\s*(.+)", text)
    if m: meta["作者"] = m.group(1).strip()
    m = re.search(r"发布时间[:：]\s*([\d\-]+)", text)
    if m: meta["发布时间"] = m.group(1)
    m = re.search(r"(BV[0-9A-Za-z]{8,})", text)
    if m: meta["BV号"] = m.group(1)
    return meta


def fix_summary_placeholder(src_abs, lines):
    """检测摘要占位符 → DeepSeek 补写结构化摘要 → 写回原文件。返回补全数量。
    2026-08-16 修复：原实现只补第一个占位符，多占位符文件仅修一处；正文截取懒构建一次。"""
    body_text = None
    n = 0
    for i, l in enumerate(lines):
        if PLACEHOLDER_RE.search(l):
            # 用正文生成摘要（懒构建一次）
            if body_text is None:
                body_start = 0
                for j, x in enumerate(lines):
                    if "第二部分" in x:
                        body_start = j + 1
                        break
                body_text = "\n".join(lines[body_start:body_start + 300])[:12000]
            prompt = f"""你是视频摘要师。根据以下转录正文，写结构化摘要（与知识库既有格式一致）：
### 核心观点
（1-2 句）
### 主要论点
1. **...**：...
2. ...
### 关键结论
- ...

转录正文：
{body_text}"""
            try:
                summary = chat([{"role": "user", "content": prompt}], max_tokens=2000)
            except Exception as e:
                print(f"  ⚠ 摘要补全失败: {e}")
                return n
            # 摘要必须保持单行，否则写回会改变物理行数坐标。
            lines[i] = re.sub(r"\s+", " ", summary).strip()
            n += 1
            print(f"  ✓ 补全摘要占位符（原第 {i + 1} 行）")
    if n:
        if not ALLOW_SOURCE_WRITE:
            raise RuntimeError("检测到摘要占位符；默认禁止写回源 TXT，请在私有配置中设置 ALLOW_SOURCE_WRITE=1 后重试")
        src_abs.write_text("\n".join(lines), encoding="utf-8")
    return n


def process_one(scan, tag_list, term_list):
    """单个文件：读原文 → LLM 切条目 → 校验 → 写索引 md → 术语表/标签表增量。"""
    global _term_pairs
    _term_pairs = load_term_table()
    rel, abs_path = scan["rel"], scan["abs"]
    digest = scan.get("hash") or file_hash(abs_path)
    key = scan.get("artifact_key") or artifact_key(rel, digest)

    # 1) 读原文（read_lines 统一 `\r\r\n` 双回车污染行尾，保证行号坐标系与校对版一致）
    lines = read_lines(abs_path)
    total_lines = len(lines)
    meta = parse_meta(lines)

    # 2) 摘要占位符补全（写回原文件）
    fix_summary_placeholder(abs_path, lines)

    # 3) 构建带行号正文（从「第二部分」后起，最多 4 万 token 输入预算）
    body_start = 0
    for i, l in enumerate(lines):
        if "第二部分" in l:
            body_start = i + 1
            break
    body = "\n".join(f"{i + 1}:{l}" for i, l in enumerate(lines[body_start:], start=body_start))

    # 4) 调 LLM 切条目
    # 2026-08-14 教训：大文件（正文 >4 万字符）单次调用输出会超 token 上限被截断→JSON 未闭合→反复失败。
    # 治本：按全局真实行号分块（每块 ≤4 万字符），每块一次调用，条目直接拼接（行号各自正确）。
    all_items, all_fixes = [], []
    for idx, chunk in split_body(body, 40000):
        prompt = PROMPT_TEMPLATE.format(tags=tag_list, terms=term_list, body=chunk)
        last_err = None
        data = None
        for attempt in range(3):
            try:
                out = chat([{"role": "user", "content": prompt}], max_tokens=20000, temperature=0.2)
                data = extract_json(out)
                break
            except Exception as e:
                last_err = e
                print(f"  ⚠ {rel}[块{idx}]: 第 {attempt + 1} 次尝试失败 ({e})，重试…")
                time.sleep(2 ** attempt)
        if data is None:
            raise RuntimeError(f"切条目失败[块{idx}]: {last_err}")
        all_items.extend(data.get("items", []))
        all_fixes.extend(data.get("term_fixes", []))
    data = {"items": all_items, "term_fixes": all_fixes}

    # 5) 校验行号区间 + 摘录匹配（读校对版段落做匹配底）
    _, proof_path = artifact_paths(key)
    proof_segs = []
    if proof_path.exists():
        plines = proof_path.read_text(encoding="utf-8").splitlines()
        cur = None
        for l in plines:
            m = re.match(r"> 第\d+段 \[原 第(\d+)-(\d+)行\]", l)
            if m:
                if cur: proof_segs.append(tuple(cur))
                cur = [int(m.group(1)), int(m.group(2)), ""]
            elif cur is not None:
                cur[2] += l
        if cur: proof_segs.append(tuple(cur))

    items, warnings = [], []
    # 2026-08-16 修复：正文归一化每条目重复构建 O(N×条目数)，提到循环外构建一次
    normed_lines = build_normed_lines(lines[body_start:])
    for it in data.get("items", []):
        name = str(it.get("name", "")).strip()
        summary = str(it.get("summary", "")).strip()
        excerpt = str(it.get("excerpt", "")).strip()
        tag = str(it.get("tag", "")).strip()
        # 锚点定位行号（可靠方案）；锚点全 miss 时 n-gram 降级再确认一轮；兼容旧 lines 字段
        anchors = it.get("anchors") or []
        if anchors:
            lines_range = locate_anchors(anchors, normed_lines, body_start, total_lines)
            if not lines_range:
                lines_range = locate_by_ngram(excerpt, normed_lines, body_start, total_lines)
                if lines_range:
                    warnings.append(f"条目「{name}」锚点未命中，n-gram 降级定位")
        else:
            lines_range = check_lines(it.get("lines", []), total_lines)
        if not name or not excerpt or not lines_range:
            warnings.append(f"条目「{name}」字段缺失或锚点未定位到行号")
            continue
        ok, why = check_excerpt(excerpt, lines_range, proof_segs)
        if not ok and why:
            warnings.append(f"条目「{name}」: {why}")
        items.append({"name": name, "summary": summary, "excerpt": excerpt,
                      "tag": tag, "lines": lines_range})

    # 兜底（2026-08-16 拷问 Q3）：校验后仍 0 条目且正文非空 → 强制 1 条「速查清单」。
    # 覆盖两种情况：LLM 判 0 条；LLM 切出条目但锚点无法匹配「每行短词」类正文被校验丢弃
    # （音效清单每行 2-4 字，锚点要求 6-20 字连续短语，必然失败）→ 整篇区间兜底，落实「一视同仁」。
    if not items:
        first_text = ""
        for i, l in enumerate(lines[body_start:], start=body_start):
            t = l.strip()
            if t and not t.startswith("=") and not t.startswith("B站视频"):
                first_text = t
                break
        if first_text:
            fallback_excerpt = "清单/素材类正文索引："
            for i, l in enumerate(lines[body_start:], start=body_start):
                t = l.strip()
                if t and not t.startswith("="):
                    fallback_excerpt += t + "；"
                if len(fallback_excerpt) > 200:
                    break
            # 2026-08-16 修复：条目名用视频标题（meta 标题），不用完整文件名（含作者/日期/BV，超长）
            title = (meta.get("标题") or Path(rel).stem).strip()
            items.append({"name": f"{title} 速查清单",
                          "summary": "素材/清单类正文索引，供按需检索",
                          "excerpt": fallback_excerpt.rstrip("；"),
                          "tag": "速查清单",
                          "lines": [(body_start + 1, total_lines)]})
            warnings.append("0 条目兜底：强制 1 条速查清单（整篇区间）")
            print(f"  ✓ {rel} 0 条目兜底 → 强制 1 条速查清单")

    # 6) 纠错建议返回主线程统一写入术语表，避免并发读-改-写竞态。
    # 2026-08-14 污染治理：必须走 is_acceptable_term（黑名单/形态/白名单）。
    new_terms = []
    for fix in data.get("term_fixes", []):
        fix = str(fix).strip()
        if "→" in fix:
            w, r = [x.strip() for x in fix.split("→", 1)]
            if w and r and w != r and is_acceptable_term(w, r)[0]:
                new_terms.append(f"{w} → {r}")


    # 7) 写索引 md
    meta_lines = "\n".join(f"{k}: {v}" for k, v in meta.items())
    tag_bag = sorted({it["tag"] for it in items if it["tag"]})
    md = [f"---", f"源文件: {rel}", f"产物键: {key}", *[f"{k}: {v}" for k, v in meta.items()],
          f"段落数: {len(proof_segs)}", f"条目数: {len(items)}",
          f"标签: {', '.join(tag_bag)}", f"---", f"", f"# {Path(rel).stem} 索引", f""]
    for idx, it in enumerate(items, 1):
        lines_desc = "、".join(f"原 第{s}-{e}行" for s, e in it["lines"])
        md += [f"## 条目 {idx}：{it['name']}", "",
               f"- **标签**: {it['tag'] or '（无）'}",
               f"- **概要**: {it['summary']}",
               f"- **行号**: {lines_desc}",
               f"- **摘录**: {it['excerpt']}", ""]
    md += ["## 文件级摘要", ""]
    md += [l for l in lines[:60] if "================" not in l]
    idx_path, _ = artifact_paths(key)
    idx_path.write_text("\n".join(md), encoding="utf-8")
    print(f"  ✓ {rel} → {len(items)} 条目 / {len(warnings)} 警告")
    for w in warnings:
        print(f"    ⚠ {w}")
    return len(items), len(warnings), new_terms


def main():
    force = "--force" in sys.argv
    only = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
    ensure_dirs()
    mf = load_manifest()
    scans = scan_sources()
    if only:
        scans = [s for s in scans if s["rel"] == only]
    if "--dry-run" in sys.argv:
        cls, _ = classify_new(mf, scans)
        skipped = __import__("common").scan_skips()
        print(f"预览：新增 {len(cls['new'])} / 变更 {len(cls['changed'])} / 移动 {len(cls['moved'])} / 跳过 {len(cls['skip'])} / 边界跳过 {len(skipped)}")
        for group in ("new", "changed", "moved"):
            for item in cls[group]: print(f"  {group}: {item['rel']}")
        return
    if force:
        todo = scans          # 强制重跑（试点/修表后）
        assign_artifact_keys(todo, mf)
    else:
        cls, mf = classify_new(mf, scans)
        refresh_moved(cls["moved"], mf)
        save_manifest(mf)
        todo = cls["new"] + cls["changed"]
    if not todo:
        if force:
            print("切条目：强制模式，无匹配文件")
        else:
            print(f"切条目：无新文件（新增 {len(cls['new'])} / 跳过 {len(cls['skip'])}）")
        return

    tag_list = load_table(TAG_TABLE, "（词表暂无）")
    term_list = load_table(TERM_TABLE, "（纠错表暂无）")
    if force:
        print(f"切条目：强制重跑 {len(todo)} 个文件")
    else:
        print(f"切条目：{len(todo)} 个文件（新增 {len(cls['new'])} / 变更 {len(cls['changed'])} / 移动 {len(cls['moved'])}）")
        for item in cls["dup_hash"]:
            print(f"  ⚠ 同内容副本跳过：{item['rel']}")
        for bv, names in cls["dup_bv"]:
            print(f"  ⚠ 同 BV 重复（仅提示）：{bv}：{'、'.join(names)}")

    ok, fail = 0, []
    pending_terms = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futs = {pool.submit(process_one, s, tag_list, term_list): s for s in todo}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                n, w, terms = fut.result()
                fut.new_terms = terms
                ok += 1
                # 成功 → 登记台账。
                # 2026-08-16 修复：process_one 内 fix_summary_placeholder 会写回源文件（摘要占位符补全），
                # scan 时的 hash 已失效 → 登记前重算，保证台账 hash == 文件当前 hash（防增量误判重切/体检孤儿）
                s["hash"] = file_hash(s["abs"])
                files = mf.setdefault("files", {})
                rec = files.get(s["hash"], {})
                rec.update({"rel": s["rel"], "name": s["name"], "bv": s["bv"],
                            "artifact_key": s.get("artifact_key") or artifact_key(s["rel"], s["hash"]),
                            "ok": True, "items": n, "time": time.strftime("%Y-%m-%d %H:%M")})
                files[s["hash"]] = rec
                if s["bv"]:
                    # 2026-08-16 修复：append 无去重致 BV16e411e7Zx 重复 7 次 → 幂等追加
                    lst = mf.setdefault("bv_map", {}).setdefault(s["bv"], [])
                    if s["hash"] not in lst:
                        lst.append(s["hash"])
                pending_terms.extend(getattr(fut, "new_terms", []))
                # 每个文件完成即落盘，进程中断时不会丢失已完成台账。
                save_manifest(mf)
            except Exception as e:
                fail.append((s["rel"], str(e)))
                print(f"  ✗ {s['rel']}: {e}")
    if pending_terms and AUTO_TERM:
        existing = set()
        if TERM_TABLE.exists():
            existing = {
                l.strip() for l in TERM_TABLE.read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.startswith(("#", ">", "##")) and "→" in l
            }
        fresh = sorted(set(pending_terms) - existing)
        if fresh:
            TERM_TABLE.parent.mkdir(parents=True, exist_ok=True)
            with open(TERM_TABLE, "a", encoding="utf-8") as f:
                f.write("\n" + "\n".join(fresh) + "\n")
            print(f"  ✓ 术语表新增 {len(fresh)} 条")
    save_manifest(mf)
    print(f"\n切条目完成：成功 {ok} / 失败 {len(fail)}")
    if fail:
        print("失败清单（本次未发布新的向量库；下次运行自动重试）:")
        for rel, e in fail:
            print(f"  - {rel}: {e}")
        sys.exit(2)
    retired = retire_replaced_versions(mf, scan_sources())
    if retired:
        print(f"  ✓ 已退役同路径旧版本 {len(retired)} 个")
    save_manifest(mf)


if __name__ == "__main__":
    main()
