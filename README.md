# Event-Driven Devin

Automated issue remediation for any GitHub repository using [Devin](https://devin.ai).
When an issue is created or labeled with `assign-devin`, the system triggers a Devin session that resolves the issue and opens a pull request.

## Features

- **GitHub Issue Dashboard** - Lists open issues directly from the GitHub repository (authoritative source)
- **Manual Task Kickoff** - One-click "Kick Off" button per issue to start a Devin session
- **Event-Driven Remediation** - Webhook-triggered automation on `assign-devin` label
- **Automatic PR Creation** - PRs are created only after Devin fully completes a task
- **Structured Observability** - JSON logging with task/issue identifiers at every lifecycle step
- **Per-Task Status Tracking** - Full state machine with transition history

## Architecture

Single-service Python application:
- **FastAPI** web server with Jinja2 templates
- **GitHub API client** for reading issues from the target repository (no local issue storage)
- **GitHub webhook receiver** for event-driven triggers
- **JSON file persistence** for task/session history only (not issues)
- **Docker-based local deployment** (no external dependencies)

GitHub is the authoritative source for issue data. The dashboard reads issues live from the API on each page load. Only Devin task/session state is stored locally.

## Task Lifecycle

Every task follows this state machine:

```
[ready] -> [queued] -> [resolving] -> [ready_to_merge] -> [merged]
                |            |
                v            v
           [failed]   [attention_required]
```

| Status | Meaning |
|--------|---------|
| `ready` | Issue received, task created |
| `queued` | Task queued for Devin session |
| `resolving` | Devin session is actively working |
| `ready_to_merge` | Devin completed, PR created |
| `merged` | PR merged successfully |
| `failed` | Task execution failed |
| `attention_required` | Requires human intervention |

## Trigger Model

The webhook only fires automation when:
1. The event is an `issues` event (type: `opened` or `labeled`)
2. The issue has the **`assign-devin`** label

All other events and labels are ignored. The webhook returns a clear rejection reason for non-matching events.

## Quick Start (Local Docker)

### 1. Configure

Edit `config.json` with your settings:

```json
{
  "target_repo": "your-org/your-repo",
  "github_webhook_secret": "your-webhook-secret",
  "github_token": "ghp_...",
  "devin_api_token": "your-devin-api-token",
  "trigger_label": "assign-devin",
  "log_level": "INFO",
  "data_dir": "/data"
}
```

| Setting | Description |
|---------|-------------|
| `target_repo` | The single GitHub repository to monitor (e.g. `owner/repo`) |
| `github_webhook_secret` | Secret for verifying webhook payloads (optional in dev) |
| `github_token` | GitHub personal access token for API calls |
| `devin_api_token` | Devin API token for creating sessions |
| `trigger_label` | Label that triggers automation (default: `assign-devin`) |
| `log_level` | Logging verbosity: DEBUG, INFO, WARNING, ERROR |
| `data_dir` | Directory for task persistence (Docker volume mount) |

### 2. Run

```bash
docker compose up --build
```

The dashboard is available at **http://localhost:8000**.

All features work without a tunnel — issue listing, task history, manual kickoff, and the webhook endpoint (for local testing via curl) are fully functional.

### 3. Enable webhook tunnel (optional)

To receive live GitHub webhook events, enable the ngrok tunnel:

1. Sign up free at https://ngrok.com/signup
2. Copy your auth token from https://dashboard.ngrok.com/get-started/your-authtoken
3. Create a `.env` file (see `.env.example`):

```bash
NGROK_AUTHTOKEN=your_ngrok_authtoken_here
```

4. Start with the tunnel profile:

```bash
docker compose --profile tunnel up --build
```

The ngrok inspect UI is at **http://localhost:4040**.  
Without `--profile tunnel`, the ngrok container is not started and the app runs fully offline.

### 4. Configure GitHub Webhook

The dashboard displays the current webhook URL in a green banner at the top. You can either:

**Option A (automatic):** If your `github_token` has `admin:repo_hook` scope, the app auto-registers the webhook on startup. No manual configuration needed.

**Option B (manual):** Copy the webhook URL from the dashboard and configure it in your target repository:
- Go to **Settings → Webhooks → Add webhook**
- **Payload URL**: The URL shown in the dashboard (e.g. `https://abc123.ngrok-free.app/webhook/github`)
- **Content type**: `application/json`
- **Secret**: Same value as `github_webhook_secret` in config
- **Events**: Select "Issues"

### 5. Use

**Webhook path**: Create or label a GitHub issue with `assign-devin` to trigger automation.

**Manual path**: Open the dashboard at http://localhost:8000, browse the issues fetched from GitHub, and click "Kick Off" on any issue to start a Devin session.

Both paths write to the same task model and produce identical lifecycle tracking. There are no user-editable fields — the dashboard is read-only for issue data.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard — lists GitHub issues with kick-off buttons (HTML) |
| `GET` | `/tasks/{id}` | Task detail with status history (HTML) |
| `POST` | `/tasks/kickoff/{issue_number}` | Trigger Devin session for a specific issue |
| `POST` | `/webhook/github` | GitHub webhook receiver |

## Logging

All logs are structured JSON with consistent fields:

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "level": "INFO",
  "logger": "app.webhook",
  "message": "Task created from webhook",
  "task_id": "abc-123",
  "issue_number": 42,
  "repo": "owner/repo",
  "event_type": "webhook",
  "state": "ready"
}
```

Every lifecycle step emits a log entry with `task_id`, `issue_number`, `repo`, `event_type`, `state`, and `outcome` fields.

## Development

Run locally without Docker:

```bash
pip install -r requirements.txt
EDD_CONFIG_PATH=config.json uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Single-Repository Assumption

The MVP is designed for a single target repository configured in `config.json`. The webhook receiver validates incoming events and the dashboard displays tasks for that repository. Multi-repo support is deferred to a future iteration.

## Technical Stack

- Python 3.12
- FastAPI + Uvicorn
- Jinja2 templates
- JSON file persistence
- Docker / Docker Compose
