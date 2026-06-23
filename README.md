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

## How to Get Started

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) installed on your machine

### Step 1: Clone the repository

```bash
git clone https://github.com/JorgiEagle/event-driven-devin.git
cd event-driven-devin
```

### Step 2: Set your target repository

Edit `config.json` and set the GitHub repository you want to monitor:

```json
{
  "target_repo": "your-org/your-repo",
  "trigger_label": "assign-devin",
  "log_level": "INFO",
  "data_dir": "./data"
}
```

| Setting | Description | Default |
|---------|-------------|---------|
| `target_repo` | GitHub repository to monitor (e.g. `owner/repo`) | `"owner/repo"` |
| `trigger_label` | Issue label that triggers automation | `"assign-devin"` |
| `log_level` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` | `"INFO"` |
| `data_dir` | Directory for task history persistence | `"./data"` |

> **Note:** `config.json` is for non-secret settings only. Tokens and secrets go in `.env` (see next step).

### Step 3: Create your `.env` file with API keys

Copy the example file:

```bash
cp .env.example .env
```

Then edit `.env` and fill in your tokens:

```bash
# GitHub personal access token (required for issue listing)
EDD_GITHUB_TOKEN=ghp_your_token_here

# GitHub webhook secret (optional, for verifying webhook signatures)
EDD_GITHUB_WEBHOOK_SECRET=

# Devin API token (required for automated session creation)
EDD_DEVIN_API_TOKEN=your_devin_token_here

# ngrok auth token (optional, only needed for webhook tunnel)
NGROK_AUTHTOKEN=
```

#### Where to get each key

| Key | Where to sign up / create | Required? |
|-----|---------------------------|-----------|
| `EDD_GITHUB_TOKEN` | [github.com/settings/tokens](https://github.com/settings/tokens) - Create a **Personal Access Token (classic)** with `repo` scope (or minimum: `issues:read` + `admin:repo_hook` for auto-registering webhooks) | **Yes** - needed to list issues on the dashboard |
| `EDD_GITHUB_WEBHOOK_SECRET` | Choose any random string (e.g. run `openssl rand -hex 20`). Use the same value when configuring the webhook in GitHub. | No - but recommended for production |
| `EDD_DEVIN_API_TOKEN` | [app.devin.ai](https://app.devin.ai) - Go to **Settings > API** and generate an API token | **Yes** - needed to create Devin sessions |
| `NGROK_AUTHTOKEN` | [ngrok.com/signup](https://ngrok.com/signup) (free) - After signing up, copy your token from [dashboard.ngrok.com/get-started/your-authtoken](https://dashboard.ngrok.com/get-started/your-authtoken) | No - only needed if you want GitHub to send live webhook events |

> **Security:** The `.env` file is in `.gitignore` and `.dockerignore` so your tokens are never committed to git or baked into the Docker image. They are injected at runtime only.

### Step 4: Start the app

```bash
docker compose up --build
```

The dashboard is now available at **http://localhost:8000**.

### Step 5: Use the dashboard

Open [http://localhost:8000](http://localhost:8000) in your browser. You will see:

- A list of open issues fetched live from your target GitHub repository
- A **"Kick Off"** button next to each issue to manually trigger a Devin session
- A task history table showing the status of all triggered tasks

There are no user-editable fields — the dashboard is read-only for issue data. The only user action is clicking "Kick Off" to start automation on a specific issue.

### Step 6: Enable webhook tunnel (optional)

To receive live GitHub webhook events (so issues with the `assign-devin` label automatically trigger Devin sessions), you need a public URL. The app supports ngrok as an optional tunnel:

1. Make sure `NGROK_AUTHTOKEN` is set in your `.env` file
2. Start with the tunnel profile:

```bash
docker compose --profile tunnel up --build
```

3. The dashboard will show a green banner with your webhook URL (e.g. `https://abc123.ngrok-free.app/webhook/github`)
4. The ngrok inspection UI is available at **http://localhost:4040**

Without `--profile tunnel`, the ngrok container is not started. The app runs fully offline — issue listing, task history, and manual kickoff all work without a tunnel.

### Step 7: Configure GitHub webhook (optional)

Once your tunnel is running, configure GitHub to send events to your app:

**Option A (automatic):** If your `EDD_GITHUB_TOKEN` has `admin:repo_hook` scope, the app auto-registers the webhook on startup. No manual configuration needed.

**Option B (manual):**
1. Go to your target repository on GitHub
2. Navigate to **Settings > Webhooks > Add webhook**
3. Fill in:
   - **Payload URL**: The URL shown in the dashboard green banner (e.g. `https://abc123.ngrok-free.app/webhook/github`)
   - **Content type**: `application/json`
   - **Secret**: Same value as `EDD_GITHUB_WEBHOOK_SECRET` in your `.env` file
   - **Events**: Select **"Issues"** only
4. Click **Add webhook**

Now, when an issue is created or labeled with `assign-devin` in your target repo, a Devin session will automatically start.

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

Both the webhook path (automatic) and the manual kickoff path write to the same task model and produce identical lifecycle tracking.

## Architecture

Single-service Python application:
- **FastAPI** web server with Jinja2 templates
- **GitHub API client** for reading issues from the target repository (no local issue storage)
- **GitHub webhook receiver** for event-driven triggers
- **JSON file persistence** for task/session history only (not issues)
- **Docker-based local deployment**

GitHub is the authoritative source for issue data. The dashboard reads issues live from the API on each page load. Only Devin task/session state is stored locally.

## Configuration Reference

Settings are loaded with this precedence (highest wins first):

1. **Environment variables** (`EDD_*` prefix) - from `.env` file or exported in shell
2. **config.json** - non-secret, repo-specific settings
3. **Code defaults** - built-in fallback values

For example, if `EDD_LOG_LEVEL=DEBUG` is in your `.env` and `"log_level": "INFO"` is in `config.json`, the app uses `DEBUG`.

### Environment variables

All app settings can be overridden with `EDD_` prefixed environment variables:

| Variable | Maps to | Example |
|----------|---------|---------|
| `EDD_GITHUB_TOKEN` | `github_token` | `ghp_abc123...` |
| `EDD_GITHUB_WEBHOOK_SECRET` | `github_webhook_secret` | `mysecret123` |
| `EDD_DEVIN_API_TOKEN` | `devin_api_token` | `tok_xyz...` |
| `EDD_TARGET_REPO` | `target_repo` | `owner/repo` |
| `EDD_TRIGGER_LABEL` | `trigger_label` | `assign-devin` |
| `EDD_LOG_LEVEL` | `log_level` | `DEBUG` |
| `EDD_DATA_DIR` | `data_dir` | `/data` |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard - lists GitHub issues with kick-off buttons |
| `GET` | `/tasks/{id}` | Task detail with status history |
| `POST` | `/tasks/kickoff/{issue_number}` | Trigger Devin session for a specific issue |
| `POST` | `/webhook/github` | GitHub webhook receiver |

## Logging

All logs are structured JSON:

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

## Development (without Docker)

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your tokens
set -a; source .env; set +a
EDD_CONFIG_PATH=config.json uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Technical Stack

- Python 3.12
- FastAPI + Uvicorn
- Jinja2 templates
- JSON file persistence
- Docker / Docker Compose
- ngrok (optional, for webhook tunneling)
