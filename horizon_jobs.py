from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_HORIZON_RUN_WORKER_API = "https://aixec.exbridge.jp/api.php?path=horizon/run-worker"
DEFAULT_TIMEOUT = 120


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
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "rqdb4ai-horizon/0.1"},
        method="POST",
    )
    timeout = int(kwargs.get("timeout") or os.environ.get("AIXEC_HORIZON_TRIGGER_TIMEOUT", DEFAULT_TIMEOUT))
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

    trigger_started = bool(response.get("ok"))
    if not trigger_started:
        raise RuntimeError(f"Horizon trigger API did not start worker: {response}")

    finished_at = dt.datetime.now(dt.timezone.utc)
    note_parts = ["trigger_started=true", "actual_items=reported_by_horizon_worker"]
    if response.get("job_id"):
        note_parts.append(f"job_id={response.get('job_id')}")
    if response.get("message"):
        note_parts.append(str(response.get("message")))
    return {
        "status": "ok",
        "created_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "dry_run": bool(dry_run),
        "url": url,
        "source": payload["source"],
        "http_status": status_code,
        "trigger_started": True,
        "items": 0,
        "note": " / ".join(note_parts),
        "response": response,
    }
