# -*- coding: utf-8 -*-
"""跳过清单的唯一解析层（check_schedule / check_asset_coverage / pipeline_state 共用）。

同一份 `提示词/基础/跳过清单.md` 曾有三个读者、两套文法、一套子串匹配，实测两种代价：

- **白跑一整趟真端点**：生成侧写短横条目、旧库留表格行；覆盖率门两者都认，`check_schedule`
  只认短横 → 覆盖率门判"合规跳过"、verify 报「缺基础提示词」，两条门各自自洽却互相打脸。
- **豁免外溢（假放行）**：`asset_id in k or k in asset_id` 的双向子串匹配下，清单里只写了
  `PR_剑`，毫无产物的 `PR_剑鞘` 也拿到豁免理由 → 覆盖率门（签发门之一）判 PASS。

所以：认两种格式，ID 一律**精确**匹配。
"""
from __future__ import annotations

import re

# 资产 ID：两字母前缀 + 下划线 + 名称；名称里不得再出现空白/冒号/括号/顿号，
# 否则「- CH_张三：非资产」会把理由一起吞进 ID（旧的 `[A-Z]{2}_\S+` 就吃掉了）。
_ID = r"[A-Z]{2}_[^\s：:（(,，、]+"
_BULLET_RE = re.compile(rf"^[-*]\s*({_ID})")
_TABLE_ID_RE = re.compile(rf"^({_ID})$")


def parse_skip_entries(text: str) -> dict[str, str]:
    """解析跳过清单正文 → `{资产ID: 理由}`；短横条目与表格行都认。"""
    out: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _BULLET_RE.match(line)
        if m:
            out[m.group(1)] = line[m.end():].strip()
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells and _TABLE_ID_RE.match(cells[0]):
                out[cells[0]] = " ".join(cells[1:]).strip()
    return out
