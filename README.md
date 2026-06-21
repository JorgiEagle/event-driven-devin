# Event-Driven Devin

Automated issue remediation for any GitHub repository using [Devin](https://devin.ai).
When an issue is created or labeled with `assign-devin`, the system triggers a Devin session that resolves the issue and opens a pull request.

## Features

- **GitHub Issue Dashboard** - View tracked issues, task statuses, and full lifecycle history
- **Manual Task Kickoff** - Start the same automation workflow from the web UI
- **Event-Driven Remediation** - Webhook-triggered automation on `assign-devin` label
- **Automatic PR Creation** - PRs are created only after Devin fully completes a task
- **Structured Observability** - JSON logging with task/issue identifiers at every lifecycle step
- **Per-Task Status Tracking** - Full state machine with transition history

## Architecture

Single-service Python application:
- **FastAPI** web server with Jinja2 templates
- **GitHub webhook receiver** for event-driven triggers
- **JSON file persistence** for task history (survives restarts via Docker volume)
- **Docker-based local deployment** (no external dependencies)

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

### 3. Set Up GitHub Webhook

In your target repository's settings:
- **Payload URL**: `http://your-host:8000/webhook/github`
- **Content type**: `application/json`
- **Secret**: Same value as `github_webhook_secret` in config
- **Events**: Select "Issues"

### 4. Use

**Webhook path**: Create or label a GitHub issue with `assign-devin` to trigger automation.

**Manual path**: Use the dashboard form at http://localhost:8000 to kick off a task by issue number.

Both paths write to the same task model and produce identical lifecycle tracking.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard (HTML) |
| `GET` | `/tasks/{id}` | Task detail with history (HTML) |
| `POST` | `/tasks/kickoff` | Manual task kickoff (form) |
| `POST` | `/tasks/{id}/status` | Update task status (form) |
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
