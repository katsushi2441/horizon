#!/usr/bin/env python3
"""Horizonワーカー — 記事生成・動画生成・AIxSNS告知を一括実行

kdeck/RQDB4AIから6時間ごと、1日4回を目安に実行される。
1. Horizon でニュース収集・summary生成
2. post_to_zenn.py で記事投稿（GitHub Pages）
3. AIxSNS で記事告知（author=kurage）
4. はてなブログ/Bloggerへメール投稿
5. generate_news_videos.py で動画生成
6. AIxSNS で動画告知（author=kurage）
7. dashboard に実行結果を報告

YouTubeへの自動アップロードは通常行わない。
明示的に HORIZON_AUTO_YOUTUBE_UPLOAD=1 を設定した場合だけ実行する。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urljoin
from datetime import date
from pathlib import Path
import glob
import re

SCRIPT_DIR = Path(__file__).parent
HORIZON_DIR = SCRIPT_DIR / "Horizon"
KURAGE_DIR = SCRIPT_DIR.parent / "kurage"
YOUTUBE_DIR = Path(os.environ.get("YOUTUBE_DIR", str(SCRIPT_DIR.parent / "airadio-scripted-mv")))
YOUTUBE_UPLOAD = YOUTUBE_DIR / "tools" / "youtube" / "upload_youtube.py"
YOUTUBE_STORAGE = YOUTUBE_DIR / "storage" / "youtube"
KURAGE_API = os.environ.get("KURAGE_API", "http://localhost:18303")
AIXSNS_API = os.environ.get("AIXSNS_API", "https://aixec.exbridge.jp/api.php?path=posts")
DASHBOARD_API = os.environ.get("DASHBOARD_API", "http://localhost:8081/worker/report")
VWORK_DIR = Path(os.environ.get("VWORK_DIR", str(SCRIPT_DIR.parent / "vwork")))
HORIZON_AUTO_YOUTUBE_UPLOAD = os.environ.get("HORIZON_AUTO_YOUTUBE_UPLOAD", "0").strip().lower() in {"1", "true", "yes", "on"}
VWORK_ARTICLES_URL = "https://katsushi2441.github.io/vwork/articles/"
HORIZONV_URL = "https://aiknowledgecms.exbridge.jp/horizonv.php"
LOG_PATH = Path("/tmp/horizon_worker.log")
LOCK_PATH = Path("/tmp/horizon_worker_api.pid")


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            env[key] = value
    return env


def log(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def find_ssh_agent() -> str:
    for sock in glob.glob("/tmp/ssh-*/agent.*"):
        result = subprocess.run(["ssh-add", "-l"],
                                env={**os.environ, "SSH_AUTH_SOCK": sock},
                                capture_output=True)
        if result.returncode == 0:
            return sock
    return os.environ.get("SSH_AUTH_SOCK", "")


def report_worker(status: str, items: int, note: str = ""):
    try:
        payload = json.dumps({
            "name": "horizon-worker-enqueue",
            "status": status,
            "items": items,
            "note": note[:200],
        }).encode()
        req = urllib.request.Request(DASHBOARD_API, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"dashboard report失敗: {e}")


def post_to_sns(content: str) -> str:
    try:
        payload = json.dumps({"author": "kurage", "content": content}).encode()
        req = urllib.request.Request(AIXSNS_API, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as res:
            data = json.loads(res.read())
        post_id = data.get("item", {}).get("id", "")
        log(f"AIxSNS投稿完了: id={post_id}")
        return str(post_id)
    except Exception as e:
        log(f"AIxSNS投稿失敗: {e}")
        return ""


def post_article_to_hatena_blogger(article: Path) -> bool:
    script = VWORK_DIR / "scripts" / "post_to_hatena.py"
    if not article.exists():
        log(f"はてな/Blogger投稿スキップ: 記事なし {article}")
        return False
    if not script.exists():
        log(f"はてな/Blogger投稿スキップ: スクリプトなし {script}")
        return False

    env = {**os.environ}
    # The mail credentials live outside this repo. Do not print them; only pass
    # them through to the existing VWork mail posting script.
    for key, value in load_env_file(SCRIPT_DIR.parent / "aixec" / ".env").items():
        env.setdefault(key, value)

    required = ["SMTP_FROM", "SMTP_PASSWORD", "HATENA_POST_EMAIL"]
    missing = [key for key in required if not env.get(key)]
    if missing:
        log(f"はてな/Blogger投稿スキップ: 環境変数不足 {','.join(missing)}")
        return False

    ok = run_step(
        ["python3", str(script), str(article)],
        "はてな/Bloggerメール投稿",
        timeout=300,
        extra_env=env,
        cwd=VWORK_DIR,
    )
    return ok


def run_step(cmd: list, desc: str, timeout: int = 1800, extra_env: dict = None, cwd: Path = SCRIPT_DIR) -> bool:
    log(f"=== {desc} 開始 ===")
    env = {**os.environ}
    if extra_env:
        env.update(extra_env)
    stdout_lines = []
    stderr_lines = []
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    start = time.time()
    import selectors

    sel = selectors.DefaultSelector()
    if proc.stdout:
        sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
    if proc.stderr:
        sel.register(proc.stderr, selectors.EVENT_READ, "stderr")

    while sel.get_map():
        if time.time() - start > timeout:
            proc.kill()
            log(f"=== {desc} タイムアウト ({timeout}s) ===")
            return False
        for key, _ in sel.select(timeout=1):
            line = key.fileobj.readline()
            if not line:
                sel.unregister(key.fileobj)
                continue
            line = line.rstrip()
            if key.data == "stderr":
                stderr_lines.append(line)
                log(f"  [err] {line}")
            else:
                stdout_lines.append(line)
                log(f"  {line}")
    return_code = proc.wait()
    run_step.last_stdout = "\n".join(stdout_lines)
    run_step.last_stderr = "\n".join(stderr_lines)
    if return_code != 0:
        log(f"=== {desc} 失敗 (exit {return_code}) ===")
        return False
    log(f"=== {desc} 完了 ===")
    return True


run_step.last_stdout = ""
run_step.last_stderr = ""


def get_article_title(article: Path) -> str:
    if not article.exists():
        return ""
    for line in article.read_text(encoding="utf-8").splitlines():
        if line.startswith("title:"):
            return line.split(":", 1)[1].strip().strip('"')
    return ""


def article_url(article: Path) -> str:
    return f"{VWORK_ARTICLES_URL}{article.stem}.html"


def get_video_job_id_for_article(target_article_url: str) -> str:
    try:
        res = urllib.request.urlopen(f"{KURAGE_API}/jobs?source=horizon&limit=20", timeout=10)
        data = json.loads(res.read())
        for j in data.get("jobs", []):
            if j.get("tweet_url") == target_article_url and j.get("status") == "done" and j.get("has_video"):
                return j.get("job_id", "")
    except Exception as e:
        log(f"job_id取得失敗: {e}")
    return ""


def parse_created_article(stdout: str) -> Path | None:
    for line in stdout.splitlines():
        m = re.search(r"記事作成:\s*(/.+?\.md)\s*$", line)
        if m:
            return Path(m.group(1))
    return None


def latest_today_article() -> Path | None:
    today = date.today().strftime("%Y-%m-%d")
    files = sorted(VWORK_DIR.joinpath("articles").glob(f"{today}-ai-news*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def parse_job_id(stdout: str) -> str:
    m = re.search(r"送信完了:\s*job_id=([0-9A-Za-z_-]+)", stdout)
    return m.group(1) if m else ""


def wait_video_done(job_id: str, timeout: int = 1800) -> bool:
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        try:
            res = urllib.request.urlopen(f"{KURAGE_API}/status/{job_id}", timeout=10)
            data = json.loads(res.read())
            status = data.get("status", "")
            progress = data.get("progress", 0)
            phase = data.get("phase") or data.get("step") or data.get("message") or data.get("current_step") or ""
            note = f"動画生成待ち job={job_id} status={status} progress={progress}%"
            if phase:
                note += f" phase={phase}"
            log(f"  {note}")
            if note != last_status:
                report_worker("running", 0, note)
                last_status = note
            if status == "done":
                return True
            if status == "error":
                log(f"  動画生成エラー: {data.get('error')}")
                return False
        except Exception as e:
            log(f"  ステータス取得失敗: {e}")
        time.sleep(30)
    log("  動画生成タイムアウト")
    return False


def job_json_path(job_id: str) -> Path:
    return KURAGE_DIR / "storage" / "jobs" / f"{job_id}.json"


def load_job(job_id: str) -> dict:
    path = job_json_path(job_id)
    if not path.exists():
        try:
            res = urllib.request.urlopen(f"{KURAGE_API}/status/{job_id}", timeout=10)
            return json.loads(res.read())
        except Exception as exc:
            log(f"job API読込失敗: {exc}")
            return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"job JSON読込失敗: {exc}")
        return {}


def save_job(job_id: str, data: dict):
    path = job_json_path(job_id)
    if not path.exists():
        return
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def download_kurage_asset(job_id: str, job: dict, key: str, suffix: str) -> Path:
    url = job.get(key) or ""
    if not url:
        return Path("")
    if url.startswith("/"):
        url = urljoin(KURAGE_API.rstrip("/") + "/", url.lstrip("/"))
    out_dir = Path("/tmp/horizon_youtube") / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{key}{suffix}"
    try:
        urllib.request.urlretrieve(url, out_path)
        return out_path
    except Exception as exc:
        log(f"Kurage asset download失敗: {key} {url} {exc}")
        return Path("")


def upload_youtube(job_id: str, title: str) -> tuple[str, bool]:
    job = load_job(job_id)
    if job.get("youtube_url"):
        log(f"YouTube投稿済み: {job['youtube_url']}")
        return job["youtube_url"], False

    video_path = KURAGE_DIR / "storage" / "jobs" / job_id / "output.mp4"
    thumbnail_path = KURAGE_DIR / "storage" / "jobs" / job_id / "thumbnail.jpg"
    if not video_path.exists():
        downloaded = download_kurage_asset(job_id, job, "video_url", ".mp4")
        if downloaded.exists():
            video_path = downloaded
        else:
            log(f"YouTube投稿スキップ: 動画ファイルなし {video_path}")
            return "", False
    if not thumbnail_path.exists():
        downloaded_thumb = download_kurage_asset(job_id, job, "thumbnail_url", ".jpg")
        if downloaded_thumb.exists():
            thumbnail_path = downloaded_thumb
    if not YOUTUBE_UPLOAD.exists():
        log(f"YouTube投稿スキップ: upload_youtube.pyなし {YOUTUBE_UPLOAD}")
        return "", False

    YOUTUBE_STORAGE.mkdir(parents=True, exist_ok=True)
    source_article_url = job.get("tweet_url") or ""
    horizon_url = f"{HORIZONV_URL}?id={job_id}"
    description = (
        "Horizonで収集・要約したAI/Web3ニュースを、Kurageでショート動画化しました。\n\n"
        f"元記事:\n{source_article_url}\n\n"
        f"Horizon動画ページ:\n{horizon_url}\n\n"
        "バイブコーディングフレームワーク VWork\n"
        "https://katsushi2441.github.io/vwork/\n\n"
        "名古屋バイブコーディング経営革命\n"
        "https://xb-bittensor.hatenablog.com/\n\n"
        "株式会社エクスブリッジ\n"
        "https://exbridge.jp/"
    )
    json_out = YOUTUBE_STORAGE / f"horizon_{job_id}_response.json"
    cmd = [
        "python3", str(YOUTUBE_UPLOAD), str(video_path),
        "--title", title[:100],
        "--description", description,
        "--tags", "AI,LLM,Web3,Horizon,Kurage,AIニュース,バイブコーディング,NVIDIA,Cloudflare,MiniMax",
        "--privacy", "public",
        "--json-out", str(json_out),
    ]
    if thumbnail_path.exists():
        cmd += ["--thumbnail-intro", str(thumbnail_path), "--thumbnail-intro-seconds", "3.0"]
    youtube_pythonpath = os.pathsep.join(
        part for part in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        if part and "/python3.11/" not in part
    )
    ok = run_step(
        cmd,
        "YouTube動画投稿",
        timeout=900,
        extra_env={"PYTHONPATH": youtube_pythonpath},
        cwd=YOUTUBE_DIR,
    )
    if not ok:
        return "", False
    try:
        response = json.loads(json_out.read_text(encoding="utf-8"))
        video_id = response.get("id", "")
        if video_id:
            youtube_url = f"https://youtu.be/{video_id}"
            job["youtube_url"] = youtube_url
            job["youtube_video_id"] = video_id
            job["youtube_uploaded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_job(job_id, job)
            log(f"YouTube投稿完了: {youtube_url}")
            return youtube_url, True
    except Exception as exc:
        log(f"YouTube投稿結果読込失敗: {exc}")
    return "", False


def main():
    log("====== horizon_worker 開始 ======")
    ssh_sock = find_ssh_agent()
    log(f"SSH_AUTH_SOCK: {ssh_sock or '(未検出)'}")
    log(f"Python: {sys.executable}")
    log(f"PYTHONPATH: {os.environ.get('PYTHONPATH', '')}")

    report_worker("running", 0, "Horizonニュース収集中")
    today = date.today().strftime("%Y-%m-%d")
    article_post_id = ""
    video_post_id = ""
    articles_created = 0
    videos_created = 0
    youtube_uploaded = 0
    mail_posts = 0
    skipped_existing = 0
    failed = 0
    job_ids = []
    youtube_urls = []
    created_article = None

    # Step 1: Horizon summary生成
    ok = run_step(
        ["python3.11", "-m", "src.main"],
        "Horizon summary生成",
        timeout=3600,
        extra_env={"OLLAMA_API_KEY": "ollama"},
        cwd=HORIZON_DIR,
    )
    if not ok:
        report_worker("error", 0, "Horizon summary生成失敗")
        sys.exit(1)

    # Step 2: 記事投稿
    report_worker("running", 0, "記事投稿中")
    env = {**os.environ}
    if ssh_sock:
        env["SSH_AUTH_SOCK"] = ssh_sock
    ok = run_step(
        ["python3", "post_to_zenn.py", "--skip-horizon"],
        "Zenn記事投稿",
        timeout=300,
        extra_env={"SSH_AUTH_SOCK": ssh_sock} if ssh_sock else {},
    )
    if ok:
        created_article = parse_created_article(run_step.last_stdout)
        if created_article:
            articles_created += 1
            article_title = get_article_title(created_article)
            source_article_url = article_url(created_article)
            if post_article_to_hatena_blogger(created_article):
                mail_posts += 1
            else:
                failed += 1
            if article_title:
                article_post_id = post_to_sns(
                    f"📰 {article_title}\n\n"
                    f"Horizon-AIが収集したAI・Web3・スタートアップのニュースをまとめました。\n\n"
                    f"{source_article_url}\n\n"
                    f"株式会社エクスブリッジ https://exbridge.jp/"
                )
        else:
            skipped_existing += 1
            log("新規記事作成なし")
            created_article = latest_today_article()
            if created_article:
                log(f"既存記事で動画生成を続行: {created_article}")
    else:
        failed += 1

    # Step 3: 動画生成
    report_worker("running", videos_created, "動画生成中")
    video_done = False
    video_title = "AIニュース動画"
    youtube_url = ""
    if created_article:
        source_article_url = article_url(created_article)
        retry_max_values = [5, 3, 2]
        retry_count = int(os.environ.get("HORIZON_VIDEO_RETRIES", "3") or "3")
        for attempt, max_items in enumerate(retry_max_values[:max(1, retry_count)], 1):
            ok = run_step(
                ["python3", "generate_news_videos.py", "--article-url", source_article_url, "--max", str(max_items)],
                f"ニュース動画生成 attempt={attempt} max={max_items}",
                timeout=120,
            )
            if not ok:
                continue
            job_id = parse_job_id(run_step.last_stdout) or get_video_job_id_for_article(source_article_url)
            if not job_id:
                log("新規動画job_idなし")
                continue
            job_ids.append(job_id)
            log(f"動画job_id: {job_id} 完了待ち...")
            if wait_video_done(job_id):
                video_done = True
                break
            log(f"動画生成失敗のため再試行します: job_id={job_id}")
    else:
        log("新規記事がないため動画生成をスキップ")

    if video_done and job_ids:
        job_id = job_ids[-1]
        if job_id:
            videos_created += 1
            # 動画タイトル取得
            try:
                res = urllib.request.urlopen(f"{KURAGE_API}/status/{job_id}", timeout=10)
                video_data = json.loads(res.read())
                video_title = video_data.get("title", "AIニュース動画")
            except Exception:
                video_title = "AIニュース動画"

            if HORIZON_AUTO_YOUTUBE_UPLOAD:
                youtube_url, uploaded = upload_youtube(job_id, video_title)
                if uploaded:
                    youtube_uploaded += 1
                    youtube_urls.append(youtube_url)
                elif youtube_url:
                    skipped_existing += 1
            else:
                log("YouTube自動アップロードは無効です")

            youtube_block = f"YouTube:\n{youtube_url}\n\n" if youtube_url else ""
            video_post_id = post_to_sns(
                f"🎬 {video_title}\n\n"
                f"Horizon-AIニュースからKurageがショート動画を自動生成しました。\n\n"
                f"{HORIZONV_URL}?id={job_id}\n\n"
                f"{youtube_block}"
                f"株式会社エクスブリッジ https://exbridge.jp/"
            )
    elif created_article:
        failed += 1

    note = (
        f"articles_created={articles_created} videos_created={videos_created} "
        f"mail_posts={mail_posts} youtube_uploaded={youtube_uploaded} "
        f"youtube_auto_upload={'enabled' if HORIZON_AUTO_YOUTUBE_UPLOAD else 'disabled'} "
        f"skipped_existing={skipped_existing} failed={failed}"
    )
    if job_ids:
        note += f" job_ids={','.join(job_ids)}"
    if youtube_urls:
        note += f" youtube_urls={','.join(youtube_urls)}"
    sns_count = sum(1 for x in (article_post_id, video_post_id) if x)
    if sns_count:
        note += f" sns_posts={sns_count}"

    if videos_created >= 1 and failed == 0:
        report_worker("ok", videos_created, note)
    elif failed == 0:
        report_worker("warn", videos_created, note)
    else:
        report_worker("down", videos_created, note)
    log(f"====== horizon_worker 完了 ({note}) ======")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            if LOCK_PATH.exists():
                pid = LOCK_PATH.read_text(encoding="utf-8").strip()
                if pid == str(os.getpid()):
                    LOCK_PATH.unlink()
                    log("pid lock削除完了")
        except Exception as exc:
            log(f"pid lock削除失敗: {exc}")
