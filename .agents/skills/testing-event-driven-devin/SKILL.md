---
name: testing-event-driven-devin
description: Test the event-driven-devin FastAPI service end-to-end. Use when verifying webhook handling, dashboard UI, task lifecycle, or logging changes.
---

# Testing Event-Driven Devin

## Prerequisites

- Python 3.12 with dependencies installed (`pip install -r requirements.txt`)
- No external credentials needed for local testing (Devin API token absence is handled gracefully)

## Running the App Locally

```bash
cd /home/ubuntu/repos/event-driven-devin
EDD_CONFIG_PATH=config.json uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Dashboard available at http://localhost:8000

## Key Test Scenarios

### 1. Webhook Filtering
The webhook at `POST /webhook/github` should:
- Reject non-`issues` events with `{"status": "ignored"}`
- Reject issues without `assign-devin` label
- Accept issues with `assign-devin` label and create a task

```bash
# Should be ignored (wrong event type)
curl -s -X POST http://localhost:8000/webhook/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: push" \
  -d '{}'

# Should be ignored (no trigger label)
curl -s -X POST http://localhost:8000/webhook/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: issues" \
  -d '{"action":"opened","issue":{"number":1,"title":"Test","html_url":"http://x","labels":[{"name":"bug"}]},"repository":{"full_name":"test/repo"}}'

# Should be accepted (has assign-devin label)
curl -s -X POST http://localhost:8000/webhook/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: issues" \
  -d '{"action":"opened","issue":{"number":42,"title":"Fix bug","html_url":"http://x/42","labels":[{"name":"assign-devin"}]},"repository":{"full_name":"test/repo"}}'
```

### 2. Dashboard UI
- Navigate to http://localhost:8000 to verify the dashboard renders
- Check task table populates after webhook/manual kickoff
- Click task rows to view detail pages with transition timelines

### 3. Manual Kickoff
- Use the form at the top of the dashboard (Issue #, Title, Repository)
- Verify it produces identical lifecycle to webhook path

### 4. Status Updates
- On task detail page, use the "Update Status" form
- Verify transitions are appended (not replaced) in the timeline

### 5. JSON Logging
- All logs go to stdout as structured JSON
- Required fields: `timestamp`, `level`, `logger`, `message`
- Task logs should also include: `task_id`, `issue_number`, `repo`, `event_type`, `state`

## Without Devin API Token

When `devin_api_token` is empty in config.json, the app gracefully degrades:
- Tasks transition to `attention_required` instead of `resolving`
- The reason logged is "Failed to start Devin session - check API token and connectivity"
- This is expected behavior for local testing

## Data Persistence

Task data is stored in `./data/tasks.json` (local) or `/data/tasks.json` (Docker).
The file persists across restarts. Delete it to reset state.

## Docker Testing

```bash
docker compose up --build
# Dashboard at http://localhost:8000
```

## Devin Secrets Needed

- `EDD_DEVIN_API_TOKEN` - Devin API token (optional for local testing, required for full flow)
- `EDD_GITHUB_TOKEN` - GitHub PAT (optional for local testing)
- `EDD_GITHUB_WEBHOOK_SECRET` - Webhook HMAC secret (optional in dev mode)
