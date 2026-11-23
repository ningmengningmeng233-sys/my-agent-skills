#!/usr/bin/env python3
"""人工十项画质表闸门（工单 S5 / Task D2）：机器签发不等于内容正确。

本脚本只做三件事：
1. 结构校验——十项必须齐全且有序、判定取值合法、每条「通过」必须有真人评审人 + 日期 +
   真实存在的产物路径（缺任何一项就是假绿灯）；
2. 升版闸门——只要有一项未签或判「不通过」，`SKILL.md` 的版本号就不许去掉 `-rc`；
   真实两图冒烟没有 PASS 同样不许；验收文档里也不许正面宣称「正式版已完成」；
3. 冒烟复核——`smoke.真实API两图冒烟` 若为 BLOCKED-CRED，必须写明原因、解锁条件、复核命令；
   若声称 PASS，必须有日期和真实存在的产物。

本机现状：无任何出图密钥（六节点池 key 全为字面量 REPLACE）⇒ 冒烟只能是 BLOCKED-CRED，
十项全为「待人工」⇒ 闸门自洽（退出码 0）并明确打印「拒绝出正式版」。

退出码：0 当前口径自洽（rc 状态 + 全表待人工即为自洽）/ 2 出现假绿灯或口径不一致。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# 闸门结论要进总门与其它脚本的 stdout（按 UTF-8 解码），因此本脚本的输出必须是 UTF-8，
# 不能跟随调用方的 GBK 代码页——否则中文结论会变成乱码，读的人以为「没打印拒绝原因」。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
TABLE = REPO / "docs" / "验收" / "人工画质表.json"
VERDICTS = ("待人工", "通过", "不通过")
EXPECTED_IDS = ("style", "identity", "age", "composition", "crop",
                "ratio", "material", "lighting", "forbidden", "series")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BANNED_CLAIM = "正式版已完成"
NEGATION_WORDS = ("禁止", "不得", "不可", "勿")
SMOKE_STATUSES = ("BLOCKED-CRED", "NOT_RUN", "PASS")
SMOKE_KEY = "真实API两图冒烟"


def skill_version() -> str:
    """读 SKILL.md frontmatter 里的技能版本（读不到返回空串，由调用方判红）。"""
    text = (REPO / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r'^\s{2}version:\s*"?([\w.\-]+)"?\s*$', text, re.M)
    return m.group(1) if m else ""


def _release_base(version: str) -> str:
    """`2.17.0-rc.2` 与 `2.17.0` 属同一条待发布线；`2.18.0` 则是另一条。"""
    return re.split(r"-rc", version.strip())[0].strip()


def _missing_path(raw: object) -> bool:
    """空串与不存在的路径都算「没有证据」——空串拼上仓库根会指向目录本身。"""
    rel = str(raw).strip()
    return not rel or not (REPO / rel).exists()


def validate(table: dict, version: str) -> list[str]:
    """返回全部违规原因；空列表 == 当前口径自洽（不等于「可以发正式版」）。"""
    reasons: list[str] = []
    if table.get("schema") != "asset-extract-manual-qc/1":
        reasons.append(f"画质表 schema 不受支持: {table.get('schema')!r}")
    if "机器签发不等于内容正确" not in str(table.get("policy", "")):
        reasons.append("policy 必须写明「机器签发不等于内容正确」")
    items = table.get("items") or []
    ids = tuple(i.get("id") for i in items)
    if ids != EXPECTED_IDS:
        reasons.append(f"十项必须齐全且有序，实际为 {ids}")
    for i, item in enumerate(items):
        v = item.get("判定")
        if v not in VERDICTS:
            reasons.append(f"items[{i}]({item.get('id')}) 判定非法: {v!r}"
                           f"（只能是 {'/'.join(VERDICTS)}）")
            continue
        if v == "通过":
            if not str(table.get("reviewer", "")).strip():
                reasons.append(f"items[{i}]({item.get('id')}) 声称通过但没有真人评审人"
                               "（reviewer 为空）")
            if not DATE_RE.match(str(table.get("review_date", ""))):
                reasons.append(f"items[{i}]({item.get('id')}) 声称通过但没有合规评审日期 "
                               "review_date（要 YYYY-MM-DD）")
            paths = item.get("证据路径") or []
            if not paths:
                reasons.append(f"items[{i}]({item.get('id')}) 声称通过但没有产物路径")
            for p in paths:
                if _missing_path(p):
                    reasons.append(f"items[{i}]({item.get('id')}) 产物路径不存在或为空: {p!r}")
        if v == "不通过" and not str(item.get("备注", "")).strip():
            reasons.append(f"items[{i}]({item.get('id')}) 判「不通过」必须写备注（问题出在哪）")

    smoke = (table.get("smoke") or {}).get(SMOKE_KEY) or {}
    smoke_status = str(smoke.get("status", "")).strip()
    if not smoke:
        reasons.append(f"smoke.{SMOKE_KEY} 缺失：必须登记状态（哪怕是 BLOCKED-CRED）")
    elif smoke_status not in SMOKE_STATUSES:
        reasons.append(f"冒烟状态非法: {smoke_status!r}（只能是 {'/'.join(SMOKE_STATUSES)}）")
    if smoke_status == "BLOCKED-CRED":
        for f in ("reason", "unlock_condition", "recheck_command"):
            if not str(smoke.get(f, "")).strip():
                reasons.append(f"冒烟记录 BLOCKED-CRED 必须写 {f}")
    elif smoke_status == "NOT_RUN":
        if not str(smoke.get("reason", "")).strip():
            reasons.append("冒烟记录 NOT_RUN 必须写 reason（为什么没跑）")
    elif smoke_status == "PASS":
        if not DATE_RE.match(str(smoke.get("date", ""))):
            reasons.append("冒烟声称 PASS 但没有日期")
        artifact_paths = smoke.get("artifact_paths") or []
        if not artifact_paths:
            reasons.append("冒烟声称 PASS 但没有产物路径")
        for p in artifact_paths:
            if _missing_path(p):
                reasons.append(f"冒烟产物路径不存在或为空: {p!r}")

    reviewed = str(table.get("version_under_review", "")).strip()
    if not reviewed:
        reasons.append("画质表必须写明 version_under_review（这张表审的是哪个版本）")
    elif version and _release_base(reviewed) != _release_base(version):
        reasons.append(f"画质表审的是 {reviewed}，当前版本 {version} 不是同一版——"
                       "换版本必须以新版本重签")

    unsigned = [i.get("id") for i in items if i.get("判定") != "通过"]
    if unsigned and "-rc" not in version:
        reasons.append(f"十项未全签（缺 {len(unsigned)} 项）却已是正式版号 {version}"
                       "——升版条件未满足")
    if "-rc" not in version and smoke_status != "PASS":
        reasons.append(f"正式版号 {version} 还要求真实两图冒烟通过，"
                       f"当前冒烟状态 {smoke_status or '未登记'}——升版条件未满足")

    for doc in sorted((REPO / "docs" / "验收").glob("*.md")):
        for line in doc.read_text(encoding="utf-8", errors="replace").splitlines():
            # 「禁止写『正式版已完成』」这类反面表述是合规的，只有正面宣称才算违规
            if BANNED_CLAIM in line and not any(w in line for w in NEGATION_WORDS):
                reasons.append(f"{doc.relative_to(REPO)} 出现禁用口径「{BANNED_CLAIM}」")
    return reasons


def _smoke_status(table: dict) -> str:
    return str(((table.get("smoke") or {}).get(SMOKE_KEY) or {}).get("status") or "未登记")


def main(argv=None) -> int:
    if not TABLE.is_file():
        print(f"找不到人工画质表：{TABLE}", file=sys.stderr)
        return 2
    try:
        table = json.loads(TABLE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"人工画质表不是合法 JSON：{exc}", file=sys.stderr)
        return 2
    version = skill_version()
    reasons = validate(table, version)
    items = table.get("items") or []
    signed = sum(1 for i in items if i.get("判定") == "通过")
    print(f"人工画质表：{signed}/{len(items)} 项已由真人签署 · 当前版本 {version}")
    if reasons:
        print("闸门拒绝：", file=sys.stderr)
        for r in reasons:
            print(f"  - {r}", file=sys.stderr)
        return 2
    if signed < len(items):
        print(f"闸门结论：拒绝出正式版（原因：十项中有 {len(items) - signed} 项待人工；"
              f"冒烟状态 {_smoke_status(table)}）。解锁前只可写「工程候选可试用」。")
    else:
        print("闸门结论：十项与冒烟均已成立，可进入升正式版复核（签署必须来自真人）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
