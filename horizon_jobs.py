from __future__ import annotations

import datetime as dt
import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_HORIZON_RUN_WORKER_API = "https://aixec.exbridge.jp/api.php?path=horizon/run-worker"
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


def _worker_status() -> dict[str, Any]:
    url = "https://aixec.exbridge.jp/api.php?path=worker/status"
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
    """Trigger the WEB/API-side Horizon worker. RQDB4AI does not run Horizon work."""
    started_at = dt.datetime.now(dt.timezone.utc)
    url = str(
        kwargs.get("submit_url")
        or kwargs.get("run_worker_url")
        or os.environ.get("AIXEC_HORIZON_RUN_WORKER_API")
        or DEFAULT_HORIZON_RUN_WORKER_API
    )
    token = (
        kwargs.get("api_token")
        or kwargs.get("AIXEC_API_TOKEN")
        or kwargs.get("aixec_api_token")
        or os.environ.get("AIXEC_HORIZON_API_TOKEN")
        or os.environ.get("AIXEC_API_TOKEN")
    )
    if not token:
        raise RuntimeError("AIXEC_API_TOKEN is required to trigger Horizon worker")

    payload = {
        "api_token": str(token),
        "dry_run": bool(dry_run),
        "source": str(kwargs.get("source") or "rqdb4ai"),
    }
    timeout = int(kwargs.get("timeout") or os.environ.get("AIXEC_HORIZON_TRIGGER_TIMEOUT", DEFAULT_TIMEOUT))
    status_code, response = _post_json(url, payload, timeout)

    trigger_started = bool(response.get("ok"))
    if not trigger_started:
        raise RuntimeError(f"Horizon trigger API did not start worker: {response}")
    if dry_run:
        result = response.get("result") if isinstance(response, dict) else None
        return _standard_result(
            ok=True,
            status="ok",
            items=0,
            metrics={"created": 0, "dry_run": 1},
            note=f"dry_run check running={bool(result.get('running')) if isinstance(result, dict) else False}",
            **{
                "created_at": started_at.isoformat(),
                "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "dry_run": True,
                "url": url,
                "source": payload["source"],
                "http_status": status_code,
                "trigger_started": False,
                "response": response,
            },
        )

    wait_timeout = int(kwargs.get("wait_timeout") or os.environ.get("AIXEC_HORIZON_WAIT_TIMEOUT", DEFAULT_WAIT_TIMEOUT))
    poll_interval = int(kwargs.get("poll_interval") or os.environ.get("AIXEC_HORIZON_POLL_INTERVAL", DEFAULT_POLL_INTERVAL))
    deadline = time.monotonic() + max(1, wait_timeout)
    last_check: dict[str, Any] = {}
    worker_status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        check_payload = {
            "api_token": str(token),
            "dry_run": True,
            "check_only": True,
            "source": payload["source"],
        }
        _, last_check = _post_json(url, check_payload, timeout)
        result = last_check.get("result") if isinstance(last_check, dict) else None
        api_running = bool(result.get("running")) if isinstance(result, dict) else False
        worker_status = _worker_status()
        status_running = str(worker_status.get("status") or "").lower() == "running"
        running = api_running or status_running
        if not running:
            break
        time.sleep(max(1, poll_interval))
    else:
        raise TimeoutError(f"Horizon worker still running after {wait_timeout} seconds: api={last_check} status={worker_status}")

    worker_status = worker_status or _worker_status()
    business_status = str(worker_status.get("status") or "unknown")
    items = int(worker_status.get("items") or 0)
    finished_at = dt.datetime.now(dt.timezone.utc)
    note_parts = [f"worker_status={business_status}", f"items={items}"]
    if response.get("job_id"):
        note_parts.append(f"job_id={response.get('job_id')}")
    if response.get("message"):
        note_parts.append(str(response.get("message")))
    if worker_status.get("note"):
        note_parts.append(str(worker_status.get("note")))
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
            "url": url,
            "source": payload["source"],
            "http_status": status_code,
            "trigger_started": True,
            "response": response,
            "last_check": last_check,
            "worker_status": worker_status,
        },
    )
