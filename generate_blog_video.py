#!/usr/bin/env python3
"""Blog article URL -> Kurage 2-minute commentary video.

This is separate from generate_news_videos.py. Use this for VWork blog,
episodes, and other article-style pages where the video should preserve the
blog title and speak as commentary rather than a news broadcast.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import requests

KURAGE_API = "http://exbridge.ddns.net:18200"
PUBLIC_HORIZONV = "https://aiknowledgecms.exbridge.jp/horizonv.php"


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def submit(url: str) -> str:
    res = requests.post(f"{KURAGE_API}/generate_from_blog_url", json={"url": url}, timeout=20)
    data = res.json()
    if not data.get("ok"):
        raise RuntimeError(f"送信失敗: {data}")
    return data["job_id"]


def wait_done(job_id: str, timeout: int = 1800) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = requests.get(f"{KURAGE_API}/status/{job_id}", timeout=10)
        data = res.json()
        status = data.get("status", "")
        log(f"  [{job_id}] {status} ({data.get('progress', 0)}%)")
        if status == "done":
            return data
        if status == "error":
            raise RuntimeError(f"生成エラー: {data.get('error')}")
        time.sleep(30)
    raise TimeoutError(f"タイムアウト: {job_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ブログ記事URLから2分の考察動画を生成")
    parser.add_argument("url")
    parser.add_argument("--wait", action="store_true", help="完了まで待機する")
    parser.add_argument("--dry-run", action="store_true", help="送信せずURLだけ確認")
    args = parser.parse_args()

    url = args.url.strip()
    if not url:
        raise SystemExit("url is required")

    log(f"blog url: {url}")
    if args.dry_run:
        print(json.dumps({"url": url, "method": "generate_from_blog_url"}, ensure_ascii=False, indent=2))
        return

    job_id = submit(url)
    log(f"送信完了: job_id={job_id}")
    log(f"動画ページ: {PUBLIC_HORIZONV}?id={job_id}")

    if args.wait:
        data = wait_done(job_id)
        log(f"動画生成完了: {PUBLIC_HORIZONV}?id={job_id}")
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
