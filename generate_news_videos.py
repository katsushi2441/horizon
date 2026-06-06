#!/usr/bin/env python3
"""Horizon summary → Kurage backend でニュース動画を生成するスクリプト

上位3〜5記事をまとめて1本のKurage動画（12シーン・約2分）を生成する。

使い方:
  python3 generate_news_videos.py               # 最新summaryから上位5記事で1本生成
  python3 generate_news_videos.py --max 3       # 上位3記事まとめ
  python3 generate_news_videos.py --dry-run     # 送信せず確認のみ
  python3 generate_news_videos.py --wait        # 完了まで待機
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

HORIZON_DIR = Path(os.environ.get("HORIZON_DIR") or (Path(__file__).parent / "Horizon"))
SUMMARIES_DIR = HORIZON_DIR / "data" / "summaries"
KURAGE_JOBS_DIR = Path(os.environ.get("KURAGE_JOBS_DIR", "/home/kojima/exdirect/kurage/storage/jobs"))
KURAGE_API = os.environ.get("KURAGE_API", "http://exbridge.ddns.net:18200")
VWORK_ARTICLES_URL = os.environ.get("VWORK_ARTICLES_URL", "https://katsushi2441.github.io/vwork/articles/")
VWORK_ARTICLES_DIR = Path(os.environ.get("VWORK_ARTICLES_DIR") or Path(os.environ.get("VWORK_DIR", "/home/kojima/exdirect/vwork")) / "articles")


def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def get_latest_summary() -> tuple[Path, str]:
    today = date.today().strftime("%Y-%m-%d")
    for pattern in [f"horizon-{today}-ja.md", f"horizon-{today}-*.md"]:
        files = sorted(SUMMARIES_DIR.glob(pattern), reverse=True)
        if files:
            return files[0], today
    raise FileNotFoundError(f"今日のsummaryファイルが見つかりません: horizon-{today}-*.md")


def parse_news_items(md_text: str) -> list[dict]:
    """Horizon Markdown から記事を抽出する。"""
    items = []

    # frontmatterを除去
    if md_text.startswith("---"):
        end = md_text.find("---", 3)
        if end != -1:
            md_text = md_text[end + 3:].lstrip()

    # Horizon summaryの各ニュースは「## [title](url) ⭐️ score」で始まる。
    # H1や目次は動画素材にしない。
    sections = re.split(r'\n(?=## \[)', md_text)

    for section in sections:
        lines = section.strip().splitlines()
        if not lines:
            continue

        header_line = lines[0].strip()
        if not header_line.startswith('## ['):
            continue
        title = re.sub(r'^##\s*', '', header_line).strip()
        title = re.sub(r'\s*⭐️.*$', '', title).strip()
        title = re.sub(r'^\[(.*?)\]\((.*?)\)$', r'\1', title).strip()
        if not title or len(title) < 5:
            continue

        body_lines = [
            l for l in lines[1:]
            if l.strip()
            and not l.startswith('<a id=')
            and not l.startswith('rss ·')
            and not l.startswith('reddit ·')
            and not l.startswith('hackernews ·')
            and not l.startswith('**Tags**:')
            and l.strip() != '---'
        ]
        content = ' '.join(body_lines).strip()
        if not content:
            content = title

        urls = re.findall(r'https?://[^\s\)\]]+', section)
        url = urls[0] if urls else ''

        source_name = "Horizon"
        if url:
            m = re.search(r'https?://([^/]+)', url)
            if m:
                domain = m.group(1).replace('www.', '')
                source_name = domain.split('.')[0].capitalize()

        items.append({
            "title": title[:100],
            "content": content[:400],
            "url": url,
            "source_name": source_name,
        })

    return items


STOP_TOPIC_TOKENS = {
    "https", "http", "www", "com", "the", "and", "with", "from", "that",
    "this", "into", "using", "uses", "lets", "new", "news", "blog", "study",
    "tool", "tools", "model", "models", "agent", "agents", "open", "source",
}


def topic_tokens(text: str) -> set[str]:
    tokens = set()
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._+-]{2,}", text.lower()):
        token = token.strip("._+-")
        if len(token) < 3 or token in STOP_TOPIC_TOKENS:
            continue
        tokens.add(token)
    return tokens


def collect_used_topic_text(summary_date: str, current_article_url: str) -> str:
    parts = []
    for path in sorted(VWORK_ARTICLES_DIR.glob(f"{summary_date}-ai-news*.md")):
        article_url = f"{VWORK_ARTICLES_URL}{path.stem}.html"
        if article_url == current_article_url:
            continue
        try:
            parts.append(path.read_text(encoding="utf-8"))
        except Exception:
            continue
    prefix = f"{VWORK_ARTICLES_URL}{summary_date}-ai-news"
    if KURAGE_JOBS_DIR.exists():
        for meta_path in sorted(KURAGE_JOBS_DIR.glob("*.json")):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            tweet_url = str(data.get("tweet_url") or "")
            if not tweet_url.startswith(prefix) or tweet_url == current_article_url:
                continue
            parts.extend([
                str(data.get("title") or ""),
                str(data.get("tweet_text") or ""),
            ])
            script = data.get("script") or {}
            if isinstance(script, dict):
                for scene in script.get("scenes") or []:
                    if isinstance(scene, dict):
                        parts.append(str(scene.get("narration") or ""))
    return "\n".join(parts)


def filter_used_items(items: list[dict], summary_date: str, current_article_url: str) -> list[dict]:
    used_text = collect_used_topic_text(summary_date, current_article_url)
    used_lower = used_text.lower()
    used_tokens = topic_tokens(used_text)
    if not used_tokens:
        return items
    filtered = []
    for item in items:
        title = item.get("title") or ""
        tokens = topic_tokens(title + " " + (item.get("content") or "")[:160])
        overlap = tokens & used_tokens
        exact = title.lower() and title.lower() in used_lower
        has_strong_overlap = any(len(token) >= 7 for token in overlap)
        if exact or has_strong_overlap or (len(overlap) >= 2 and len(overlap) / max(1, len(tokens)) >= 0.25):
            log(f"同日既存動画/記事との重複を除外: {title}")
            continue
        filtered.append(item)
    removed = len(items) - len(filtered)
    if removed:
        log(f"同日重複除外: {removed}件")
    return filtered


def article_url_for(summary_date: str) -> str:
    return f"{VWORK_ARTICLES_URL}{summary_date}-ai-news.html"


def already_generated_for_article(article_url: str) -> bool:
    """対象記事URLの動画が既に生成済み/生成中かチェック"""
    try:
        res = requests.get(f"{KURAGE_API}/jobs?source=horizon&limit=50", timeout=10)
        data = res.json()
        for j in data.get("jobs", []):
            if j.get("tweet_url") == article_url and j.get("status") in ("done", "scripting", "imaging", "rendering", "queued"):
                return True
    except Exception as exc:
        log(f"生成済みチェック失敗: {exc}")
    return False


def submit(news_items: list, title: str) -> str:
    payload = {"news_items": news_items, "title": title}
    res = requests.post(f"{KURAGE_API}/generate_from_news", json=payload, timeout=15)
    data = res.json()
    if not data.get("ok"):
        raise RuntimeError(f"送信失敗: {data}")
    return data["job_id"]


def wait_done(job_id: str, timeout: int = 900) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            res = requests.get(f"{KURAGE_API}/status/{job_id}", timeout=10)
            data = res.json()
            status = data.get("status", "")
            log(f"  [{job_id}] {status} ({data.get('progress', 0)}%)")
            if status == "done":
                return data
            if status == "error":
                raise RuntimeError(f"生成エラー: {data.get('error')}")
        except requests.RequestException as exc:
            log(f"  ステータス取得失敗: {exc}")
        time.sleep(30)
    raise TimeoutError(f"タイムアウト: {job_id}")


def main():
    parser = argparse.ArgumentParser(description="Horizon ニュース → Kurage 動画生成（複数記事→1本）")
    parser.add_argument("--max", type=int, default=5, help="まとめる記事数（デフォルト5）")
    parser.add_argument("--dry-run", action="store_true", help="送信せず確認のみ")
    parser.add_argument("--wait", action="store_true", help="完了まで待機する")
    parser.add_argument("--force", action="store_true", help="今日分が既にあっても生成する")
    parser.add_argument("--article-url", default="", help="動画の識別に使うVWork記事URL")
    args = parser.parse_args()

    summary_path, summary_date = get_latest_summary()
    log(f"summary: {summary_path}")

    article_url = args.article_url.strip() or article_url_for(summary_date)

    if not args.force and not args.dry_run and already_generated_for_article(article_url):
        log(f"対象記事の動画は既に生成済みです: {article_url}（--force で強制実行）")
        return

    md_text = summary_path.read_text(encoding="utf-8")
    items = parse_news_items(md_text)
    items = filter_used_items(items, summary_date, article_url)
    log(f"記事を {len(items)} 件抽出")

    if not items:
        log("記事が見つかりません")
        sys.exit(1)

    top_items = items[:args.max]
    # Horizonの日次動画は「個別ニュース」ではなく日次記事の動画として管理する。
    # Kurage側は先頭news_itemのurlを動画の識別URLに使うため、ここで日次記事URLを入れる。
    top_items[0]["url"] = article_url
    title = f"AIニュース {summary_date} — " + "・".join(i["title"][:15] for i in top_items[:3])
    title = title[:60]

    log(f"動画タイトル: {title}")
    for i, item in enumerate(top_items, 1):
        log(f"  {i}. [{item['source_name']}] {item['title'][:50]}")

    if args.dry_run:
        print(json.dumps({"title": title, "news_items": top_items}, ensure_ascii=False, indent=2))
        return

    job_id = submit(top_items, title)
    log(f"送信完了: job_id={job_id}")

    if args.wait:
        wait_done(job_id)
        log(f"動画生成完了: https://aiknowledgecms.exbridge.jp/horizonv.php")


if __name__ == "__main__":
    main()
