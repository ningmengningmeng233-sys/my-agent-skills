#!/usr/bin/env python3

import argparse
import os
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None
if load_dotenv is not None:
    load_dotenv(str(Path(__file__).resolve().parent.parent / ".env"), override=True)

try:
    import requests
except ImportError:
    print("错误: 请安装 requests: pip install requests", file=sys.stderr)
    sys.exit(1)

from transcript_db import TranscriptDB  # noqa: E402
from logger import log, success, warn, error as log_error  # noqa: E402


def display_key(bvid: str, page: int = 1) -> str:
    return bvid if int(page or 1) == 1 else f"{bvid}_P{int(page or 1)}"


MIN_SUMMARY_CHARS = 80
MIN_TRANSCRIPT_CHARS = 300


def _looks_truncated(content: str, finish_reason: str, transcript_chars: int) -> bool:
    """判断摘要是否疑似被截断。

    触发条件：空输出、`finish_reason` 不是正常的 stop，或正文足够长但摘要短得离谱。
    后者是实测到的真实故障：网关偶尔返回被切断的内容却不报错，
    旧实现会把它当正常摘要写库（曾存下 62 字的半句摘要）。
    """
    text = str(content or "").strip()
    if not text:
        return True
    if finish_reason and finish_reason != "stop":
        return True
    return transcript_chars >= MIN_TRANSCRIPT_CHARS and len(text) < MIN_SUMMARY_CHARS


def call_llm_api(
    title: str, transcript_text: str, api_key: str, api_url: str, api_model: str
) -> str:
    payload = {
        "model": api_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一个视频摘要助手。请对以下转录文本生成结构化摘要，"
                    "包含：1) 核心观点 2) 主要论点 3) 关键结论。用中文回复，简洁明了。"
                ),
            },
            {
                "role": "user",
                "content": f"视频标题：{title}\n\n转录文本：\n{transcript_text[:30000]}",  # noqa: E501
            },
        ],
        "max_tokens": 1024,
    }
    last = ""
    for attempt in (1, 2):
        resp = requests.post(
            api_url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            timeout=60,
        )
        resp.raise_for_status()
        resp_data = resp.json()
        choices = resp_data.get("choices", [])
        if not choices or "message" not in choices[0]:
            raise ValueError(f"API 返回格式异常: {resp_data}")
        choice = choices[0]
        content = str((choice.get("message") or {}).get("content") or "")
        finish = str(choice.get("finish_reason") or "")
        transcript_chars = len(str(transcript_text or ""))
        if not _looks_truncated(content, finish, transcript_chars):
            return content
        last = content
        log("generate_summary",
            f"第 {attempt} 次摘要疑似被截断（{len(content.strip())} 字，"
            f"finish_reason={finish or '未知'}）")
    # 宁可留占位符，也不把残缺摘要写进库：交给上层标记为待补
    raise ValueError(
        f"摘要输出疑似被截断（{len(last.strip())} 字），重试后仍不合格，保留占位符")


def get_env_config() -> dict:
    return {
        "api_key": os.environ.get("OPENAI_API_KEY", ""),
        "api_url": os.environ.get(
            "SUMMARY_API_URL", "https://api.openai.com/v1/chat/completions"
        ),
        "api_model": os.environ.get("SUMMARY_API_MODEL", "gpt-4o-mini"),
    }


def generate_summary_by_bvid(
    bvid: str,
    page: int = 1,
    api_key: str | None = None,
    api_url: str | None = None,
    api_model: str | None = None,
) -> tuple[bool, str | None]:
    page = int(page or 1)
    key = display_key(bvid, page)
    config = get_env_config()
    api_key = api_key or config["api_key"]
    api_url = api_url or config["api_url"]
    api_model = api_model or config["api_model"]

    if not api_key:
        log("generate_summary", f"跳过 {key}: 未设置 OPENAI_API_KEY")
        return False, None

    with TranscriptDB() as db:
        record = db.get_by_bvid(bvid, page)
        if not record:
            return False, None

        if record.get("summary"):
            log("generate_summary", f"{key} 已有摘要，跳过")
            return True, record["summary"]

        title = record.get("title", "")
        if record.get("part_title"):
            title = f"{title}（P{page} {record.get('part_title')}）"
        transcript_text = record.get("transcript_text", "")
        if not transcript_text:
            log("generate_summary", f"{key} 无转录全文，跳过")
            return False, None

        try:
            log("generate_summary", f"开始生成摘要: {key} - {title[:30]}")
            summary = call_llm_api(title, transcript_text, api_key, api_url, api_model)
            db.update_summary(bvid, page, summary.strip())
            try:
                db.render_txt(bvid, page)
            except Exception as e:
                log_error("generate_summary", f"{key} 摘要已入库但TXT渲染失败: {e}")
            success("generate_summary", f"摘要生成成功: {key}")
            return True, summary
        except Exception as e:
            log_error("generate_summary", f"{key} 摘要生成失败: {e}")
            print(f"   ⚠️ LLM摘要生成失败: {e}", file=sys.stderr)
            return False, None


def generate_summary(
    filepath: str,
    api_key: str | None = None,
    api_url: str | None = None,
    api_model: str | None = None,
) -> tuple[bool, str | None]:
    path = Path(filepath)
    if not path.exists():
        return False, None
    content = path.read_text(encoding="utf-8")

    if "【AI待处理" not in content:
        return False, None

    config = get_env_config()
    api_key = api_key or config["api_key"]
    api_url = api_url or config["api_url"]
    api_model = api_model or config["api_model"]

    if not api_key:
        return False, None

    title = ""
    for line in content.splitlines():
        if "视频标题：" in line:
            title = line.split("视频标题：", 1)[1].strip()
            break

    text_start = content.find("第二部分：完整原文")
    if text_start == -1:
        return False, None
    transcript_text = content[text_start:].strip()

    try:
        summary = call_llm_api(title, transcript_text, api_key, api_url, api_model)
        path.write_text(
            content.replace(
                "【AI待处理：请阅读全文后，替换此行，写结构化摘要】", summary.strip()
            ),
            encoding="utf-8",
        )

        bvid_match = re.search(r"BV[a-zA-Z0-9]+", content)
        if bvid_match:
            page_match = re.search(r"分P序号：P(\d+)", content) or re.search(
                r"[?&]p=(\d+)", content
            )
            page = int(page_match.group(1)) if page_match else 1
            try:
                with TranscriptDB() as db:
                    db.update_summary(bvid_match.group(0), page, summary.strip())
            except Exception as e:
                warn("generate_summary", f"TXT摘要已生成但DB同步失败: {e}")
        return True, summary
    except Exception as e:
        print(f"   ⚠️ LLM摘要生成失败: {e}", file=sys.stderr)
        return False, None


def main():
    parser = argparse.ArgumentParser(description="AI视频摘要生成器")
    parser.add_argument("filepath", nargs="?", help="转录TXT文件路径（文件模式）")
    parser.add_argument("--bvid", help="B站BV号（DB模式，推荐）")
    parser.add_argument("--page", type=int, default=1, help="分P序号，默认1")
    parser.add_argument("--api-key", help="API密钥（覆盖环境变量）")
    parser.add_argument("--api-url", help="API地址（覆盖环境变量）")
    parser.add_argument("--model", help="模型名（覆盖环境变量）")
    args = parser.parse_args()

    if args.bvid:
        ok, _ = generate_summary_by_bvid(
            args.bvid,
            page=args.page,
            api_key=args.api_key,
            api_url=args.api_url,
            api_model=args.model,
        )
        if ok:
            print(f"✅ 摘要已就绪（{display_key(args.bvid, args.page)}）")
            return 0
    elif args.filepath:
        ok, _ = generate_summary(
            args.filepath,
            api_key=args.api_key,
            api_url=args.api_url,
            api_model=args.model,
        )
        if ok:
            print("✅ AI摘要已生成")
            return 0
    else:
        parser.print_help()
        return 1

    if not (args.api_key or os.environ.get("OPENAI_API_KEY", "")):
        print("ℹ️  未设置 OPENAI_API_KEY，跳过摘要生成")
    else:
        print("❌ 摘要生成失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
