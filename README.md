# Todoist ↔ Toggl Track Sync

A small Flask service that starts a Toggl Track timer when a Todoist task is
created, then adds the tracked duration to that task as a comment when the
timer stops.

## Flow

1. Todoist sends an `item:added` event to `POST /webhooks/todoist`.
2. The service validates `X-Todoist-Hmac-SHA256`. When the task has the
   configured trigger label (`@work` by default), it creates a running Toggl
   entry named `<task content> [todoist:<task id>]`.
3. Toggl sends a `time_entry` `updated` event to `POST /webhooks/toggl`.
4. The service validates `X-Webhook-Signature-256`. If the tagged entry has
   stopped, it posts the formatted duration to the original Todoist task.

Both integrations sign the raw request body. Unsigned or incorrectly signed
requests are rejected.

## Configuration

Copy `.env.example` for the list of required variables. Set these in Railway's
Variables dashboard for production:

| Variable | Source |
| --- | --- |
| `TODOIST_TOKEN` | Todoist Settings → Integrations → Developer |
| `TODOIST_CLIENT_SECRET` | Todoist App Management Console |
| `TODOIST_TRIGGER_LABEL` | Optional; label that starts a timer (default: `work`) |
| `TOGGL_API_TOKEN` | Toggl Track Profile → API Token |
| `TOGGL_WORKSPACE_ID` | Numeric Toggl workspace ID |
| `TOGGL_WEBHOOK_SECRET` | Secret used for the Toggl webhook subscription |

Never commit a populated `.env` file.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

The health check is available at `GET /health`. Public HTTPS URLs are required
to register webhooks, so use a tunnel such as ngrok for an end-to-end local
test.

Run the automated tests with:

```bash
python -m pip install pytest
python -m pytest -q
```

## Deploy to Railway

1. Push this repository to GitHub and create a Railway project from it.
2. Add the five required environment variables.
3. Generate a public Railway domain.
4. Confirm `https://<domain>/health` returns `{"status":"ok"}`.

The included `Procfile` starts `gunicorn main:app` on Railway's assigned port.

## Register webhooks

In the Todoist App Management Console, subscribe to `item:added` with:

```text
https://<domain>/webhooks/todoist
```

Create a Toggl Track webhook subscription with the callback below, the same
secret stored in `TOGGL_WEBHOOK_SECRET`, and this event filter:

```json
{"entity": "time_entry", "action": "updated"}
```

```text
https://<domain>/webhooks/toggl
```

Toggl's subscription API base URL is
`https://api.track.toggl.com/webhooks/api/v1`; it uses HTTP Basic Auth with
`<TOGGL_API_TOKEN>:api_token`. The endpoint accepts Toggl's signed ping used to
validate the callback.

## Test the live integration

1. Add a Todoist task with the `@work` label and confirm a running Toggl entry
   appears. A task without that label should be ignored.
2. Stop that Toggl timer.
3. Confirm a comment such as `Toggl time tracked: **12m 8s**` appears on the
   Todoist task.
4. Check Railway logs if either side does not appear.

## TODO

- [x] **Recommended:** Only start a Toggl timer when a newly created Todoist
  task has the `@work` label (`work` in the webhook payload).
- [ ] Test the automatic timer-start behavior in the real workflow and adjust
  the trigger if it is too disruptive.
- [ ] Add persistent webhook idempotency before treating the service as
  production-ready.
