"""调用 OpenAI Chat Completions 兼容接口生成三段式摘要。

摘要失败绝不阻断转录交付：任何异常/空响应/无密钥均返回 pending，
由 fill_summaries.py 后续补跑。日志与返回值不含密钥或正文。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import requests

try:  # python-dotenv 是锁定依赖；不可导入时退化为内置最小解析，与各入口同一条路
    from dotenv import load_dotenv
except ImportError:
    from scripts.paths import minimal_load_dotenv as load_dotenv

import os

logger = logging.getLogger("douyin_transcript.summary")

MAX_TRANSCRIPT_CHARS = 30_000
MAX_SUMMARY_TOKENS = 1024
REQUEST_TIMEOUT_SECONDS = 60

MODE_NORMAL = "normal"
MODE_RESTRICTED = "restricted"
MODE_VISUAL = "visual_text_only"

PROMPT_VERSION = "v2-summary-1"

_NL = "\n"
_HEADINGS = _NL.join(("核心观点：", "主要论点：", "关键结论：", ""))

SYSTEM_PROMPT = (
    "你是一名严谨的中文内容分析助手。请基于给定的视频标题与完整口播逐字稿，"
    "输出简洁的中文摘要，必须依次包含三个部分："
    + _NL + _HEADINGS +
    "只依据逐字稿内容，不要编造；语言简洁，避免空话。"
)

RESTRICTED_PROMPT = (
    "你是一名严谨的中文内容分析助手。这条视频的识别质量偏低（低质量素材），"
    "摘要必须保持保守：不得确定人名、数字、机构或争议结论，"
    "遇到不确定处写“疑似”“未能确认”。"
    "输出必须依次包含三个部分："
    + _NL + _HEADINGS +
    "只依据给定素材，不要编造。"
)

VISUAL_PROMPT = (
    "你是一名严谨的中文内容分析助手。这条视频没有可用旁白，只有画面文字。"
    "请只依据画面文字输出摘要，措辞必须明确写成“画面显示/画面文字”，"
    "不得声称是口播内容。输出必须依次包含三个部分："
    + _NL + _HEADINGS
)

_RESTRICTED_PREFIX = ("【受限摘要：本条识别质量偏低，以下内容仅供参考，"
                      "请以证据文件为准】" + _NL)

SYSTEM_PROMPTS = {MODE_NORMAL: SYSTEM_PROMPT, MODE_RESTRICTED: RESTRICTED_PROMPT,
                  MODE_VISUAL: VISUAL_PROMPT}

DEFAULT_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_summary_config() -> dict:
    load_dotenv(_ENV_FILE)
    return {
        "api_key": os.environ.get("OPENAI_API_KEY", "").strip(),
        "url": os.environ.get("SUMMARY_API_URL", "").strip() or DEFAULT_URL,
        "model": os.environ.get("SUMMARY_API_MODEL", "").strip() or DEFAULT_MODEL,
    }


def summary_source_text(transcript: str, corrected_text: str, mode: str,
                        *, visual_text: str = "") -> str:
    """摘要依据：正常/受限用校正版（缺失时退回原始稿），画面模式只用画面文字。"""
    if mode == MODE_VISUAL:
        return str(visual_text or "")[:MAX_TRANSCRIPT_CHARS]
    return str(corrected_text or transcript or "")[:MAX_TRANSCRIPT_CHARS]


def build_messages(title: str, transcript: str, *, mode: str = MODE_NORMAL,
                   visual_text: str = "") -> list[dict]:
    resolved_mode = mode if mode in SYSTEM_PROMPTS else MODE_NORMAL
    if resolved_mode == MODE_VISUAL:
        label = "画面文字"
        body = str(visual_text or transcript or "")[:MAX_TRANSCRIPT_CHARS]
    else:
        label = "完整逐字稿"
        body = str(transcript or "")[:MAX_TRANSCRIPT_CHARS]
    hint = (_NL * 2 + "（本条为低质量受限摘要，请保守表述）"
            if resolved_mode == MODE_RESTRICTED else "")
    user = (f"视频标题：{title or '无标题'}" + _NL * 2
            + f"{label}：" + _NL + body + hint)
    return [
        {"role": "system", "content": SYSTEM_PROMPTS[resolved_mode]},
        {"role": "user", "content": user},
    ]


def summary_meta(config: dict, mode: str, *, pipeline_version: str = "v2") -> dict:
    return {"model": str((config or {}).get("model", "")),
            "mode": mode, "prompt_version": PROMPT_VERSION,
            "pipeline_version": pipeline_version}


def call_api(messages: list[dict], config: dict) -> str:
    response = requests.post(
        config["url"],
        json={"model": config["model"], "messages": messages,
              "max_tokens": MAX_SUMMARY_TOKENS},
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    choices = data.get("choices") if isinstance(data, dict) else None
    content = ""
    if isinstance(choices, list) and choices:
        first = choices[0] or {}
        message = first.get("message") or {}
        content = str(message.get("content") or "")
    return content.strip()


def call_api_safe(messages: list[dict],
                  config: dict) -> tuple[Literal["summarized", "pending"], str]:
    try:
        summary = call_api(messages, config)
    except Exception as exc:  # 摘要失败绝不能影响原稿交付
        logger.warning("摘要生成失败（%s），标记为待补", type(exc).__name__)
        return ("pending", "")
    if not summary:
        logger.warning("摘要响应为空，标记为待补")
        return ("pending", "")
    return ("summarized", summary)


def _decorate(summary: str, mode: str) -> str:
    if mode == MODE_RESTRICTED:
        return _RESTRICTED_PREFIX + summary
    if mode == MODE_VISUAL:
        return ("【画面文字记录】以下摘要仅依据画面文字，不含旁白转录" + _NL
                + summary)
    return summary


def generate_or_pending(
    title: str,
    transcript: str,
    config: dict | None = None,
    *,
    mode: str = MODE_NORMAL,
    visual_text: str = "",
) -> tuple[Literal["summarized", "pending"], str]:
    """永不抛异常到主转录编排器；失败即 pending（三种模式一致）。"""
    try:
        resolved = config or load_summary_config()
    except OSError:
        return ("pending", "")
    if not resolved.get("api_key"):
        logger.info("未配置摘要 API 密钥，摘要标记为待补")
        return ("pending", "")
    resolved_mode = mode if mode in SYSTEM_PROMPTS else MODE_NORMAL
    status, summary = call_api_safe(
        build_messages(title, transcript, mode=resolved_mode,
                       visual_text=visual_text), resolved)
    if status != "summarized":
        return (status, "")
    return (status, _decorate(summary, resolved_mode))
