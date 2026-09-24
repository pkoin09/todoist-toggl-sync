"""Sync newly created Todoist tasks with Toggl Track time entries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, redirect, request
from urllib.parse import urlencode
from werkzeug.middleware.proxy_fix import ProxyFix


TODOIST_API_URL = "https://api.todoist.com/api/v1"
TOGGL_API_URL = "https://api.track.toggl.com/api/v9"
TODOIST_MARKER = re.compile(r"\[todoist:([^\]]+)]")
HTTP_TIMEOUT_SECONDS = 15
DEFAULT_TRIGGER_LABEL = "work"
DEFAULT_IDEMPOTENCY_DB_PATH = "data/webhook_deliveries.sqlite3"
DELIVERY_CLAIM_TTL_SECONDS = 300
OAUTH_STATE_TTL_SECONDS = 600

app = Flask(__name__)
# Railway terminates HTTPS before forwarding traffic to Gunicorn. Trust its
# forwarded scheme and host so OAuth redirect URLs retain the public HTTPS URL.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
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


def _trigger_label() -> str:
    return (
        os.getenv("TODOIST_TRIGGER_LABEL", DEFAULT_TRIGGER_LABEL)
        .removeprefix("@")
        .casefold()
    )


def _todoist_redirect_uri() -> str:
    # Todoist requires HTTPS callback URLs. Railway terminates TLS before the
    # request reaches Flask and does not consistently expose the public scheme.
    return f"https://{request.host}/oauth/todoist/callback"


def _oauth_state() -> str:
    payload = f"{int(time.time())}.{secrets.token_urlsafe(24)}"
    signature = hmac.new(
        _env("TODOIST_CLIENT_SECRET").encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def _oauth_state_is_valid(state: str | None) -> bool:
    if not state:
        return False
    try:
        timestamp, nonce, signature = state.split(".", 2)
        issued_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    if not nonce or not 0 <= time.time() - issued_at <= OAUTH_STATE_TTL_SECONDS:
        return False
    payload = f"{timestamp}.{nonce}"
    expected = hmac.new(
        _env("TODOIST_CLIENT_SECRET").encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _has_trigger_label(task: dict[str, Any]) -> bool:
    labels = task.get("labels", [])
    if not isinstance(labels, list):
        return False
    normalized = {
        str(label).removeprefix("@").casefold()
        for label in labels
        if isinstance(label, str)
    }
    return _trigger_label() in normalized


def _delivery_key(provider: str, identifier: Any, body: bytes) -> str:
    stable_id = (
        str(identifier)
        if identifier is not None
        else hashlib.sha256(body).hexdigest()
    )
    return f"{provider}:{stable_id}"


def _idempotency_connection() -> sqlite3.Connection:
    database_path = Path(
        os.getenv("IDEMPOTENCY_DB_PATH", DEFAULT_IDEMPOTENCY_DB_PATH)
    )
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=5)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_deliveries (
            delivery_key TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    return connection


def _claim_delivery(delivery_key: str) -> bool:
    """Atomically reserve a delivery, reclaiming abandoned work after five minutes."""
    now = time.time()
    stale_before = now - DELIVERY_CLAIM_TTL_SECONDS
    with _idempotency_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status, updated_at FROM webhook_deliveries WHERE delivery_key = ?",
            (delivery_key,),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO webhook_deliveries VALUES (?, 'processing', ?)",
                (delivery_key, now),
            )
            return True
        status, updated_at = row
        if status == "processing" and updated_at < stale_before:
            connection.execute(
                "UPDATE webhook_deliveries SET updated_at = ? WHERE delivery_key = ?",
                (now, delivery_key),
            )
            return True
        return False


def _complete_delivery(delivery_key: str) -> None:
    with _idempotency_connection() as connection:
        connection.execute(
            "UPDATE webhook_deliveries SET status = 'completed', updated_at = ? "
            "WHERE delivery_key = ?",
            (time.time(), delivery_key),
        )


def _release_delivery(delivery_key: str) -> None:
    with _idempotency_connection() as connection:
        connection.execute(
            "DELETE FROM webhook_deliveries "
            "WHERE delivery_key = ? AND status = 'processing'",
            (delivery_key,),
        )


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


@app.get("/oauth/todoist/start")
def todoist_oauth_start():
    try:
        query = urlencode(
            {
                "client_id": _env("TODOIST_CLIENT_ID"),
                "scope": "data:read_write",
                "state": _oauth_state(),
                "response_type": "code",
                "redirect_uri": _todoist_redirect_uri(),
            }
        )
    except RuntimeError as exc:
        logger.error("Todoist OAuth configuration error: %s", exc)
        return jsonify(error=str(exc)), 503
    return redirect(f"https://app.todoist.com/oauth/authorize?{query}")


@app.get("/oauth/todoist/callback")
def todoist_oauth_callback():
    try:
        if not _oauth_state_is_valid(request.args.get("state")):
            return jsonify(error="invalid or expired OAuth state"), 400
        code = request.args.get("code")
        if not code:
            return jsonify(error=request.args.get("error", "missing authorization code")), 400
        response = requests.post(
            "https://api.todoist.com/oauth/access_token",
            data={
                "client_id": _env("TODOIST_CLIENT_ID"),
                "client_secret": _env("TODOIST_CLIENT_SECRET"),
                "code": code,
                "redirect_uri": _todoist_redirect_uri(),
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except (RuntimeError, requests.RequestException) as exc:
        logger.exception("Could not complete Todoist OAuth authorization")
        return jsonify(error="Todoist authorization failed", detail=str(exc)), 502
    return jsonify(
        status="authorized",
        message="Todoist webhook activation is complete. You may close this page.",
    )


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
    if not _has_trigger_label(task):
        logger.info(
            "Ignored Todoist task %s without @%s label",
            task["id"],
            _trigger_label(),
        )
        return jsonify(status="ignored", reason="trigger label missing"), 200

    task_id = str(task["id"])
    delivery_key = _delivery_key(
        "todoist", request.headers.get("X-Todoist-Delivery-ID"), body
    )
    try:
        if not _claim_delivery(delivery_key):
            return jsonify(status="duplicate"), 200
        entry = _start_toggl_entry(task_id, str(task["content"]))
        _complete_delivery(delivery_key)
    except (
        RuntimeError,
        requests.RequestException,
        ValueError,
        sqlite3.Error,
        OSError,
    ) as exc:
        try:
            _release_delivery(delivery_key)
        except (sqlite3.Error, OSError):
            logger.exception("Could not release Todoist delivery claim")
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
    # A stopped entry can produce more than one distinct "updated" webhook
    # (for example, when its project or tags change later). Key the side effect
    # by the entry itself so its duration is reported to Todoist only once.
    time_entry_id = entry.get("id")
    delivery_key = _delivery_key(
        "toggl-time-entry",
        (
            f"{time_entry_id}:stopped"
            if time_entry_id is not None
            else payload.get("event_id")
        ),
        body,
    )
    try:
        if not _claim_delivery(delivery_key):
            return jsonify(status="duplicate"), 200
        _post_todoist_comment(
            task_id, f"Toggl time tracked: **{_format_duration(duration)}**"
        )
        _complete_delivery(delivery_key)
    except (
        RuntimeError,
        requests.RequestException,
        sqlite3.Error,
        OSError,
    ) as exc:
        try:
            _release_delivery(delivery_key)
        except (sqlite3.Error, OSError):
            logger.exception("Could not release Toggl delivery claim")
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
