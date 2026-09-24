"""Sync newly created Todoist tasks with Toggl Track time entries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import requests
from flask import Flask, jsonify, request


TODOIST_API_URL = "https://api.todoist.com/api/v1"
TOGGL_API_URL = "https://api.track.toggl.com/api/v9"
TODOIST_MARKER = re.compile(r"\[todoist:([^\]]+)]")
HTTP_TIMEOUT_SECONDS = 15

app = Flask(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("todoist_toggl_sync")


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _todoist_signature_is_valid(body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    digest = hmac.new(
        _env("TODOIST_CLIENT_SECRET").encode(), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def _toggl_signature_is_valid(body: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(
        _env("TOGGL_WEBHOOK_SECRET").encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", signature)


def _start_toggl_entry(task_id: str, task_content: str) -> dict[str, Any]:
    workspace_id = _env("TOGGL_WORKSPACE_ID")
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    response = requests.post(
        f"{TOGGL_API_URL}/workspaces/{workspace_id}/time_entries",
        auth=(_env("TOGGL_API_TOKEN"), "api_token"),
        json={
            "created_with": "todoist-toggl-sync",
            "description": f"{task_content} [todoist:{task_id}]",
            "duration": -1,
            "start": now,
            "stop": None,
            "workspace_id": int(workspace_id),
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def _post_todoist_comment(task_id: str, content: str) -> dict[str, Any]:
    response = requests.post(
        f"{TODOIST_API_URL}/comments",
        headers={"Authorization": f"Bearer {_env('TODOIST_TOKEN')}"},
        json={"task_id": task_id, "content": content},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def _unwrap_toggl_entry(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Handle the webhook envelope as well as direct entry payloads in tests."""
    candidate: Any = payload.get("payload", payload)
    if isinstance(candidate, dict) and isinstance(candidate.get("data"), dict):
        candidate = candidate["data"]
    return candidate if isinstance(candidate, dict) else None


def _duration_seconds(entry: dict[str, Any]) -> int | None:
    duration = entry.get("duration")
    if isinstance(duration, (int, float)) and duration >= 0:
        return round(duration)

    start, stop = entry.get("start"), entry.get("stop")
    if not start or not stop:
        return None
    try:
        start_at = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        stop_at = datetime.fromisoformat(str(stop).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, round((stop_at - start_at).total_seconds()))


def _format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


@app.get("/")
@app.get("/health")
def health():
    return jsonify(status="ok")


@app.post("/webhooks/todoist")
def todoist_webhook():
    body = request.get_data(cache=True)
    try:
        valid = _todoist_signature_is_valid(
            body, request.headers.get("X-Todoist-Hmac-SHA256")
        )
    except RuntimeError as exc:
        logger.error("Todoist webhook configuration error: %s", exc)
        return jsonify(error=str(exc)), 503
    if not valid:
        logger.warning("Rejected Todoist webhook with invalid signature")
        return jsonify(error="invalid signature"), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="invalid JSON payload"), 400
    if payload.get("event_name") != "item:added":
        return jsonify(status="ignored"), 200

    task = payload.get("event_data")
    if not isinstance(task, dict) or not task.get("id") or not task.get("content"):
        return jsonify(error="missing task id or content"), 400

    task_id = str(task["id"])
    try:
        entry = _start_toggl_entry(task_id, str(task["content"]))
    except (RuntimeError, requests.RequestException, ValueError) as exc:
        logger.exception("Could not start Toggl timer for Todoist task %s", task_id)
        return jsonify(error=str(exc)), 502

    logger.info(
        "Started Toggl time entry %s for Todoist task %s", entry.get("id"), task_id
    )
    return jsonify(status="started", time_entry_id=entry.get("id")), 200


@app.post("/webhooks/toggl")
def toggl_webhook():
    body = request.get_data(cache=True)
    try:
        valid = _toggl_signature_is_valid(
            body, request.headers.get("X-Webhook-Signature-256")
        )
    except RuntimeError as exc:
        logger.error("Toggl webhook configuration error: %s", exc)
        return jsonify(error=str(exc)), 503
    if not valid:
        logger.warning("Rejected Toggl webhook with invalid signature")
        return jsonify(error="invalid signature"), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="invalid JSON payload"), 400
    if payload.get("payload") == "ping":
        return jsonify(status="ok"), 200

    entry = _unwrap_toggl_entry(payload)
    if entry is None:
        return jsonify(status="ignored"), 200

    description = str(entry.get("description") or "")
    match = TODOIST_MARKER.search(description)
    duration = _duration_seconds(entry)
    if not match or duration is None or not entry.get("stop"):
        return jsonify(status="ignored"), 200

    task_id = match.group(1)
    try:
        _post_todoist_comment(
            task_id, f"Toggl time tracked: **{_format_duration(duration)}**"
        )
    except (RuntimeError, requests.RequestException) as exc:
        logger.exception("Could not comment on Todoist task %s", task_id)
        return jsonify(error=str(exc)), 502

    logger.info(
        "Posted duration for Toggl time entry %s to Todoist task %s",
        entry.get("id"),
        task_id,
    )
    return jsonify(status="commented", task_id=task_id), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
