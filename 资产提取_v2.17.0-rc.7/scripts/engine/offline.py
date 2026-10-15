# -*- coding: utf-8 -*-
"""显式离线开关（问题清单 B8 修复）。

背景：把 `GEMINI_API_KEY` 等密钥从进程环境里全部 unset 并不等于「不外呼」——
配置解析链会回退读取包外 `.env`（`ASSET_ENV_PATH` → 技能目录 `.env` →
`~/.asset-extract/.env`），其中的密钥与 `GOOGLE_GEMINI_BASE_URL`（本机中转池）
一起被注入进程环境，代码于是带着「你以为不存在」的密钥直连传输层。

本模块提供唯一事实源的离线语义：

- `ASSET_OFFLINE=1`（或 true/yes/on）置真时：
  1. 传输层在任何 socket 之前抛 `OfflineRequested`（真实外呼一律本地拦截）；
  2. 配置解析**不再回退**包外隐式 `.env`（技能目录 / `~/.asset-extract`），
     只认显式 `ASSET_ENV_PATH`——测试与自动化跑批因此与本机钥匙串状态无关；
- 豁免：显式 loopback（127.x / localhost / ::1）端点允许——单测假端点走的就是
  回环地址；生产直连域名（含未显式配置 base_url 的默认端点）一律拦截。

零依赖、零业务算法改动：只服务密钥校验与离线开关。
"""
from __future__ import annotations

import os
import re

OFFLINE_ENV = "ASSET_OFFLINE"
OFFLINE_BLOCKED = "OFFLINE_BLOCKED"

_TRUTHY = ("1", "true", "yes", "on")
_LOOPBACK_RE = re.compile(
    r"^https?://(127\.\d{1,3}\.\d{1,3}\.\d{1,3}|localhost|\[::1\])(:\d+)?([/?]|$)",
    re.IGNORECASE)


class OfflineRequested(RuntimeError):
    """离线模式置真、且调用即将触达真实网络时抛出：**发生在任何 socket 之前**。"""

    code = OFFLINE_BLOCKED

    def __init__(self, detail: str = ""):
        self.detail = detail or (
            f"离线模式（{OFFLINE_ENV}）已置真：拒绝发起任何外呼。")
        super().__init__(f"{self.code}：{self.detail}")


def offline_requested(env=None) -> bool:
    """`ASSET_OFFLINE` 是否置真（按调用时读取，便于测试注入）。"""
    source = os.environ if env is None else env
    return str(source.get(OFFLINE_ENV) or "").strip().lower() in _TRUTHY


def is_loopback_url(url) -> bool:
    """仅显式回环端点判真（127.0.0.0/8、localhost、::1）。"""
    return bool(_LOOPBACK_RE.match(str(url or "").strip()))


def ensure_online(action: str, url: str | None = None, env=None) -> None:
    """真实 socket 打开前必须调用；离线模式置真即抛 `OfflineRequested`。

    `url` 给出时，显式 loopback 端点放行（单测假端点）；`url=None` 表示目标
    端点不可知或默认真实服务商——离线时一律硬拦。
    """
    if not offline_requested(env):
        return
    if url is not None and is_loopback_url(url):
        return
    raise OfflineRequested(
        f"离线模式（{OFFLINE_ENV}）已置真：{action}已在任何网络请求之前本地拦截。"
        f"确认要外呼请取消 {OFFLINE_ENV}，并设置环境变量 GEMINI_API_KEY；"
        "不想外呼可用 export 子命令手工导出提示词。")
