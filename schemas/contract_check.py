#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""contract_check.py — 跨技能文件级契约对账器（零第三方依赖：仅 stdlib + PyYAML）。

它读各处 SKILL.md 里 `<!-- pipeline-contract:begin … end -->` 声明块
（契约版本号 + 字段/列清单 + 规范源角色 + 内核配对），与 schemas/ 目录里的
JSON Schema（draft 2020-12）、schemas/shot-table.md、schemas/twin-kernel.json 对账。
任一侧字段改名、缺失、版本不一致、编号命名空间对不上、H3 规范源没有唯一权威、
两份索引内核同名文件分叉却没登记，即退出码非 0 并打印白话原因。

例外基线（可行动信号，本仓 schemas/contract-exceptions.json）：
上面某些漂移是**人工复核过、有意保留的现状**（例如资产提取的输入接缝真的只吃纯文本）。
永远红的门等于没有门，所以这类断点逐条登记在基线里：每条含稳定身份
`rule + 契约/接缝标识 + 涉及文件集合 (+ subject 判别子，如分镜表列名)`、真实原因 reason、
owner、added 与 review_by。命中基线的漂移**仍然打印**，但单独成节、不计红；
三条反腐蚀规则：
  * 基线之外的**新增漂移** → 退出码 1（与加基线前逐字一致）；
  * **到期**（review_by 早于今天）的例外 → 重新计红，例外清单不许变垃圾桶；
  * **失效**（基线里有、实测已不再发生）的例外 → 计红并提示删掉它。
身份匹配一律精确相等（禁止前缀/子集式模糊匹配），否则一条宽登记会悄悄吞掉新接缝。

用法：
    python contract_check.py                # 在工作区根跑全量对账
    python contract_check.py --root <目录>  # 指定工作区根（含 schemas/）
    python contract_check.py --json <路径>  # 结构化报告落盘（供 CI / 自愈读取）
    python contract_check.py --loose        # 只查两端都在场的接缝，不强制命名空间齐活
    python contract_check.py --exceptions <路径>  # 换一份例外基线（默认 schemas/contract-exceptions.json）

退出码：0=无未登记漂移（含「全部命中在期例外基线」）；1=检出新增/到期/失效漂移（真实断点）；
2=工具错误（文件缺失/声明块或基线解析失败）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

try:
    import yaml  # 本机已装 PyYAML 6.x；除此之外零第三方依赖
except ImportError:  # pragma: no cover
    print("缺少 PyYAML，无法解析 SKILL.md 声明块（本机应已安装 PyYAML 6.0.3）", file=sys.stderr)
    sys.exit(2)

# ---------------------------------------------------------------- 静态配置 --

# 参与对账的技能文件（相对工作区根）
SKILL_FILES = [
    "小说转短剧_v1.5.3/SKILL.md",
    "资产提取_v2.17.0-rc.5工作树/SKILL.md",
    "MiniMaxH3提示词_v7.4.4/SKILL.md",
    # 两份索引库：只为双胞胎内核配对（twin_of）进门，不参与 JSON Schema 接缝
    "知识库转录_v5.4.4/SKILL.md",
    "成人文学索引_v4.8.3/SKILL.md",
]

# H3 提示词写法规范源扫描集：唯一权威 = 官方骨架（不在本工作区，装在技能库里）。
# 两份内化副本（novel-storyboard 的 references/h3-prompt.md、MiniMaxH3提示词 v7.4.2）
# 都不许删（删了运行时就没规范可读了），但必须在契约块里声明 h3_spec_role: derived +
# derived_from 指回这里——改权威骨架时，靠这两条声明找出要同步的副本。
OFFICIAL_H3_SKELETON = "~/.agents/skills/minimax-official-workflows/config/workflows/" \
                       "drama-series/reference/h3-video-prompt.md"
H3_SPEC_SCAN_SET = [
    {
        "path": OFFICIAL_H3_SKELETON,
        "name": "MiniMax 官方 drama-series/reference/h3-video-prompt.md（唯一完整可执行骨架）",
        "h3_spec_role": "authority",
    },
]

# 双胞胎内核漂移登记簿（两份索引库同名 scripts/*.py 的分叉解释都记在这）
TWIN_REGISTRY = "schemas/twin-kernel.json"
TWIN_STATUSES = {"identical", "trunk-ahead", "customized", "pending-rebase"}

# 已复核例外基线（命中即不计红，但仍打印；到期/失效重新计红）
EXCEPTIONS_REGISTRY = "schemas/contract-exceptions.json"
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# 管线接缝：两端命名空间必须同时在场（strict），且 id_prefixes 必须有交集
PIPELINE_EDGES = [
    ("script", "storyboard", "剧本 script.json → 分镜 storyboard.json"),
    ("script", "asset-ledger", "剧本 script.json → 资产总账 asset-ledger.json"),
    ("storyboard", "h3-shot-table", "分镜 storyboard.json → H3 六列分镜表"),
]

BLOCK_RE = re.compile(
    r"<!--\s*pipeline-contract:begin\s*(.*?)\s*pipeline-contract:end\s*-->", re.S
)
SHOT_TABLE_RE = re.compile(
    r"<!--\s*shot-table:begin\s*(.*?)\s*shot-table:end\s*-->", re.S
)
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.S)
VERSION_RE = re.compile(r"^\s*version:\s*[\"']?([\w.\-]+)[\"']?\s*$", re.M)

DRIFT_CODES = {
    "SCHEMA_NOT_FOUND": "契约 schema 文件不存在",
    "VERSION_MISMATCH": "SKILL.md 声明的契约版本与 schema 内嵌版本不一致",
    "SKILL_VERSION_MISMATCH": "声明块登记的技能版本与 SKILL.md frontmatter 不一致（技能升版后契约未复审）",
    "FIELD_MISSING": "技能声明了字段，但 schema 里没有（一侧改名/删字段）",
    "FIELD_UNDECLARED": "schema 必填字段，技能侧没有声明（一侧新增另一侧未跟上）",
    "CONSUMER_EMPTY_CONTRACT": "消费侧未声明任何输入字段（该接缝目前是纯文本/人肉交接，机器契约待引入）",
    "COLUMN_MISMATCH": "六列分镜表列名/路径三方（schema ↔ shot-table.md ↔ SKILL.md）不一致",
    "COLUMN_PATH_MISSING": "分镜表列映射到 schema 中不存在的字段（该列当前无机器字段，需人肉转抄）",
    "ID_NAMESPACE_DISJOINT": "接缝两端编号命名空间完全不相交（编号对不上，只能靠名字人肉匹配）",
    "NAMESPACE_NOT_DECLARED": "管线接缝一端技能未做任何契约声明",
    "H3_SPEC_SOURCE_CONFLICT": "H3 提示词写法存在两个并立的权威源（唯一权威只能有一个）",
    "H3_AUTHORITY_UNDECLARED": "H3 提示词写法有多个事实源，却没有任何一侧声明为唯一权威（h3_spec_role: authority）",
    "H3_DERIVATION_UNDECLARED": "H3 规范的派生副本未用 derived_from 指认唯一权威（改权威时不知道要同步它）",
    "TWIN_UNEXPLAINED_DRIFT": "双胞胎内核同名文件字节不同，却没在 twin-kernel.json 里登记（静默分叉）",
    "TWIN_REGISTRY_STALE": "双胞胎内核登记簿与磁盘现状不符（哈希过期／状态非法／缺分类理由）",
}

# 例外基线自身的问题类别（不是对账判定的漂移，故不进 DRIFT_CODES，免得被拿来当 rule 登记）
EXCEPTION_PROBLEMS = {
    "EXPIRED": "已登记例外的复审到期日已过，未复审即重新计红",
    "STALE": "例外基线条目已失效：实测不再出现该漂移，请从 contract-exceptions.json 删掉它",
    "INVALID": "例外基线条目不合法（缺字段／无 owner／无到期日／规则名写错／重复登记）",
}


# ---------------------------------------------------------------- schema 遍历 --


def _resolve_ref(node, root):
    """解开本地 $ref（#/… 形式），最多 20 跳防环。"""
    seen = 0
    while isinstance(node, dict) and "$ref" in node and seen < 20:
        seen += 1
        cur = root
        ok = True
        for p in node["$ref"].lstrip("#/").split("/"):
            p = p.replace("~1", "/").replace("~0", "~")
            if not isinstance(cur, dict) or p not in cur:
                ok = False
                break
            cur = cur[p]
        if not ok:
            return node
        node = cur
    return node


def _prop_candidates(node, name, root):
    """在 object（含 oneOf/anyOf 分支、合并节点）里收集属性 name 的所有可能定义。"""
    node = _resolve_ref(node, root)
    if not isinstance(node, dict):
        return []
    if "__merged__" in node:
        out = []
        for b in node["__merged__"]:
            out.extend(_prop_candidates(b, name, root))
        return out
    props = node.get("properties")
    if isinstance(props, dict) and name in props:
        return [props[name]]
    for key in ("oneOf", "anyOf"):
        if node.get(key):
            out = []
            for b in node[key]:
                out.extend(_prop_candidates(b, name, root))
            if out:
                return out
    return []


def _merge_nodes(nodes):
    if len(nodes) == 1:
        return nodes[0]
    return {"__merged__": nodes}


def _descend(node, prop_token, root):
    """沿 'name' 或 'name[]' 前进一步；返回 (ok, next_node)。"""
    name = prop_token.removesuffix("[]")
    is_arr = prop_token.endswith("[]")
    candidates = _prop_candidates(node, name, root)
    if not candidates:
        return False, None
    if is_arr:
        items = []
        for c in candidates:
            c = _resolve_ref(c, root)
            it = c.get("items") if isinstance(c, dict) else None
            if it is None:
                return False, None
            items.append(it)
        return True, _merge_nodes(items)
    return True, _merge_nodes(candidates)


def path_exists(root_schema, dotted):
    """'episodes[].scenes[].sceneId' 式路径能否在 schema 里走通。"""
    if not isinstance(dotted, str) or not dotted:
        return False
    cur = root_schema
    for tok in dotted.split("."):
        ok, cur = _descend(cur, tok, root_schema)
        if not ok:
            return False
    return True


def collect_required(root_schema):
    """收集所有必填字段的完整路径（含 oneOf 各分支、嵌套 items）。"""
    out = set()
    seen = set()

    def walk(node, prefix):
        node = _resolve_ref(node, root_schema)
        if not isinstance(node, dict) or id(node) in seen:
            return
        seen.add(id(node))
        branches = node.get("oneOf") or node.get("anyOf") or [node]
        props = {}
        for br in branches:
            br = _resolve_ref(br, root_schema)
            if not isinstance(br, dict):
                continue
            for req in br.get("required") or []:
                out.add(".".join(prefix + [req]))
            for k, v in (br.get("properties") or {}).items():
                props.setdefault(k, []).append(v)
        it = node.get("items")  # 数组：前缀已带 []，直接下钻元素
        if it is not None:
            walk(it, prefix)
        for pname, pnodes in props.items():
            pn = _merge_nodes(pnodes)
            pnr = _resolve_ref(pn, root_schema)
            if not isinstance(pnr, dict):
                continue
            is_arr = pnr.get("type") == "array" or "items" in pnr
            if pnr.get("properties") or pnr.get("items") or pnr.get("oneOf") or pnr.get("anyOf"):
                walk(pn, prefix + [pname + ("[]" if is_arr else "")])

    walk(root_schema, [])
    return out


def parse_frontmatter_version(md_text):
    m = FRONTMATTER_RE.search(md_text)
    if not m:
        return None
    v = VERSION_RE.search(m.group(1))
    return v.group(1) if v else None


# ---------------------------------------------------------- 路径与哈希工具 --


def _expand(ref):
    return os.path.expandvars(os.path.expanduser(str(ref or "").strip().strip("\"'")))


def _canon_ref(ref, root):
    """把 derived_from / 登记簿路径归一成一个可比较的字符串（绝对化 + 真实路径 + 忽略大小写）。"""
    s = _expand(ref).replace("\\", "/")
    if not s:
        return ""
    p = Path(s)
    if not p.is_absolute():
        p = Path(_expand(root)) / p
    try:
        return p.resolve().as_posix().casefold()
    except OSError:  # pragma: no cover - 非法路径/符号环
        return p.as_posix().casefold()


def _sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ------------------------------------------------------------ H3 规范源对账 --


def _h3_authorities(root, contracts, h3_scan_set, drift):
    """权威身份清单：扫描集登记的官方骨架文件 + 契约块里自称 authority 的一侧。"""
    found = {}
    for item in h3_scan_set or []:
        if not isinstance(item, dict):
            item = {"path": item}
        if str(item.get("h3_spec_role") or "authority") != "authority":
            continue
        raw = str(item.get("path") or "").strip()
        if not raw:
            continue
        p = Path(_expand(raw).replace("\\", "/"))
        if not p.is_absolute():
            p = Path(_expand(root)) / p
        if not p.is_file():
            drift("H3_AUTHORITY_UNDECLARED", raw, "h3-prompt",
                  f"扫描集登记的 H3 唯一权威骨架在磁盘上不存在：{raw}——"
                  f"权威源失踪（换机/改名？），派生副本失去对齐目标，请同步 H3_SPEC_SCAN_SET")
            continue
        ref = _canon_ref(raw, root)
        found[ref] = {"ref": ref, "label": str(item.get("name") or raw)}
    for c in contracts:
        if str(c.get("h3_spec_role") or "").strip() == "authority":
            ref = _canon_ref(c["_file"], root)
            found.setdefault(ref, {"ref": ref, "label": str(c.get("h3_spec_source") or c["_file"]),
                                   "source": str(c.get("h3_spec_source") or "")})
    return list(found.values())


def _check_h3_spec_sources(root, contracts, h3_scan_set, drift):
    """C6：H3 提示词写法只允许一个权威源；其余副本必须显式声明派生并指回权威。"""
    specs = [c for c in contracts if c.get("h3_spec_source")]
    if not specs:
        return
    sources = {}
    for c in specs:
        sources.setdefault(str(c["h3_spec_source"]), []).append(c)
    authorities = _h3_authorities(root, contracts, h3_scan_set, drift)

    if len(authorities) > 1:
        drift("H3_SPEC_SOURCE_CONFLICT", "（跨文件）", "h3-prompt",
              f"H3 提示词写法登记了 {len(authorities)} 个权威源："
              f"{'；'.join(a['label'] for a in authorities)}——唯一权威只能有一个，"
              f"其余一侧必须改写 h3_spec_role: derived + derived_from")
        return

    multi = len(sources) > 1
    if not authorities:
        if multi:
            parts = "；".join(f"{src} ← {', '.join(c['_file'] + '#' + str(c.get('id')) for c in cs)}"
                              for src, cs in sorted(sources.items()))
            drift("H3_AUTHORITY_UNDECLARED", "（跨文件）", "h3-prompt",
                  f"H3 提示词写法有 {len(sources)} 个并行事实源：{parts}——"
                  f"没有任何一侧声明 h3_spec_role: authority，两份规范各改各的必然漂移")
        return

    auth_refs = {a["ref"] for a in authorities}
    auth_names = {a["source"].strip().casefold() for a in authorities if a.get("source")}
    auth_label = authorities[0]["label"]
    for c in specs:
        role = str(c.get("h3_spec_role") or "").strip()
        if role == "authority":
            continue
        if not multi and role != "derived":
            continue  # 只有一个事实源、也没自认派生：没有派生关系可判
        df = str(c.get("derived_from") or "").strip()
        if df and (_canon_ref(df, root) in auth_refs or df.strip().casefold() in auth_names):
            continue
        drift("H3_DERIVATION_UNDECLARED", c["_file"], str(c.get("id", "?")),
              f"该技能登记了 H3 规范源「{str(c['h3_spec_source'])[:60]}」，唯一权威是「{auth_label}」，"
              f"但{'derived_from 指向的不是权威：' + df if df else '未声明 derived_from'}"
              f"——权威骨架变更后没人知道要同步这一份副本")


# ---------------------------------------------------------- 双胞胎内核对账 --


def _check_twin_kernel(root, contracts, registry_rel, drift):
    """C5：两份索引库共用同名 scripts/*.py，分叉必须在登记簿里留下解释。

    只处理契约块里用 twin_of 显式声明过配对的仓库对——不凭目录名猜双胞胎。
    返回工具错误列表（登记簿本身读不动/配对扫不到文件等）。
    """
    declared = {}
    for c in contracts:
        twin = str(c.get("twin_of") or "").strip().replace("\\", "/").strip("/")
        own = str(c["_file"]).replace("\\", "/").split("/")[0]
        if not twin or twin == own:
            continue
        declared.setdefault(frozenset({own, twin}), []).append(c)
    if not declared:
        return []

    tool_errors = []
    root = Path(root)
    pairs = []
    rpath = root / registry_rel
    if rpath.is_file():
        try:
            pairs = json.loads(rpath.read_text(encoding="utf-8")).get("pairs") or []
        except Exception as exc:  # noqa: BLE001
            tool_errors.append(f"双胞胎内核登记簿 {registry_rel} 解析失败：{exc}")
            return tool_errors
    else:
        tool_errors.append(f"未找到双胞胎内核登记簿：{registry_rel}——"
                           f"已声明 twin_of 的内核配对无处登记分叉解释")

    for pair, blocks in sorted(declared.items(), key=lambda kv: sorted(kv[0])):
        names = set(pair)
        entry = next((p for p in pairs
                      if {str(p.get("trunk") or ""), str(p.get("customization") or "")} == names), None)
        who = " ↔ ".join(sorted(names))
        where = ", ".join(f"{b['_file']}#{b.get('id', '?')}" for b in blocks)
        trunk = str((entry or {}).get("trunk") or sorted(names)[0])
        custom = str((entry or {}).get("customization") or sorted(names)[-1])
        tdir = root / trunk / str((entry or {}).get("trunk_scripts") or "scripts")
        cdir = root / custom / str((entry or {}).get("customization_scripts") or "scripts")
        if not tdir.is_dir() or not cdir.is_dir():
            tool_errors.append(f"twin_of 配对 {who} 的脚本目录不存在：{tdir} / {cdir}")
            continue
        if entry is not None:  # 登记簿在位时，才核对各仓自报的 trunk/customization 角色
            for b in blocks:
                own = str(b["_file"]).replace("\\", "/").split("/")[0]
                role = str(b.get("twin_role") or "").strip()
                expected = "trunk" if own == trunk else "customization"
                if role and role != expected:
                    drift("TWIN_REGISTRY_STALE", b["_file"], str(b.get("id", "?")),
                          f"{who} 的配对里 {b['_file']} 自报 twin_role: {role}，"
                          f"登记簿认定它是 {expected}——谁在说谎要查清（主干只应有一个）")
        recorded = {str(f.get("file")): f for f in (entry or {}).get("files", []) or []}
        same_named = sorted({p.name for p in tdir.glob("*.py") if p.is_file()} &
                            {p.name for p in cdir.glob("*.py") if p.is_file()})

        for name in same_named:
            tp, cp = tdir / name, cdir / name
            th, ch = _sha_file(tp), _sha_file(cp)
            if th == ch or name in recorded:
                continue
            drift("TWIN_UNEXPLAINED_DRIFT", f"{trunk}/scripts/{name} ↔ {custom}/scripts/{name}",
                  "twin-kernel",
                  f"{name} 两份内核同名文件已字节分叉（主干 {th[:12]}… {tp.stat().st_size}B / "
                  f"定制侧 {ch[:12]}… {cp.stat().st_size}B），{registry_rel} 里却没有它的条目——"
                  f"静默分叉：要么回填代码，要么登记 status + reason"
                  f"{'' if entry else '（该配对的整条登记都缺，配对由 ' + where + ' 声明）'}")

        for name in sorted(recorded):
            reg = recorded[name]
            tp, cp = tdir / name, cdir / name
            th = _sha_file(tp) if tp.is_file() else None
            ch = _sha_file(cp) if cp.is_file() else None
            ev = reg.get("evidence") or {}
            status = str(reg.get("status") or "")
            problems = []
            if status not in TWIN_STATUSES:
                problems.append(f"status「{status or '(空)'}」不在 {sorted(TWIN_STATUSES)} 之内")
            if not str(reg.get("reason") or "").strip():
                problems.append("缺 reason（分叉理由/差异性质没写，等于没解释）")
            for side in ("trunk_sha256", "customization_sha256"):
                digest = str(ev.get(side) or "")
                if digest not in {th, ch}:
                    problems.append(f"{side}={digest[:12] or '(空)'}… 与磁盘两侧现状都不符"
                                    f"（主干 {str(th)[:12]}… / 定制侧 {str(ch)[:12]}…）")
            if status == "identical" and th != ch:
                problems.append("登记为 identical，实测两侧已分叉")
            if problems:
                drift("TWIN_REGISTRY_STALE", f"{trunk}/scripts/{name} ↔ {custom}/scripts/{name}",
                      "twin-kernel",
                      f"登记条目已过期：{name} —— {'；'.join(problems)}。"
                      f"改了一侧代码就要重算哈希、复核 status/reason，或明确改成 pending-rebase")
    return tool_errors


# ------------------------------------------------------- 已复核例外基线 --


def _norm_files(files):
    """涉及文件集合归一：反斜杠→正斜杠、去空白、去重、排序（身份与书写顺序无关）。"""
    out = []
    for f in files or []:
        s = str(f).replace("\\", "/").strip()
        if s and s not in out:
            out.append(s)
    return sorted(out)


def exception_key(rule, contract, files, subject=""):
    """例外的稳定身份：rule + 契约/接缝标识 + 涉及文件集合（+ subject 判别子，如分镜表列名）。

    拼成单一字符串后只做**精确相等**比较：不做前缀、不做子集、不做模糊匹配——
    一条宽登记会悄悄吞掉以后长出来的新接缝，那就又是假门。
    """
    return "|".join([str(rule or "").strip(), str(contract or "").strip(),
                     ";".join(_norm_files(files)), str(subject or "").strip()])


def _split_files(csv):
    """逗号拼接的文件串 -> 去空列表。登记侧与漂移侧必须同一套切法，否则键对不上。"""
    return [x for x in str(csv or "").split(",") if x.strip()]


def drift_key(d):
    """漂移侧身份：ID_NAMESPACE_DISJOINT 的 file 是逗号拼接的多文件串，按同一规则归一。"""
    return exception_key(d.get("code"), d.get("contract"),
                         _split_files(d.get("file")), d.get("subject"))


def entry_key(e):
    files = e.get("files") or _split_files(e.get("file"))
    return exception_key(e.get("rule"), e.get("contract"), files, e.get("subject"))


def entry_problems(e):
    """基线条目自检：身份不清／没人认领／永不到期的登记不具备「免红」资格。"""
    problems = []
    if not _norm_files(e.get("files") or ([e["file"]] if e.get("file") else [])):
        problems.append("files 为空（不知道这条例外涉及哪些文件，等于允许任意同类漂移）")
    if not str(e.get("contract") or "").strip():
        problems.append("contract 缺失（没有契约/接缝标识，身份无法精确匹配）")
    if not str(e.get("rule") or "").strip():
        problems.append("rule 缺失")
    elif str(e["rule"]).strip() not in DRIFT_CODES:
        problems.append(f"rule「{e['rule']}」不是已知漂移码（永远匹配不上，只会假装成例外）")
    if not str(e.get("owner") or "").strip():
        problems.append("owner 缺失（没有责任人的例外不会有人复审）")
    if not str(e.get("reason") or "").strip():
        problems.append("reason 缺失（写不出真实原因就不该登记为例外）")
    elif len(str(e["reason"]).strip()) < 12:
        problems.append("reason 过短（「待办」「暂时不管」这类空话不算复核结论）")
    for k in ("added", "review_by"):
        v = str(e.get(k) or "").strip()
        if not v:
            problems.append(f"{k} 缺失")
        elif not ISO_DATE_RE.match(v):
            problems.append(f"{k}「{v}」不是 YYYY-MM-DD（无法判断是否到期）")
    return problems


def load_exceptions(path):
    """读例外基线，返回 (条目列表, 工具错误)。文件不存在＝没有基线（漂移照旧全计红）。"""
    p = Path(path)
    if not p.is_file():
        return [], []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [], [f"例外基线 {p} 解析失败：{exc}——宁可报工具错误，"
                    f"也不当作「没有例外」把已知断点重新算成新漂移"]
    entries = data.get("exceptions") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return [], [f"例外基线 {p} 的 exceptions 不是数组（缺该键＝基线未落位）"]
    return [e for e in entries if isinstance(e, dict)], []


def review_drifts(drifts, entries, today=None):
    """把漂移分成「已登记例外 / 计红漂移」，并挑出到期、失效、非法的例外条目。纯函数，好单测。"""
    today = str(today or date.today().isoformat())
    valid, invalid = {}, []
    for e in entries or []:
        problems = entry_problems(e)
        if problems:
            invalid.append({"entry": e, "problems": problems})
            continue
        valid.setdefault(entry_key(e), []).append(e)
    for k in [k for k, g in valid.items() if len(g) > 1]:
        invalid.extend({"entry": e, "problems": ["同一身份重复登记，请合并成一条"]}
                       for e in valid.pop(k))

    hit, failing, excepted, expired = set(), [], [], []
    for d in drifts or []:
        k = drift_key(d)
        if k not in valid:
            failing.append(d)
            continue
        hit.add(k)
        entry = valid[k][0]
        (expired if str(entry["review_by"]) < today else excepted).append(
            {"drift": d, "entry": entry, "key": k})
    stale = [e for k, g in valid.items() if k not in hit for e in g]
    # 只报在期例外的最早复审日：到期/失效的那几条已经各自计红、另有提示
    deadlines = sorted({x["entry"]["review_by"] for x in excepted})
    counts = {"failing": len(failing), "excepted": len(excepted), "expired": len(expired),
              "stale": len(stale), "invalid": len(invalid)}
    return {"today": today, "failing": failing, "excepted": excepted, "expired": expired,
            "stale": stale, "invalid": invalid, "counts": counts,
            "review_deadline": deadlines[0] if deadlines else None,
            "clean": not (failing or expired or stale or invalid)}


# ---------------------------------------------------------------- 核心对账 --


def run_check(root, strict=True, skill_files=None, h3_scan_set=None, twin_registry=None,
              exceptions=None, exceptions_registry=None, today=None):
    """返回 (exit_code, drifts, summary)。drifts 为可打印白话 dict 列表（含已登记例外）。

    exit_code 只看未登记/到期/失效的漂移与非法基线；summary["review"] 是例外基线的分类明细。
    """
    root = Path(root)
    skill_files = list(skill_files) if skill_files else SKILL_FILES
    h3_scan_set = H3_SPEC_SCAN_SET if h3_scan_set is None else h3_scan_set
    twin_registry = twin_registry or TWIN_REGISTRY
    drifts = []
    tool_errors = []

    def drift(code, file, contract, msg, subject=None):
        rec = {"code": code, "file": file, "contract": contract,
               "reason": DRIFT_CODES[code], "message": msg}
        if subject:  # 同一契约下可逐条区分的判别子（如分镜表列名），基线靠它精确登记
            rec["subject"] = str(subject)
        drifts.append(rec)

    schemas_dir = root / "schemas"
    if not schemas_dir.is_dir():
        return 2, [], {"error": f"未找到 schemas 目录：{schemas_dir}"}

    shot_table_cols = None
    st_path = schemas_dir / "shot-table.md"
    if st_path.is_file():
        m = SHOT_TABLE_RE.search(st_path.read_text(encoding="utf-8"))
        if m:
            try:
                shot_table_cols = yaml.safe_load(m.group(1)).get("columns")
            except Exception as exc:  # noqa: BLE001
                tool_errors.append(f"schemas/shot-table.md 机器块解析失败：{exc}")

    contracts = []
    for rel in skill_files:
        fp = root / rel
        if not fp.is_file():
            tool_errors.append(f"技能文件不存在：{rel}")
            continue
        text = fp.read_text(encoding="utf-8", errors="replace")
        fm_version = parse_frontmatter_version(text)
        for m in BLOCK_RE.finditer(text):
            try:
                block = yaml.safe_load(m.group(1))
            except Exception as exc:  # noqa: BLE001
                tool_errors.append(f"{rel} 声明块 YAML 解析失败：{exc}")
                continue
            if not isinstance(block, dict):
                tool_errors.append(f"{rel} 声明块不是字段清单结构")
                continue
            block["_file"] = rel
            block["_skill_version"] = fm_version
            contracts.append(block)

    if not contracts:
        tool_errors.append(f"{len(skill_files)} 处 SKILL.md 均未找到 pipeline-contract 声明块"
                           f"——契约指针未落位")

    ids = [c.get("id") for c in contracts]
    if len(ids) != len(set(ids)):
        dup = sorted({str(i) for i in ids if ids.count(i) > 1})
        tool_errors.append(f"契约声明块 id 重复：{dup}")

    by_namespace = {}
    for c in contracts:
        if c.get("namespace"):
            by_namespace.setdefault(c["namespace"], []).append(c)

    for c in contracts:
        f = c["_file"]
        cid = c.get("id", "?")
        schema_rel = c.get("schema", "")
        spath = (root / schema_rel) if schema_rel else None
        if not spath or not spath.is_file():
            drift("SCHEMA_NOT_FOUND", f, cid, f"声明指向 {schema_rel or '(空)'}，文件不存在")
            continue
        schema = json.loads(spath.read_text(encoding="utf-8"))
        xc = schema.get("x-contract", {})
        if str(c.get("version")) != str(xc.get("version")):
            drift("VERSION_MISMATCH", f, cid,
                  f"{f} 声明契约版本 {c.get('version')}，{schema_rel} 内嵌版本 "
                  f"{xc.get('version')}——改了一侧没改另一侧")
        if str(c.get("skill_version")) != str(c["_skill_version"]):
            drift("SKILL_VERSION_MISMATCH", f, cid,
                  f"声明块登记技能版本 {c.get('skill_version')}，SKILL.md frontmatter 实测 "
                  f"{c['_skill_version']}——技能升版后请复审契约指针")

        fields = c.get("fields")
        if fields is not None:
            if c.get("role") == "consumer" and not fields:
                drift("CONSUMER_EMPTY_CONTRACT", f, cid,
                      f"消费侧只按纯文本吃上游文件，未声明任何 {schema_rel} 字段——"
                      f"该接缝当前没有机器契约，靠人肉交接")
            for dotted in fields:
                if not path_exists(schema, dotted):
                    drift("FIELD_MISSING", f, cid,
                          f"技能声明字段「{dotted}」，{schema_rel} 里不存在——一侧改名/删字段")
            if c.get("role") == "producer":
                for dotted in sorted(collect_required(schema) - set(fields)):
                    drift("FIELD_UNDECLARED", f, cid,
                          f"{schema_rel} 必填字段「{dotted}」，技能侧字段清单未声明——"
                          f"schema 新增另一侧未跟上")

        cols = c.get("columns")
        if cols is not None:
            schema_cols = (schema.get("x-shot-table") or {}).get("columns") or []

            def norm(lst):
                return [(x.get("column"), x.get("path")) for x in lst]

            if norm(cols) != norm(schema_cols):
                drift("COLUMN_MISMATCH", f, cid,
                      f"{f} 声明的列清单与 {schema_rel} 的 x-shot-table 不一致")
            if shot_table_cols is not None and norm(cols) != norm(shot_table_cols):
                drift("COLUMN_MISMATCH", f, cid,
                      f"{f} 声明的列清单与 schemas/shot-table.md 表头机器块不一致")
            derive = {x.get("column"): x.get("derive", "") for x in schema_cols}
            for col in cols:
                if not path_exists(schema, str(col.get("path", ""))):
                    note = derive.get(col.get("column"), "")[:60]
                    drift("COLUMN_PATH_MISSING", f, cid,
                          f"列「{col.get('column')}」映射 {col.get('path')}，"
                          f"{schema_rel} 里没有该字段——此列现在靠人肉转抄"
                          f"（契约登记的推导规则：{note}）",
                          subject=col.get("column"))

    def union_prefixes(items):
        pa = set()
        for x in items:
            pa |= set(x.get("id_prefixes") or [])
        return pa

    for ns_a, ns_b, label in PIPELINE_EDGES:
        a_list, b_list = by_namespace.get(ns_a, []), by_namespace.get(ns_b, [])
        if not a_list or not b_list:
            if strict:
                miss = ns_a if not a_list else ns_b
                drift("NAMESPACE_NOT_DECLARED", "-", "-",
                      f"接缝「{label}」一端（命名空间 {miss}）没有任何契约声明")
            continue
        pa, pb = union_prefixes(a_list), union_prefixes(b_list)
        if pa and pb and not (pa & pb):
            files = ",".join(x["_file"] for x in a_list + b_list)
            drift("ID_NAMESPACE_DISJOINT", files, f"{ns_a}↔{ns_b}",
                  f"接缝「{label}」两端编号前缀 {sorted(pa)} 与 {sorted(pb)} 完全不相交——"
                  f"跨文件引用只能靠资产名人肉匹配")

    _check_h3_spec_sources(root, contracts, h3_scan_set, drift)
    tool_errors.extend(_check_twin_kernel(root, contracts, twin_registry, drift))

    # 已复核例外基线：命中即不计红（但仍打印），到期/失效/非法则重新计红
    if exceptions is None:
        reg_rel = exceptions_registry or EXCEPTIONS_REGISTRY
        reg_path = Path(root) / reg_rel
        entries, errs = load_exceptions(reg_path)
        tool_errors.extend(errs)
        reg_used = reg_path.as_posix()
    else:
        entries = list(exceptions)
        reg_used = str(exceptions_registry or EXCEPTIONS_REGISTRY)
    review = review_drifts(drifts, entries, today=today)
    review["file"] = reg_used

    summary = {"contracts": len(contracts), "drifts": len(drifts),
               **review["counts"], "review": review}
    if tool_errors:
        for e in tool_errors:
            print(f"[工具错误] {e}", file=sys.stderr)
        return 2, drifts, summary

    return (0 if review["clean"] else 1), drifts, summary


def _label(d):
    """可读物标识：带 subject（如分镜表列名）的漂移要一起标出，否则三条同契约漂移看着一样。"""
    return (f"{d['file']} · {d['contract']}"
            + (f" · {d['subject']}" if d.get("subject") else ""))


def _pair_public(x):
    d = x["drift"]
    return {"key": x["key"], "code": d["code"], "file": d["file"], "contract": d["contract"],
            "subject": d.get("subject"), "message": d["message"], "entry": x["entry"]}


def _entry_public(x, key):
    return {"key": key, "entry": x}


def print_report(code, summary, drifts):
    """打印对账报告：新增漂移节（逐字沿用加基线前的格式）+ 基线待处置节 + 已登记例外（不计红）节。"""
    review = summary.get("review") or {}
    failing = review.get("failing", drifts)
    expired, stale = review.get("expired") or [], review.get("stale") or []
    invalid, excepted = review.get("invalid") or [], review.get("excepted") or []
    today = review.get("today") or date.today().isoformat()

    if failing:
        print(f"检出 {len(failing)} 条跨技能契约漂移：\n")
        for i, d in enumerate(failing, 1):
            print(f"【漂移{i}】[{d['code']}] {_label(d)}")
            print(f"    {d['message']}\n")
        print("以上漂移即 PIPELINE.md 登记的断点，修复一侧时必须同步另一侧或更新 schemas/。")
    pending = len(expired) + len(stale) + len(invalid)
    if pending and not failing:
        print(f"实测漂移已全部登记为例外，但例外基线本身有 {pending} 条需处置（一并计红）：\n")

    for i, x in enumerate(expired, 1):
        d, e = x["drift"], x["entry"]
        if i == 1:
            print(f"\n——— 例外已到期（重新计红）：{len(expired)} 条 ———")
        print(f"【到期例外{i}】[{d['code']}] {_label(d)}")
        print(f"    实测：{d['message']}")
        print(f"    这条例外的复审到期日 {e.get('review_by')} 已过（今天 {today}），owner "
              f"{e.get('owner')}：现状仍然成立就复核后把 review_by 往后推，"
              f"断点已修就删掉条目——到期例外不算绿。\n")

    for i, e in enumerate(stale, 1):
        if i == 1:
            print(f"——— 例外已失效（计红，请删掉它）：{len(stale)} 条 ———")
        print(f"【失效例外{i}】[{e.get('rule')}] "
              f"{'、'.join(_norm_files(e.get('files') or []))} · {e.get('contract')}"
              + (f" · {e['subject']}" if e.get("subject") else ""))
        print(f"    例外已失效，请删掉它：基线登记了这条，实测已不再出现该漂移"
              f"（断点修好了？判定规则变了？）。留着它，下次同类漂移会静默漏网。"
              f"原登记理由：{e.get('reason')}\n")

    for i, x in enumerate(invalid, 1):
        e = x["entry"]
        if i == 1:
            print(f"——— 例外基线条目非法（计红）：{len(invalid)} 条 ———")
        ident = json.dumps({k: e.get(k) for k in ("rule", "contract", "files", "subject")},
                           ensure_ascii=False)
        print(f"【非法条目{i}】{ident}")
        print(f"    {'；'.join(x['problems'])}——不具备免红资格，"
              f"它本想盖住的漂移一律按新增计红\n")

    if excepted:
        print(f"——— 已登记例外（不计红）：{len(excepted)} 条 ———")
        print(f"来源 {review.get('file')}；最早复审到期 "
              f"{review.get('review_deadline') or '未登记'}，到期未复审会重新计红，请提前复核。")
        for i, x in enumerate(excepted, 1):
            d, e = x["drift"], x["entry"]
            print(f"【例外{i}】[{d['code']}] {_label(d)}")
            print(f"    实测漂移：{d['message']}")
            print(f"    例外原因：{e.get('reason')}")
            print(f"    owner：{e.get('owner')}　登记 {e.get('added')}　"
                  f"复审到期 {e.get('review_by')}\n")

    if code == 0:
        if excepted:
            print(f"✓ 除上述 {len(excepted)} 条已登记例外外无新增漂移"
                  f"（SKILL.md 声明 ↔ schemas/ schema ↔ shot-table.md 三方一致）")
        else:
            print("✓ 全部契约对齐（SKILL.md 声明 ↔ schemas/ schema ↔ shot-table.md 三方一致）")


def main(argv=None):
    ap = argparse.ArgumentParser(description="跨技能文件级契约对账器")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                    help="工作区根（含 schemas/ 目录），默认取本脚本上级目录")
    ap.add_argument("--json", dest="json_out", default=None, help="结构化报告落盘路径")
    ap.add_argument("--loose", action="store_true", help="接缝一端缺席时不报漂移")
    ap.add_argument("--exceptions", dest="exceptions", default=None,
                    help=f"已复核例外基线路径，默认 {EXCEPTIONS_REGISTRY}（相对工作区根或绝对路径）")
    args = ap.parse_args(argv)

    code, drifts, summary = run_check(Path(args.root), strict=not args.loose,
                                       exceptions_registry=args.exceptions)
    print_report(code, summary, drifts)
    if args.json_out:
        review = summary.get("review") or {}
        payload = {"summary": {k: v for k, v in summary.items() if k != "review"},
                   "drifts": drifts}
        if review:
            payload["exceptions"] = {
                "file": review.get("file"), "today": review.get("today"),
                "counts": review["counts"], "clean": review["clean"],
                "problem_legend": EXCEPTION_PROBLEMS,
                "excepted": [_pair_public(x) for x in review["excepted"]],
                "expired": [_pair_public(x) for x in review["expired"]],
                "stale": [_entry_public(e, entry_key(e)) for e in review["stale"]],
                "invalid": [_entry_public(x["entry"], entry_key(x["entry"]))
                            | {"problems": x["problems"]} for x in review["invalid"]],
            }
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return code


if __name__ == "__main__":
    # 同 ledger.py：本文件被各仓当 CLI 调用，中文报告不能被调用方 shell 编码改写
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
