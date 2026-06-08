#!/usr/bin/env python3.11
"""Horizon → VWork articles（Zenn）自動投稿スクリプト

Horizonでニュースを収集し、Zennフォーマットに変換してvworkリポジトリへpushする。
"""
import argparse
import glob
import os
import json
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

HORIZON_DIR = Path(os.environ.get("HORIZON_DIR") or (Path(__file__).parent / "Horizon"))
SUMMARIES_DIR = HORIZON_DIR / "data" / "summaries"
VWORK_DIR = Path(os.environ.get("VWORK_DIR", "/home/kojima/work/vwork"))
ARTICLES_DIR = VWORK_DIR / "articles"
KURAGE_JOBS_DIR = Path(os.environ.get("KURAGE_JOBS_DIR", "/home/kojima/work/kurage/storage/jobs"))
DASHBOARD_API = os.environ.get("DASHBOARD_API", "http://localhost:8081/worker/report")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://192.168.0.14:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e4b")


def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def find_ssh_agent() -> str:
    """SSH agentソケットを自動検出する"""
    for sock in glob.glob("/tmp/ssh-*/agent.*"):
        result = subprocess.run(
            ["ssh-add", "-l"],
            env={**os.environ, "SSH_AUTH_SOCK": sock},
            capture_output=True,
        )
        if result.returncode == 0:
            return sock
    return os.environ.get("SSH_AUTH_SOCK", "")


def report_worker(status: str, items: int, note: str = ""):
    import json, urllib.request
    if os.environ.get("POST_TO_ZENN_REPORT", "").lower() not in {"1", "true", "yes"}:
        return
    try:
        payload = json.dumps({"name": "horizon_zenn", "status": status, "items": items, "note": note}).encode()
        req = urllib.request.Request(DASHBOARD_API, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        log(f"dashboard report失敗: {exc}")


def translate_to_japanese(summary_text: str, post_date: str) -> str:
    """Ollamaを使ってHorizonのsummary（英語）を日本語記事に変換する"""
    import urllib.request, json

    # frontmatterを除去してテキスト部分だけ抽出
    if summary_text.startswith("---"):
        end = summary_text.find("---", 3)
        if end != -1:
            summary_text = summary_text[end + 3:].lstrip()

    prompt = f"""以下はHorizonが収集したAI・Web3・スタートアップのニュースまとめ（英語）です。
これを**日本語のブログ記事**に書き直してください。

要件:
- 記事全体のH1タイトルは、その日の上位ニュースの意味が伝わる具体的なタイトルにする
- H1タイトルには、元ニュースに出てくる企業名・個人名・製品名・モデル名などの固有名詞を1〜2個以上入れる
- 例: OpenAI、Sam Altman、Microsoft、NVIDIA、Google、Anthropic、Gemma、Qwen、Simon Willison など
- タイトル例: 「OpenAIとSam Altmanで読むAIインフラ競争」
- 「AI・Web3ニュースまとめ」のような汎用タイトルだけで終わらせない
- 「AIが描く未来」「AIの最前線」「AIが変える未来」のような抽象的なタイトルだけにしない
- 各見出しは日本語に翻訳する
- 各記事を2〜3文の日本語で要約する
- 重要なニュース上位5〜7件に絞る
- Markdown形式（## 見出し + 本文）で出力する
- スコアの高い順（⭐️ 9.0 > 8.0）を優先する
- 出力は日本語のみ。英語タイトルをそのまま使わない

元データ:
{summary_text[:3000]}

日本語記事:"""

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.5, "num_predict": 3000},
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as res:
        data = json.loads(res.read())
    return data.get("response", "").strip()


def run_horizon():
    """Horizonを実行してニュースを収集する"""
    log("Horizon 実行中...")
    env = {**os.environ, "OLLAMA_API_KEY": "ollama"}
    result = subprocess.run(
        ["python3.11", "-m", "src.main"],
        cwd=HORIZON_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.stdout:
        log("stdout: " + result.stdout[-2000:])
    if result.stderr:
        log("stderr: " + result.stderr[-1000:])
    if result.returncode != 0:
        raise RuntimeError(f"Horizon 実行失敗 (exit {result.returncode})")
    log("Horizon 完了")


def get_latest_summary() -> tuple[Path, str]:
    """最新のsummaryファイルを取得する"""
    today = date.today().strftime("%Y-%m-%d")
    for pattern in [f"horizon-{today}-ja.md", f"horizon-{today}-*.md"]:
        files = sorted(SUMMARIES_DIR.glob(pattern), reverse=True)
        if files:
            return files[0], today
    raise FileNotFoundError(f"今日のsummaryファイルが見つかりません: horizon-{today}-*.md")


STOP_TOPIC_TOKENS = {
    "https", "http", "www", "com", "the", "and", "with", "from", "that",
    "this", "into", "using", "uses", "lets", "new", "news", "blog", "study",
    "tool", "tools", "model", "models", "agent", "agents", "open", "source",
    "latest", "today", "daily", "summary", "content", "technology", "tech",
}


TOPIC_ALIASES = {
    "gemma4": ["gemma 4", "gemma4", "gemma", "ジェマ"],
    "llamacpp": ["llama.cpp", "llamacpp"],
    "mtp": ["mtp", "multi-threading", "tensor parallelism", "多重スレッド"],
    "fp8": ["fp8"],
    "qat": ["qat", "量子化アウェア", "量子化"],
    "qwen": ["qwen"],
    "deepswe": ["deepswe"],
    "sonnet": ["sonnet"],
    "avatar3d": ["3d avatar", "3dアバター", "アバター"],
    "language_control": ["language instead of buttons", "natural language", "自然言語", "言語による", "言葉"],
    "datasette": ["datasette", "構造化データ", "構造化されたデータ", "markdown", "sql", "svg"],
    "agent_edit": ["agent-edit", "編集能力", "データ編集", "編集者"],
    "career_rebuild": ["addiction", "prison", "felony", "依存症", "投獄", "重罪", "困難", "キャリア", "再起"],
    "locals": ["local llm", "localllama", "ローカルllm", "ローカルai", "ローカル環境"],
}


GENERIC_TITLE_PHRASES = (
    "AI・Web3ニュースまとめ",
    "AIニュースまとめ",
    "Web3ニュースまとめ",
    "AIが描く未来",
    "AIが変える未来",
    "AIの最前線",
    "最新AIニュース",
    "AIニュース日報",
)


PROPER_NOUN_STOPWORDS = {
    "Horizon", "Daily", "From", "Tags", "Discussion", "Jun", "Reddit",
    "HackerNews", "RSS", "LocalLLaMA", "LLM", "LLMs", "NLP", "AI",
    "ML", "Web3", "Startup", "Startups", "News", "Blog", "Medium",
    "Markdown", "SQL", "SVG",
}


KNOWN_PROPER_NOUNS = (
    "Sam Altman", "サム・アルトマン", "孫正義", "ビル・ゲイツ", "Bill Gates",
    "Elon Musk", "イーロン・マスク", "OpenAI", "Microsoft", "Google",
    "Anthropic", "NVIDIA", "Meta", "Apple", "Amazon", "Tesla", "xAI",
    "SoftBank", "Oracle", "Stargate", "Gemma 4", "Gemma4", "Qwen",
    "DeepSWE", "Sonnet", "Claude", "Llama", "llama.cpp", "Datasette",
    "datasette-agent-edit", "Simon Willison", "Helion", "Stellar",
    "MoneyGram", "AllUnity",
)


def normalize_topic_text(text: str) -> str:
    lowered = text.lower()
    extras = []
    for token, aliases in TOPIC_ALIASES.items():
        if any(alias.lower() in lowered for alias in aliases):
            extras.append(token)
    return text + ("\n" + " ".join(extras) if extras else "")


def topic_tokens(text: str) -> set[str]:
    tokens = set()
    text = normalize_topic_text(text)
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._+-]{2,}", text.lower()):
        token = token.strip("._+-")
        if len(token) < 3 or token in STOP_TOPIC_TOKENS:
            continue
        tokens.add(token)
    return tokens


def _candidate_score(name: str, text: str) -> tuple[int, int, int]:
    """固有名詞候補の並び順。出現回数、早さ、具体性を優先する。"""
    lower_text = text.lower()
    lower_name = name.lower()
    count = lower_text.count(lower_name)
    first = lower_text.find(lower_name)
    if first < 0:
        first = 10**9
    specificity = len(name.replace(" ", "").replace("・", ""))
    return (-count, first, -specificity)


def extract_proper_nouns(text: str, limit: int = 4) -> list[str]:
    """Horizon元データからタイトルに使える企業名・個人名・製品名を拾う。"""
    candidates: set[str] = set()

    for name in KNOWN_PROPER_NOUNS:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text, re.IGNORECASE):
            candidates.add(name)

    titleish_parts = []
    titleish_parts.extend(re.findall(r"(?m)^## \[([^\]]+)\]", text))
    titleish_parts.extend(re.findall(r"(?m)^\d+\.\s+\[([^\]]+)\]", text))
    titleish_parts.extend(re.findall(r"`([^`]+)`", text))
    titleish = "\n".join(titleish_parts)

    # CamelCase、英大文字始まり、数字入りモデル名、llama.cpp のような製品名を拾う。
    patterns = [
        r"\b[A-Z][A-Za-z0-9.+-]{2,}(?:\s+[A-Z0-9][A-Za-z0-9.+-]{1,}){0,2}\b",
        r"\b[A-Za-z]+[0-9][A-Za-z0-9._+-]*\b",
        r"\b[A-Za-z0-9._+-]*[A-Z][A-Za-z0-9._+-]*[0-9][A-Za-z0-9._+-]*\b",
        r"\b[a-z][a-z0-9]+(?:\.[a-z0-9]+)+\b",
        r"\b[a-z][a-z0-9]+(?:-[a-z0-9]+){1,}\b",
    ]
    for pattern in patterns:
        for raw in re.findall(pattern, titleish):
            name = raw.strip(" .,:;()[]")
            if len(name) < 3 or name in PROPER_NOUN_STOPWORDS:
                continue
            if name.lower() in {w.lower() for w in PROPER_NOUN_STOPWORDS}:
                continue
            candidates.add(name)

    # 日本語の人名らしい「氏」付き表現も拾う。
    for raw in re.findall(r"([一-鿿ァ-ンー・]{2,12})氏", text):
        if raw not in {"ユーザー", "著者"}:
            candidates.add(raw)

    lower_text = text.lower()
    if "llama.cpp" in lower_text:
        candidates.discard("Llama")
    if "gemma4" in lower_text or "gemma 4" in lower_text:
        candidates.discard("Gemma")
    if "datasette-agent-edit" in lower_text:
        candidates.discard("Datasette")

    ordered = sorted(candidates, key=lambda name: _candidate_score(name, text))
    return ordered[:limit]


def title_has_proper_noun(title: str, proper_nouns: list[str]) -> bool:
    lowered = title.lower()
    return any(name.lower() in lowered for name in proper_nouns)


def strengthen_title_with_proper_nouns(title: str, source_text: str) -> str:
    """生成タイトルが抽象的な場合、元ニュースの固有名詞を前に出す。"""
    proper_nouns = extract_proper_nouns(source_text)
    if not proper_nouns or title_has_proper_noun(title, proper_nouns):
        return title

    month_day = ""
    m = re.search(r"\s(\d{2}-\d{2})$", title)
    if m:
        month_day = m.group(1)
        title = title[:m.start()].strip()

    is_generic = any(phrase in title for phrase in GENERIC_TITLE_PHRASES)
    picked = proper_nouns[:2]
    prefix = "と".join(picked)
    if is_generic:
        strengthened = f"{prefix}で読むAIニュース"
    else:
        strengthened = f"{prefix}で読む：{title}"

    if len(strengthened) > 58 and len(picked) > 1:
        prefix = picked[0]
        strengthened = f"{prefix}で読む：{title}"
    if len(strengthened) > 68:
        strengthened = strengthened[:67].rstrip("：、・ ") + "…"
    return f"{strengthened} {month_day}".strip()


def collect_used_topic_text(post_date: str) -> str:
    parts = []
    for path in sorted(ARTICLES_DIR.glob(f"{post_date}-ai-news*.md")):
        try:
            parts.append(path.read_text(encoding="utf-8"))
        except Exception:
            continue
    prefix = f"https://katsushi2441.github.io/vwork/articles/{post_date}-ai-news"
    if KURAGE_JOBS_DIR.exists():
        for meta_path in sorted(KURAGE_JOBS_DIR.glob("*.json")):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            tweet_url = str(data.get("tweet_url") or "")
            if not tweet_url.startswith(prefix):
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


def filter_used_summary_sections(summary_text: str, post_date: str) -> str:
    used_text = collect_used_topic_text(post_date)
    if not used_text.strip():
        return summary_text
    used_lower = used_text.lower()
    used_tokens = topic_tokens(used_text)
    if not used_tokens:
        return summary_text

    sections = re.split(r"(?=\n?## \[)", summary_text)
    if len(sections) == 1:
        return summary_text
    kept = []
    removed = 0
    for section in sections:
        if not section.lstrip().startswith("## ["):
            kept.append(section)
            continue
        first_line = section.splitlines()[0]
        title = re.sub(r"^\n?##\s*", "", first_line)
        title = re.sub(r"\]\([^)]+\)", "]", title)
        title = title.strip("[] ")
        section_tokens = topic_tokens(title + "\n" + section[:1600])
        overlap = section_tokens & used_tokens
        exact = title.lower() and title.lower() in used_lower
        strong_overlap = {
            token for token in overlap
            if token in TOPIC_ALIASES or len(token) >= 7
        }
        if exact or len(strong_overlap) >= 1 or (len(overlap) >= 2 and len(overlap) / max(1, len(section_tokens)) >= 0.18):
            removed += 1
            log(f"同日既存記事との重複を除外: {title}")
            continue
        kept.append(section)
    if removed:
        log(f"同日重複除外: {removed}件")
    return "".join(kept)


def extract_h1_title(body: str, post_date: str, source_text: str = "") -> str:
    """本文のH1見出しをタイトルとして抽出する。なければ日付ベースのタイトルを返す。"""
    import re
    m = re.search(r'^#\s+(.+)$', body, re.MULTILINE)
    if m:
        title = re.sub(r'^[^\w　-鿿]+', '', m.group(1)).strip()
        if title in ("AI・Web3ニュースまとめ", "AIニュースまとめ", "Web3ニュースまとめ") or any(
            phrase in title for phrase in GENERIC_TITLE_PHRASES
        ):
            headings = re.findall(r'^##\s+(.+)$', body, re.MULTILINE)
            cleaned = [re.sub(r'^[^\w　-鿿]+', '', h).strip() for h in headings]
            cleaned = [h.split('**', 1)[0].strip() for h in cleaned if h]
            if cleaned:
                title = "・".join(cleaned[:2])
        # 日付を付加（例: 05-29）
        month_day = post_date[5:]  # "2026-05-29" → "05-29"
        return strengthen_title_with_proper_nouns(f"{title} {month_day}", source_text)
    return strengthen_title_with_proper_nouns(f"AI・Web3ニュースまとめ {post_date[5:]}", source_text)


def to_zenn_markdown(summary_text: str, post_date: str) -> tuple[str, str]:
    """HorizonのMarkdownをOllamaで日本語訳してZennフォーマットに変換する。
    Returns: (markdown全文, 記事タイトル)
    """
    log("Ollamaで日本語に変換中...")
    body = translate_to_japanese(summary_text, post_date)
    article_title = extract_h1_title(body, post_date, summary_text)
    log(f"記事タイトル: {article_title}")

    frontmatter = f"""---
title: "{article_title}"
emoji: "📰"
type: "tech"
topics: ["ai", "llm", "vibecoding", "web3", "startup"]
published: true
---

> 本記事はHorizonを使いAI/LLM・バイブコーディング・Web3・スタートアップのニュースを自動収集・要約したものです。

"""
    return frontmatter + body, article_title


def update_articles_index(article_path: Path, post_date: str, title: str):
    """articles.md のリンク一覧の先頭に新記事を追加する（重複チェック付き）"""
    articles_md = VWORK_DIR / "articles.md"
    content = articles_md.read_text(encoding="utf-8")
    slug = article_path.stem
    new_link = f"- [{title}]({slug}.html)\n"
    # 既に存在する場合はスキップ
    if slug + ".html" in content:
        log(f"articles.md: {slug} は既に存在するためスキップ")
        return
    insert_pos = content.find("- [")
    if insert_pos == -1:
        content += "\n" + new_link
    else:
        content = content[:insert_pos] + new_link + content[insert_pos:]
    articles_md.write_text(content, encoding="utf-8")
    log(f"articles.md 更新: {new_link.strip()}")


def next_article_path(post_date: str) -> Path:
    """同じ日に複数本投稿できるよう、空いているslugを返す。"""
    base = ARTICLES_DIR / f"{post_date}-ai-news.md"
    if not base.exists():
        return base
    for i in range(2, 100):
        candidate = ARTICLES_DIR / f"{post_date}-ai-news-{i}.md"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"記事slugの空きがありません: {post_date}-ai-news-*")


def push_to_vwork(article_path: Path, post_date: str, ssh_sock: str, dry_run: bool, title: str = ""):
    """vworkリポジトリへgit pushする"""
    env = {**os.environ}
    if ssh_sock:
        env["SSH_AUTH_SOCK"] = ssh_sock

    def git(args, **kwargs):
        return subprocess.run(["git"] + args, cwd=VWORK_DIR, env=env, check=True, capture_output=True, text=True, **kwargs)

    git(["pull", "origin", "main"])
    update_articles_index(article_path, post_date, title or f"AIニュース日報 {post_date}")
    git(["add", str(article_path), str(VWORK_DIR / "articles.md")])
    git(["commit", "-m", f"Add AI news digest {post_date} (Horizon auto-post)"])

    if dry_run:
        log("--dry-run: git push をスキップ")
        git(["reset", "HEAD~1"])
    else:
        git(["push", "origin", "main"])
        log(f"push完了: articles/{article_path.name}")


def main():
    parser = argparse.ArgumentParser(description="Horizon → Zenn自動投稿")
    parser.add_argument("--dry-run", action="store_true", help="git pushしない（動作確認用）")
    parser.add_argument("--skip-horizon", action="store_true", help="Horizon実行をスキップ（既存summaryを使用）")
    args = parser.parse_args()

    ssh_sock = find_ssh_agent()
    log(f"SSH_AUTH_SOCK: {ssh_sock or '(未検出)'}")

    report_worker("running", 0, "ニュース収集中")

    try:
        if not args.skip_horizon:
            run_horizon()
        else:
            log("Horizon実行スキップ（既存summaryを使用）")

        summary_path, post_date = get_latest_summary()
        log(f"summary: {summary_path}")

        summary_text = summary_path.read_text(encoding="utf-8")
        summary_text = filter_used_summary_sections(summary_text, post_date)
        remaining_sections = re.findall(r"(?m)^## \[", summary_text)
        if not remaining_sections:
            raise RuntimeError("同日既存記事と重複しないsummary項目が残っていません")

        article_path = next_article_path(post_date)
        log(f"投稿先記事: {article_path.name}")

        zenn_md, article_title = to_zenn_markdown(summary_text, post_date)

        if args.dry_run:
            log("--- dry-run: 生成されたMarkdown ---")
            print(zenn_md[:1000])
            log("--- end ---")
            return

        ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
        article_path.write_text(zenn_md, encoding="utf-8")
        log(f"記事作成: {article_path}")

        push_to_vwork(article_path, post_date, ssh_sock, args.dry_run, title=article_title)
        report_worker("ok", 1, f"投稿完了 {post_date}")
        log("完了")

    except Exception as exc:
        log(f"エラー: {exc}")
        report_worker("error", 0, str(exc)[:100])
        sys.exit(1)


if __name__ == "__main__":
    main()
