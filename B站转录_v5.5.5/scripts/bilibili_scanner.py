#!/usr/bin/env python3

import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None
if load_dotenv is not None:
    load_dotenv(str(SKILL_DIR / ".env"))

import requests  # noqa: E402

FAV_MEDIA_ID = os.environ.get("FAV_MEDIA_ID", "")
EXPAND_PAGES = os.environ.get("EXPAND_PAGES", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
STATE_DIR = SKILL_DIR / "state"
PROCESSED_FILE = STATE_DIR / "processed_videos.txt"
API_BASE = "https://api.bilibili.com/x/v3/fav/resource/list"
PAGELIST_API = "https://api.bilibili.com/x/player/pagelist"
VIEW_API = "https://api.bilibili.com/x/web-interface/view"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com",
}


def processed_key(bvid: str, page: int = 1) -> str:
    page = int(page or 1)
    return bvid if page == 1 else f"{bvid}_P{page}"


def is_processed(processed: set[str], bvid: str, page: int = 1) -> bool:
    page = int(page or 1)
    if processed_key(bvid, page) in processed:
        return True
    return page == 1 and f"{bvid}_P1" in processed


def fetch_all_medias() -> list[dict]:
    all_medias = []
    pn = 1
    while True:
        url = f"{API_BASE}?media_id={FAV_MEDIA_ID}&ps=20&pn={pn}"
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=30)
            data = resp.json()
        except requests.exceptions.RequestException as e:
            print(json.dumps({"error": f"网络请求失败 - {e}"}))
            sys.exit(1)
        except ValueError as e:
            print(json.dumps({"error": f"API响应解析失败 - {e}"}))
            sys.exit(1)

        if data.get("code") != 0:
            print(
                json.dumps({"error": f"B站API返回错误 - {data.get('message', '未知')}"})
            )
            sys.exit(1)

        medias = data["data"].get("medias", [])
        all_medias.extend(medias)
        if not data["data"].get("has_more"):
            break
        pn += 1
    return all_medias


def fetch_video_pages(bvid: str) -> list[tuple[int, int | None, str, int]]:
    if not bvid:
        return [(1, None, "", 0)]

    def fallback() -> list[tuple[int, int | None, str, int]]:
        print(f"⚠️ {bvid} pagelist 获取失败，按单P处理", file=sys.stderr)
        return [(1, None, "", 0)]

    try:
        resp = requests.get(f"{PAGELIST_API}?bvid={bvid}", headers=_HEADERS, timeout=10)
        data = resp.json()
        if data.get("code") != 0:
            return fallback()
        pages = data.get("data") or []
        if not pages:
            return fallback()
        return [
            (
                int(p.get("page") or 1),
                p.get("cid"),
                p.get("part") or "",
                int(p.get("duration") or 0),
            )
            for p in pages
        ]
    except Exception:
        return fallback()


def is_deleted(bvid: str) -> bool:
    try:
        resp = requests.get(f"{VIEW_API}?bvid={bvid}", headers=_HEADERS, timeout=10)
        data = resp.json()
        return data.get("code") == -404
    except Exception:
        return False


def main():
    if not FAV_MEDIA_ID:
        print(
            json.dumps({"error": "请先设置收藏夹ID！编辑 .env 文件，设置 FAV_MEDIA_ID"})
        )
        return 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    medias = fetch_all_medias()

    processed = set()
    if PROCESSED_FILE.exists():
        processed = set(
            line.strip()
            for line in PROCESSED_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    new_videos = []
    deleted_videos = []
    for m in medias:
        bvid = m.get("bvid", "") or m.get("bv_id", "")
        if not bvid:
            continue

        if EXPAND_PAGES:
            pages = fetch_video_pages(bvid)
            if len(pages) == 1 and pages[0][1] is None and is_deleted(bvid):
                deleted_videos.append({"bvid": bvid, "title": m.get("title", "")})
                print(f"⏭️ {bvid} 已失效（-404），跳过", file=sys.stderr)
                continue
            for page, cid, part_title, page_duration in pages:
                if is_processed(processed, bvid, page):
                    continue
                new_videos.append(
                    {
                        "bvid": bvid,
                        "page": page,
                        "cid": cid,
                        "part_title": part_title,
                        "title": m.get("title", ""),
                        "duration": page_duration or m.get("duration", 0),
                        "upper": m.get("upper", {}).get("name", ""),
                        "pubtime": m.get("pubtime", 0),
                    }
                )
        else:
            if is_processed(processed, bvid, 1):
                continue
            new_videos.append(
                {
                    "bvid": bvid,
                    "page": 1,
                    "cid": None,
                    "part_title": "",
                    "title": m.get("title", ""),
                    "duration": m.get("duration", 0),
                    "upper": m.get("upper", {}).get("name", ""),
                    "pubtime": m.get("pubtime", 0),
                }
            )

    output = {
        "collection_total": len(medias),
        "processed": len(processed),
        "expand_pages": EXPAND_PAGES,
        "deleted_videos": deleted_videos,
        "new_videos": new_videos,
    }
    output["status"] = (
        "all_caught_up" if not new_videos else f"new_videos:{len(new_videos)}"
    )
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
