from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
from typing import Any
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_HORIZON_RUN_WORKER_API = "https://aixec.exbridge.jp/api.php?path=horizon/run-worker"
DEFAULT_WORKER_STATUS_API = "http://192.168.0.14:8081/worker/status"
DEFAULT_TIMEOUT = 120
DEFAULT_POLL_INTERVAL = 30
DEFAULT_WAIT_TIMEOUT = 3600


def _standard_result(
    *,
    ok: bool,
    status: str,
    items: int = 0,
    metrics: dict[str, Any] | None = None,
    note: str = "",
    artifacts: list[dict[str, Any]] | None = None,
    error: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    result = {
        "ok": bool(ok),
        "status": status,
        "items": int(items or 0),
        "metrics": metrics or {},
        "note": note,
        "artifacts": artifacts or [],
        "error": error,
    }
    result.update(extra)
    return result


def _post_json(url: str, payload: dict[str, Any], timeout: int) -> tuple[int | None, dict[str, Any]]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "rqdb4ai-horizon/0.1"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", errors="replace")
            status_code = getattr(res, "status", None)
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Horizon trigger API failed http_status={exc.code} body={raw[:1000]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Horizon trigger API network error: {exc}") from exc

    try:
        response = json.loads(raw)
    except Exception:
        response = {"raw": raw}
    return status_code, response


def _worker_status(url: str | None = None) -> dict[str, Any]:
    url = url or os.environ.get("AIXEC_WORKER_STATUS_API") or DEFAULT_WORKER_STATUS_API
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "rqdb4ai-horizon/0.1"})
    try:
        with urlopen(req, timeout=30) as res:
            data = json.loads(res.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"status": "unknown", "items": 0, "note": f"worker/status unavailable: {exc}"}
    workers = data.get("workers") if isinstance(data, dict) else None
    if isinstance(workers, dict):
        item = workers.get("horizon-worker-enqueue")
        if isinstance(item, dict):
            return item
    return {"status": "unknown", "items": 0, "note": "horizon-worker-enqueue not found"}


def worker_auto_cycle_job(dry_run: bool = False, **kwargs: Any) -> dict[str, Any]:
    """Run Horizon worker directly on the RQDB4AI worker host."""
    started_at = dt.datetime.now(dt.timezone.utc)
    repo_dir = Path(
        str(
            kwargs.get("horizon_dir")
            or os.environ.get("HORIZON_WORKER_DIR")
            or Path(__file__).resolve().parent
        )
    )
    worker = repo_dir / "horizon_worker.py"
    status_api = str(kwargs.get("worker_status_api") or os.environ.get("AIXEC_WORKER_STATUS_API") or DEFAULT_WORKER_STATUS_API)
    source = str(kwargs.get("source") or "rqdb4ai")

    if not worker.exists():
        raise FileNotFoundError(str(worker))

    if dry_run:
        return _standard_result(
            ok=True,
            status="ok",
            items=0,
            metrics={"created": 0, "dry_run": 1},
            note=f"dry_run direct worker exists path={worker}",
            **{
                "created_at": started_at.isoformat(),
                "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "dry_run": True,
                "source": source,
                "horizon_dir": str(repo_dir),
                "worker": str(worker),
                "worker_status_api": status_api,
            },
        )

    wait_timeout = int(kwargs.get("wait_timeout") or os.environ.get("AIXEC_HORIZON_WAIT_TIMEOUT", DEFAULT_WAIT_TIMEOUT))
    env = dict(os.environ)
    env.setdefault("OLLAMA_API_KEY", str(kwargs.get("ollama_api_key") or "ollama"))
    env.setdefault("KURAGE_API", str(kwargs.get("kurage_api") or os.environ.get("KURAGE_API") or "http://exbridge.ddns.net:18200"))
    env.setdefault("DASHBOARD_API", str(kwargs.get("dashboard_api") or os.environ.get("DASHBOARD_API") or "http://192.168.0.14:8081/worker/report"))
    env.setdefault("AIXSNS_API", str(kwargs.get("aixsns_api") or os.environ.get("AIXSNS_API") or "https://aixec.exbridge.jp/api.php?path=posts"))
    if kwargs.get("youtube_dir"):
        env["YOUTUBE_DIR"] = str(kwargs["youtube_dir"])

    proc = subprocess.run(
        ["python3", str(worker)],
        cwd=str(repo_dir),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=wait_timeout,
    )
    output = proc.stdout or ""
    worker_status = _worker_status(status_api)
    business_status = str(worker_status.get("status") or "unknown")
    items = int(worker_status.get("items") or 0)
    finished_at = dt.datetime.now(dt.timezone.utc)
    note_parts = [f"worker_status={business_status}", f"items={items}", f"exit={proc.returncode}"]
    if worker_status.get("note"):
        note_parts.append(str(worker_status.get("note")))
    if proc.returncode != 0:
        raise RuntimeError("Horizon worker process failed: " + " / ".join(note_parts) + "\n" + output[-4000:])
    if business_status not in {"ok", "warn", "warning"}:
        raise RuntimeError("Horizon worker did not finish successfully: " + " / ".join(note_parts))
    if business_status == "ok" and items != 1:
        raise RuntimeError("Horizon worker finished without completed item: " + " / ".join(note_parts))
    metrics = {
        "created": items,
        "articles_created": items,
        "videos_created": items,
        "youtube_uploaded": items,
    }
    note_text = " / ".join(note_parts)
    artifacts = []
    note = str(worker_status.get("note") or "")
    for token_part in note.split():
        if token_part.startswith("youtube_urls="):
            for url_part in token_part.replace("youtube_urls=", "").split(","):
                if url_part.startswith("http"):
                    artifacts.append({"type": "url", "label": "youtube", "url": url_part})
    return _standard_result(
        ok=True,
        status="warn" if business_status in {"warn", "warning"} else "ok",
        items=items,
        metrics=metrics,
        note=note_text,
        artifacts=artifacts,
        **{
            "created_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "dry_run": bool(dry_run),
            "source": source,
            "horizon_dir": str(repo_dir),
            "worker": str(worker),
            "exit_code": proc.returncode,
            "output_tail": output[-4000:],
            "worker_status": worker_status,
        },
    )
