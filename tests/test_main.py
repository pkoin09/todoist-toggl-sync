import base64
import hashlib
import hmac
import json
from unittest.mock import Mock, patch

import pytest

import main


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("TODOIST_TOKEN", "todoist-token")
    monkeypatch.setenv("TODOIST_CLIENT_SECRET", "todoist-secret")
    monkeypatch.setenv("TOGGL_API_TOKEN", "toggl-token")
    monkeypatch.setenv("TOGGL_WORKSPACE_ID", "123")
    monkeypatch.setenv("TOGGL_WEBHOOK_SECRET", "toggl-secret")


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


def post_signed(client, path, payload, header, signer):
    body = json.dumps(payload, separators=(",", ":")).encode()
    return client.post(
        path, data=body, content_type="application/json", headers={header: signer(body)}
    )


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json == {"status": "ok"}


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
