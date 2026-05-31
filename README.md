![Banner](assets/banner.png)

# Memorandum Message Collector

[![CI](https://github.com/shiryavsky/memorandum/actions/workflows/python-app.yml/badge.svg)](https://github.com/shiryavsky/memorandum/actions/workflows/python-app.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**Read in:** English · [Deutsch](README.de.md) · [Русский](README.ru.md) · [简体中文](README.zh-CN.md)

Stop digging through five chat clients to find what someone said last Tuesday. Memorandum aggregates **Mattermost, Telegram, Pachca, and IMAP email** into a local searchable database and exposes it as MCP tools — so **Claude, Gemini, Hermes**, and other MCP clients can answer questions across all your work conversations.

## Ask Claude things like

> *"Summarize what the platform team discussed about `PL-15491` this week."*
>
> *"Did anyone @-mention me about the migration yesterday?"*
>
> *"Find the spreadsheet Marina sent on Tuesday and pull the Q3 numbers."*
>
> *"Draft a reply to the last email from the customer about the launch date."*

Memorandum runs locally — your messages and attachments never leave your machine, and the agent talks to them through MCP.

## Features

**Sources & sync**
- Pulls from **Mattermost, Telegram, Pachca, and IMAP** — multiple accounts of each, named independently
- Incremental sync per source; parallel fetch (per-source failure stays isolated)
- Captures **file attachments at ingest** — critical for Pachca and Telegram, whose URLs expire
- Per-source YAML filters: skip bots, channels, regex patterns

**Search & retrieval**
- **Two-tier storage** — SQLite for structured queries (sender / channel / time range), ChromaDB for semantic search
- **Live gap reads** — `get_new_messages` hits the source directly, so the agent sees up-to-the-second tail of a channel
- **Thread reconstruction** — `get_thread` returns root + all replies, including across IMAP folders
- **YouTrack issue links** — issue ids parsed from URLs and channel names; `find_by_issue` returns everything referencing one
- **Permalinks** on every result — click to jump back to the original message

**People & identity**
- **Cross-source aliases** with optional role / team / reports-to / responsible-for — the agent knows who's who from the first session
- **Agent-writable aliases** — Claude can persist what it learns about people (role change, new project) directly into `config.yaml` (round-trip preserves your comments)
- **Internal vs external** classification (source flag → email domain → per-alias override); external senders are tagged `[external]`
- **Mention graph** — `who_mentioned` answers "who pinged me / Alice this week" with alias resolution

**Operations**
- **MCP server** with tools for search, summarize, digest, decisions, threads, issue lookup, and file access
- **Send back** (opt-in, per-source gate) — Telegram business chats supported; email replies land in your Drafts folder for review
- **Retention / housekeeping** — automatic pruning of old messages + vectors; content-addressed attachment sweep keeps anything still referenced
- **CLI**: `./bin/memorandum {health, dashboard, aliases refresh, prune}` — live terminal TUI plus housekeeping tools

For implementation details (architecture, schemas, sync internals), see [AGENTS.md](AGENTS.md).

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/shiryavsky/memorandum.git
cd memorandum
```

### 2. Setup (macOS/Linux)

```bash
./setup.sh
```

`setup.sh` creates a `.venv`, installs Python deps, and bootstraps `config.yaml` from `config.example.yaml` on first run. On Linux it pre-installs CPU-only PyTorch (FlagEmbedding's transitive dep) from the [PyTorch CPU index](https://download.pytorch.org/whl/cpu) so the ~1.3 GB CUDA bundle is skipped — macOS torch is already CPU.

The default embedding model is multilingual — works well for English out of the box.

### 3. Configure

Edit `config.yaml` (created in step 2 from `config.example.yaml`) and add your sources:

```yaml
sources:
  company_mattermost:
    type: mattermost
    enabled: true
    url: "https://mattermost.yourcompany.com"
    token: "your-personal-access-token"   # Account Settings → Security
    internal: true                        # senders here are company staff (external ones get an [external] tag)
    allow_send: false                     # default; set true to let the send_message tool post here
    filters:
      skip_senders: ["github-bot"]
      skip_channels: ["off-topic"]
      skip_patterns:
        - "^Reminder:"
        - "joined the channel"

  work_telegram:
    type: telegram
    enabled: true
    token: "123456:AABBcc..."   # from @BotFather

  work_pachca:
    type: pachca
    enabled: true
    token: "your-personal-access-token"   # Automations → API in Pachca settings
    filters:
      skip_channels: ["random"]

display_timezone: "America/New_York"   # timestamps in MCP output

# Optional: classify YouTrack issue links and channel names like "PL-15491".
# Omit this block to disable issue-id detection (URLs are still extracted as generic).
youtrack:
  base_url: "https://youtrack.yourcompany.com"
  project_prefixes: [PL, DEMO, MOBILE]

# The current user (always treated as internal). Use bare usernames, no leading "@".
my_aliases:
  - "you"
  - "you.lastname"

# Canonical identity for other people. role / team / reports_to / responsible_for
# are optional and surface via the `get_user_aliases` MCP tool.
user_aliases:
  - canonical_name: "Jane Smith"
    internal: true
    role: "Backend lead"
    team: "Platform"
    responsible_for: ["dev-pl", "PL-*"]
    aliases: ["jane", "jsmith"]
```

Tip: after a few weeks of ingest, run `./bin/memorandum aliases refresh` to print stub entries for every sender not yet in `user_aliases`, sorted by message count and tagged with the source they came from. Paste the ones you care about and add `role`/`team`/`internal` by hand.

### 4. First-time Ingest

```bash
./run_ingest.sh --hours 720  # Fetch last 30 days
```

### 5. Health check

After the first ingest, verify everything wired up correctly — sources connected, messages stored, embeddings populated:

```bash
./bin/memorandum health
```

The same report is also available as a `get_health` MCP tool once the server is registered.

### 6. Start Scheduler (runs every 15 minutes)

**Linux with systemd (recommended for production):**
```bash
sudo cp systemd/memorandum-collect.service /etc/systemd/system/
sudo cp systemd/memorandum-collect.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now memorandum-collect.timer
```

**macOS or non-systemd environments:**
```bash
./bin/memorandum-sync
```
Or run the fallback scheduler:
```bash
source .venv/bin/activate
python -m pipeline.scheduler
```

### 7. Register MCP Server

#### with Claude:

Add to your Claude MCP config (`~/.config/claude/mcp_servers.json`):

```json
{
  "memorandum": {
    "command": "/path/to/memorandum/.venv/bin/python",
    "args": ["/path/to/memorandum/mcp_server/server.py"],
    "cwd": "/path/to/memorandum",
    "timeout": 120
  }
}
```

#### with Hermes:

Add to Hermes config (`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  memorandum:
    command: /path/to/memorandum/.venv/bin/python
    args:
      - /path/to/memorandum/mcp_server/server.py
      - --config
      - /path/to/memorandum/config.yaml
    timeout: 120
```

The `--config` argument ensures the server finds config.yaml even when workdir doesn't work.

### 8. (Optional) Live dashboard

Once ingest is running on a schedule, the terminal TUI gives a one-screen view of storage, ingest health, mentions, send activity, and MCP tool usage — handy in a tmux pane:

```bash
./bin/memorandum dashboard
```

Refreshes every 5 seconds; quit with `q`.

![Dashboard Screenshort](assets/dashboard.png)

## Project Structure

```
memorandum/
├── config.yaml              # Credentials and settings (gitignored)
├── config.example.yaml      # Example configuration
├── requirements.txt         # Python dependencies
├── requirements-dev.txt     # Dev dependencies (pytest, pytest-cov, responses)
│
├── connectors/                  # Source connectors
│   ├── CONTRIBUTING.md          # ★ How to add a new connector — read first if you're extending this folder
│   ├── mattermost_connector.py  # Mattermost REST API (per-channel sync)
│   ├── telegram_connector.py    # Telegram Bot API (groups, channels, business msgs; skips bot DMs)
│   ├── pachca_connector.py      # Pachca REST API (per-chat cursor sync)
│   └── email_connector.py       # IMAP (folder-per-channel; Message-ID threading; send = draft)
│
├── pipeline/                # Ingest engine (runs under systemd)
│   ├── ingest.py            # Orchestrates fetch → filter → store, one connector per source
│   ├── health.py            # Health report builder + formatter (shared by CLI and MCP)
│   ├── alias_resolver.py    # Canonical identity resolution from user_aliases config
│   ├── filter_engine.py     # Per-source YAML-based filtering
│   └── scheduler.py         # Fallback scheduler (non-systemd only)
│
├── cli/                     # User-facing CLI utilities (`python -m cli ...` / `bin/memorandum`)
│   ├── __main__.py          # argparse dispatcher
│   ├── health.py            # `memorandum health` — wraps pipeline.health
│   ├── aliases.py           # `memorandum aliases refresh` — append-only stub generator
│   ├── alias_writer.py      # Shared YAML round-trip surface (used by refresh + MCP write tools)
│   ├── prune.py             # `memorandum prune` — dry-run retention preview / --commit
│   └── dashboard.py         # `memorandum dashboard` — live rich TUI (read-only DB connection)
│
├── storage/                 # Storage layer
│   ├── db.py                # SQLite metadata store
│   └── vector_store.py      # ChromaDB embeddings
│
├── mcp_server/              # MCP server
│   └── server.py            # Claude tools
│
├── data/                    # Local storage (gitignored)
│   ├── messages.db          # SQLite database
│   ├── chroma/              # ChromaDB persistence
│   └── attachments/         # Downloaded message attachments
│
├── systemd/                         # Linux deployment
│   ├── memorandum-collect.service   # Systemd oneshot service
│   └── memorandum-collect.timer     # Systemd timer (every 15 min)
|
├── bin/                     # Scripts
│   ├── memorandum-sync      # Main sync script with lock protection
│   └── memorandum           # CLI wrapper — runs `python -m cli "$@"` in the venv
│
├── tests/                   # Unit tests (pytest)
│   ├── conftest.py          # Shared fixtures
│   ├── test_config.py
│   ├── test_filter_engine.py
│   ├── test_db.py
│   ├── test_server.py
│   ├── test_ingest.py
│   ├── test_mattermost_connector.py
│   ├── test_telegram_connector.py
│   ├── test_pachca_connector.py
│   ├── test_alias_resolver.py
│   ├── test_health.py
│   ├── test_youtrack_helpers.py
│   ├── test_cli_main.py
│   └── test_cli_aliases.py
│
├── setup.sh                 # macOS/Linux setup
├── run_ingest.sh            # One-off ingest test
└── README.md
```

## Available Tools (MCP)

| Tool                 | Description                                                |
| -------------------- | ---------------------------------------------------------- |
| `search_messages`    | Search by keyword or semantic meaning                      |
| `summarize_channel`  | Get messages from a specific channel for summarization     |
| `summarize_messages` | Digest of messages from a flexible time range (hours/days) |
| `list_channels`      | List known channels (id + name + description) from the database |
| `get_new_messages`   | Fetch messages newer than the DB for a channel, live from source (all sources) |
| `find_decisions`     | Find decisions and action items                            |
| `get_thread`         | Reconstruct a full thread (root + replies) by `thread_id`  |
| `get_stats`          | Message statistics per configured source                   |
| `get_attached_file`  | Get file content by file_id (Telegram, Mattermost, Pachca) |
| `get_user_aliases`   | Show configured identity aliases and current user aliases  |
| `get_health`         | Last ingest run status, per-source message freshness, errors |
| `send_message`       | Send a text message to a channel (opt-in via `allow_send`; all sources) |
| `find_by_issue`      | Find messages referencing a YouTrack issue id (links + channel-name match) |
| `who_mentioned`      | Find messages where someone @-mentioned a person (alias-resolved; `target: "me"` works) |
| `upsert_user_alias`  | Persist what you learned about a person (role / team / aliases / `responsible_for`) into the durable memory layer |
| `remove_user_alias`  | Delete a user_aliases entry; refuses my_aliases targets |
| `update_user_alias_strings` | Add/remove specific alias handles on one existing entry; refuses cross-canonical theft |

### send_message

Sends a text reply to a channel — the action half of the read→act loop. Two safety rails:

- **Opt-in per source** (default-deny): the tool refuses unless the source sets `allow_send: true` in `config.yaml`. Sends are visible to other people, so this is off by default.
- **Read-before-send**: the agent must call `get_new_messages` for the channel right before sending; if new messages have appeared, the send is cancelled and the reply reconsidered with the new context.

Args: `source`, `channel` (the channel **id** from `list_channels`), `text`, and optional `reply_to` (Mattermost root post id / Telegram message id / Pachca parent message id) to thread the reply. Sending file attachments is not yet supported.

### summarize_messages Parameters

| Parameter      | Type   | Default | Description                                            |
| -------------- | ------ | ------- | ------------------------------------------------------ |
| `hours`        | int    | -       | Look back N hours (e.g., 4, 24, 168). Overrides `days` |
| `days`         | int    | 1       | Look back N days                                       |
| `source`       | string | -       | Filter by source name (e.g., `company_mattermost`)     |
| `channel`      | string | -       | Filter by channel name                                 |
| `max_messages` | int    | 100     | Max messages per channel                               |

Use `get_stats` to see the source names configured in your instance.

## Testing

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run tests
pytest tests/ -v --tb=short

# Run with coverage report
pytest tests/ --cov=. --cov-report=term-missing --ignore=storage/vector_store.py
```

The test suite (~310 tests) covers config loading, filtering, SQLite storage, MCP URL generation and tool handlers, all three connectors (HTTP mocked via `responses`), the ingest orchestrator (VectorStore mocked — no BGE-M3 model loaded), the CLI dispatcher, and the `aliases refresh` round-trip through `ruamel.yaml`.

## Ingest Options

```bash
# Normal sync (uses saved channel state)
./run_ingest.sh

# Force full scan from 24 hours ago
./run_ingest.sh --hours 24 --force

# Debug mode
./run_ingest.sh --debug
```

## CLI Utilities

User-facing tools live under `cli/`. The wrapper `./bin/memorandum` resolves the venv for you; otherwise call `python -m cli <verb>` from an activated venv.

```bash
./bin/memorandum health                          # ingest status + per-source freshness
./bin/memorandum health --json                   # machine-readable
./bin/memorandum aliases refresh                 # print stub user_aliases entries for new senders
./bin/memorandum aliases refresh --in-place      # append those stubs into config.yaml
```

Exit codes for `health`: `0`=ok, `1`=partial/error, `2`=never ran — usable as a monitoring check (`./bin/memorandum health && echo healthy || echo check logs`). The same data is available from Claude via the `get_health` MCP tool.

`aliases refresh` is **append-only**: it diffs senders in the DB against your existing `user_aliases` entries and emits stubs (sorted by message count) for the ones not yet covered. Existing entries are never edited or reordered; `--in-place` uses `ruamel.yaml` round-trip so comments in your `config.yaml` survive intact.

> `python -m pipeline health` (the old form) now prints a one-line redirect and exits 2 — use `python -m cli health` (or the wrapper above).

## Linux Deployment (systemd)

For production on Linux with systemd:

```bash
# Copy service and timer files
sudo cp systemd/memorandum-collect.service /etc/systemd/system/
sudo cp systemd/memorandum-collect.timer /etc/systemd/system/

# Install logrotate config for sync logs
sudo cp systemd/memorandum-sync.logrotate /etc/logrotate.d/memorandum-sync

# Edit paths in the service file
sudo vim /etc/systemd/system/memorandum-collect.service
# Change WorkingDirectory and ExecStart paths to match your installation

# Enable and start the timer
sudo systemctl daemon-reload
sudo systemctl enable --now memorandum-collect.timer

# Check status
sudo systemctl status memorandum-collect.timer
sudo systemctl list-timers

# View logs
journalctl -u memorandum-collect -f

# View sync log
tail -f /var/log/memorandum-sync.log

# Manual run (if needed)
sudo systemctl start memorandum-collect
```

## Logging

The sync script (`bin/memorandum-sync`) logs to:
- `/var/log/memorandum-sync.log` on Linux (if /var/log is writable)
- `data/memorandum-sync.log` in the project directory (fallback)

Logs are rotated daily and kept for 7 days by the logrotate config installed above.

## Environment Requirements

- Python 3.11+
- Virtual environment (`.venv`)
- A Mattermost Personal Access Token, Telegram Bot Token, and/or Pachca Personal Access Token
- ~4.5GB disk for model + data (default BGE-M3; less with a smaller model — see [Swapping the embedding model](#swapping-the-embedding-model))
- ~2-2.5GB RAM for BGE-M3 embeddings (default; a small English-only model fits in ~300MB)

## Swapping the embedding model

The vector store's model and tuning live in `config.yaml` under `embedding:`. Omit the block to keep the BGE-M3 default; override any subset of these keys:

```yaml
embedding:
  model: "BAAI/bge-m3"       # any FlagEmbedding-supported model id
  device: "cpu"              # "cpu", "cuda", or "mps"
  use_fp16: true
  max_length: 512
  batch_size: 1
  collection_name: "messages"
```

Suggested alternatives:
- `BAAI/bge-m3` — multilingual, ~4GB on disk, **1024-dim** (default)
- `BAAI/bge-small-en-v1.5` — English-only, ~130MB, **512-dim** (fast, low RAM)

**Important — dimensionality:** Chroma stores vectors at a fixed dimension per collection. Pointing `model:` at a different model (or a different output size) silently breaks similarity search unless every document is re-embedded. Memorandum surfaces a clear error on the first insert if the existing collection's dim doesn't match the configured model, but you should still pick a migration path before swapping:

1. **Keep the old vectors around.** Set `collection_name:` to a fresh value (e.g. `messages_bge_small`). The old collection stays on disk; the new model populates the new one.
2. **Start clean.** Stop ingest, `rm -rf data/chroma/`, then `./run_ingest.sh --hours <N>` to re-embed everything.

## Extending Memorandum

### Adding a new source connector (Slack, Discord, Matrix, …)

The four built-in connectors are a small surface and the rest of the system extends naturally to a fifth. The walkthrough — interface contract, message dict shape, incremental-sync pattern, file attachments, the four dispatch sites you need to wire, tests to write, and the gotchas the existing connectors hit while being built — lives at **[connectors/CONTRIBUTING.md](connectors/CONTRIBUTING.md)**. Read it end-to-end before writing code; the contract is small but the *order* and the *invariants* matter.

## What's shipped, what's next

[**CHANGELOG.md**](CHANGELOG.md) is the decision log — every landed feature
with a short rationale and the file paths it touched. Useful both as
"what's in this build" and as "why was X built that way" reference for
contributors.

For planned work and bug reports, use [GitHub Issues](../../issues) (templates
provided); for design questions, [Discussions](../../discussions).
