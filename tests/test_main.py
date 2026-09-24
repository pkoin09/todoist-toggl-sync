import base64
import hashlib
import hmac
import json
from unittest.mock import Mock, patch

import pytest

import main


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv("TODOIST_TOKEN", "todoist-token")
    monkeypatch.setenv("TODOIST_CLIENT_ID", "todoist-client")
    monkeypatch.setenv("TODOIST_CLIENT_SECRET", "todoist-secret")
    monkeypatch.setenv("TOGGL_API_TOKEN", "toggl-token")
    monkeypatch.setenv("TOGGL_WORKSPACE_ID", "123")
    monkeypatch.setenv("TOGGL_WEBHOOK_SECRET", "toggl-secret")
    monkeypatch.setenv("IDEMPOTENCY_DB_PATH", str(tmp_path / "deliveries.sqlite3"))


@pytest.fixture
def client():
    main.app.config.update(TESTING=True)
    return main.app.test_client()


def todoist_signature(body: bytes) -> str:
    digest = hmac.new(b"todoist-secret", body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def toggl_signature(body: bytes) -> str:
    digest = hmac.new(b"toggl-secret", body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def post_signed(client, path, payload, header, signer, extra_headers=None):
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {header: signer(body), **(extra_headers or {})}
    return client.post(
        path, data=body, content_type="application/json", headers=headers
    )


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json == {"status": "ok"}


def test_todoist_oauth_start(client):
    response = client.get("/oauth/todoist/start")
    assert response.status_code == 302
    assert response.location.startswith("https://app.todoist.com/oauth/authorize?")
    assert "client_id=todoist-client" in response.location
    assert "scope=data%3Aread_write" in response.location


@patch("main.requests.post")
def test_todoist_oauth_callback_exchanges_code(request_post, client):
    upstream = Mock()
    upstream.raise_for_status.return_value = None
    request_post.return_value = upstream
    state = main._oauth_state()

    response = client.get(
        "/oauth/todoist/callback", query_string={"code": "auth-code", "state": state}
    )

    assert response.status_code == 200
    assert response.json["status"] == "authorized"
    assert request_post.call_args.kwargs["data"]["code"] == "auth-code"


def test_todoist_oauth_callback_rejects_invalid_state(client):
    response = client.get(
        "/oauth/todoist/callback",
        query_string={"code": "auth-code", "state": "invalid"},
    )
    assert response.status_code == 400


def test_rejects_invalid_signatures(client):
    assert client.post("/webhooks/todoist", json={}).status_code == 401
    assert client.post("/webhooks/toggl", json={}).status_code == 401


@patch("main.requests.post")
def test_todoist_task_starts_toggl_timer(request_post, client):
    upstream = Mock()
    upstream.raise_for_status.return_value = None
    upstream.json.return_value = {"id": 456}
    request_post.return_value = upstream
    payload = {
        "event_name": "item:added",
        "event_data": {"id": "task-1", "content": "Ship it", "labels": ["work"]},
    }
    response = post_signed(
        client, "/webhooks/todoist", payload, "X-Todoist-Hmac-SHA256", todoist_signature
    )
    assert response.status_code == 200
    assert response.json == {"status": "started", "time_entry_id": 456}
    kwargs = request_post.call_args.kwargs
    assert kwargs["auth"] == ("toggl-token", "api_token")
    assert kwargs["json"]["description"] == "Ship it [todoist:task-1]"
    assert kwargs["json"]["duration"] == -1


@patch("main.requests.post")
def test_todoist_task_without_work_label_is_ignored(request_post, client):
    payload = {
        "event_name": "item:added",
        "event_data": {"id": "task-2", "content": "Buy milk", "labels": ["home"]},
    }
    response = post_signed(
        client, "/webhooks/todoist", payload, "X-Todoist-Hmac-SHA256", todoist_signature
    )
    assert response.status_code == 200
    assert response.json == {"status": "ignored", "reason": "trigger label missing"}
    request_post.assert_not_called()


@patch("main.requests.post")
def test_todoist_trigger_label_is_configurable(request_post, client, monkeypatch):
    monkeypatch.setenv("TODOIST_TRIGGER_LABEL", "@client")
    upstream = Mock()
    upstream.raise_for_status.return_value = None
    upstream.json.return_value = {"id": 789}
    request_post.return_value = upstream
    payload = {
        "event_name": "item:added",
        "event_data": {"id": "task-3", "content": "Client call", "labels": ["Client"]},
    }
    response = post_signed(
        client, "/webhooks/todoist", payload, "X-Todoist-Hmac-SHA256", todoist_signature
    )
    assert response.status_code == 200
    assert response.json == {"status": "started", "time_entry_id": 789}


@patch("main.requests.post")
def test_duplicate_todoist_delivery_only_starts_one_timer(request_post, client):
    upstream = Mock()
    upstream.raise_for_status.return_value = None
    upstream.json.return_value = {"id": 456}
    request_post.return_value = upstream
    payload = {
        "event_name": "item:added",
        "event_data": {"id": "task-1", "content": "Ship it", "labels": ["work"]},
    }
    headers = {"X-Todoist-Delivery-ID": "delivery-1"}

    first = post_signed(
        client,
        "/webhooks/todoist",
        payload,
        "X-Todoist-Hmac-SHA256",
        todoist_signature,
        headers,
    )
    second = post_signed(
        client,
        "/webhooks/todoist",
        payload,
        "X-Todoist-Hmac-SHA256",
        todoist_signature,
        headers,
    )

    assert first.json["status"] == "started"
    assert second.json == {"status": "duplicate"}
    request_post.assert_called_once()


@patch("main.requests.post")
def test_stopped_toggl_entry_posts_todoist_comment(request_post, client):
    upstream = Mock()
    upstream.raise_for_status.return_value = None
    upstream.json.return_value = {"id": "comment-1"}
    request_post.return_value = upstream
    payload = {
        "payload": {
            "id": 456,
            "description": "Ship it [todoist:task-1]",
            "duration": 3723,
            "start": "2026-01-01T10:00:00Z",
            "stop": "2026-01-01T11:02:03Z",
        }
    }
    response = post_signed(
        client, "/webhooks/toggl", payload, "X-Webhook-Signature-256", toggl_signature
    )
    assert response.status_code == 200
    assert response.json == {"status": "commented", "task_id": "task-1"}
    kwargs = request_post.call_args.kwargs
    assert kwargs["headers"] == {"Authorization": "Bearer todoist-token"}
    assert kwargs["json"] == {
        "task_id": "task-1",
        "content": "Toggl time tracked: **1h 2m 3s**",
    }


@patch("main.requests.post")
def test_duplicate_toggl_event_only_posts_one_comment(request_post, client):
    upstream = Mock()
    upstream.raise_for_status.return_value = None
    upstream.json.return_value = {"id": "comment-1"}
    request_post.return_value = upstream
    payload = {
        "event_id": 999,
        "payload": {
            "id": 456,
            "description": "Ship it [todoist:task-1]",
            "duration": 60,
            "start": "2026-01-01T10:00:00Z",
            "stop": "2026-01-01T10:01:00Z",
        },
    }

    first = post_signed(
        client, "/webhooks/toggl", payload, "X-Webhook-Signature-256", toggl_signature
    )
    second = post_signed(
        client, "/webhooks/toggl", payload, "X-Webhook-Signature-256", toggl_signature
    )

    assert first.json["status"] == "commented"
    assert second.json == {"status": "duplicate"}
    request_post.assert_called_once()


@patch("main.requests.post")
def test_later_update_to_stopped_entry_does_not_post_another_comment(
    request_post, client
):
    upstream = Mock()
    upstream.raise_for_status.return_value = None
    upstream.json.return_value = {"id": "comment-1"}
    request_post.return_value = upstream
    entry = {
        "id": 456,
        "description": "Ship it [todoist:task-1]",
        "duration": 60,
        "start": "2026-01-01T10:00:00Z",
        "stop": "2026-01-01T10:01:00Z",
    }

    first = post_signed(
        client,
        "/webhooks/toggl",
        {"event_id": 1001, "payload": entry},
        "X-Webhook-Signature-256",
        toggl_signature,
    )
    second = post_signed(
        client,
        "/webhooks/toggl",
        {"event_id": 1002, "payload": {**entry, "tags": ["edited-later"]}},
        "X-Webhook-Signature-256",
        toggl_signature,
    )

    assert first.json["status"] == "commented"
    assert second.json == {"status": "duplicate"}
    request_post.assert_called_once()


@patch("main.requests.post")
def test_failed_delivery_is_released_for_retry(request_post, client):
    failed = Mock()
    failed.raise_for_status.side_effect = main.requests.HTTPError("temporary failure")
    succeeded = Mock()
    succeeded.raise_for_status.return_value = None
    succeeded.json.return_value = {"id": 456}
    request_post.side_effect = [failed, succeeded]
    payload = {
        "event_name": "item:added",
        "event_data": {"id": "task-1", "content": "Ship it", "labels": ["work"]},
    }
    headers = {"X-Todoist-Delivery-ID": "retryable-delivery"}

    first = post_signed(
        client,
        "/webhooks/todoist",
        payload,
        "X-Todoist-Hmac-SHA256",
        todoist_signature,
        headers,
    )
    second = post_signed(
        client,
        "/webhooks/todoist",
        payload,
        "X-Todoist-Hmac-SHA256",
        todoist_signature,
        headers,
    )

    assert first.status_code == 502
    assert second.json["status"] == "started"
    assert request_post.call_count == 2


@patch("main.requests.post")
def test_running_or_unlinked_toggl_entry_is_ignored(request_post, client):
    payload = {
        "payload": {
            "description": "Unlinked task",
            "duration": -1,
            "stop": None,
        }
    }
    response = post_signed(
        client, "/webhooks/toggl", payload, "X-Webhook-Signature-256", toggl_signature
    )
    assert response.status_code == 200
    assert response.json == {"status": "ignored"}
    request_post.assert_not_called()


def test_toggl_ping_is_accepted(client):
    response = post_signed(
        client,
        "/webhooks/toggl",
        {"payload": "ping"},
        "X-Webhook-Signature-256",
        toggl_signature,
    )
    assert response.status_code == 200
    assert response.json == {"status": "ok"}
