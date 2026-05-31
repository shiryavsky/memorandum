![Banner](assets/banner.png)

# Memorandum Message Collector

[![CI](https://github.com/shiryavsky/memorandum/actions/workflows/python-app.yml/badge.svg)](https://github.com/shiryavsky/memorandum/actions/workflows/python-app.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Add your message context to Claude, Opencode, OpenClaw, Hermes, Grok, and other AI assistants. A personal productivity system that aggregates messages from Mattermost, Telegram, Pachca, and IMAP email into a searchable vector database, exposed via an MCP server.

## Features

- **Multi-source collection**: Multiple Mattermost instances, Telegram bots, Pachca workspaces, and IMAP mail accounts, each named independently
- **Two-tier storage**: SQLite for structured metadata, ChromaDB for semantic search
- **Incremental sync**: Per-channel cursors (Mattermost timestamp, Pachca newest-message-id) and global update offset (Telegram)
- **Parallel fetch**: Sources are fetched concurrently via a thread pool — wall-clock collapses from sum-of-sources to max-of-sources. Per-source failure stays isolated; writes still serialize at the SQLite/embedding layer for correctness. Tune via `ingest.fetch_workers` (auto by default; `1` falls back to the legacy strictly-sequential path)
- **Retention / housekeeping**: After each successful ingest, prune messages / mentions / sent_messages / ingest_runs older than `retention.retention_days` (default 365). Cross-store: SQLite transaction → chroma delete-by-id → file-cache mark-and-sweep (content-addressed safe — a file_id referenced by ANY surviving message is kept). `channels` and `senders` are never pruned. Operator-driven dry-run via `./bin/memorandum prune` (preview counts; `--commit` to actually delete). Throttled to once per `retention.prune_interval_hours` (default 24h) so it doesn't run every ingest cycle
- **Live gap reads**: `get_new_messages` fetches messages newer than the DB straight from the source (all sources), so an agent sees the up-to-the-second tail of a channel
- **File attachments**: Text files embedded inline during ingest; photos and binary files retrievable on demand via `get_attached_file`. File ids are shown inline in message text (Telegram, Pachca); Pachca files are cached at ingest since their URLs expire
- **Per-source filters**: YAML rules per source — skip bots, channels, and patterns independently
- **User aliases with role / team / relations**: Canonical identity mapping across sources, plus optional `role`, `team`, `reports_to`, and `responsible_for` on each entry — surfaced via `get_user_aliases` so the agent can ground who-is-who context early in a session
- **Agent-writable user_aliases (memory layer about people)**: Three MCP tools (`upsert_user_alias`, `remove_user_alias`, `update_user_alias_strings`) let the agent persist things it learns about people (role change, new project ownership, additional handle) directly into `config.yaml` via `ruamel.yaml` round-trip — operator comments / key order survive. Default-on (`allow_alias_edits: true`); `my_aliases` is hard-refused as identity territory; soft caps prevent runaway growth. Audit trail = `git diff config.yaml`
- **Internal/external senders**: Classify company staff vs. clients via (in order, broad→specific) a per-source `internal: true` flag, a top-level `internal_domains:` list (any email on those domains is internal — handy for IMAP), or a per-alias `internal: true|false` override; external senders are tagged `[external]` in output
- **Channel descriptions**: Mattermost `purpose`/`header` and Telegram chat `description` captured at ingest and surfaced in `list_channels` / `summarize_channel` / `summarize_messages` so the agent knows what each channel is for
- **YouTrack issue links**: Issue ids (e.g. `PL-15491`) parsed out of both message URLs and channel names; the `find_by_issue` tool returns everything referencing a given id
- **Thread reconstruction**: Reply messages carry a `🧵 thread:{id}` marker in search results; pass it to `get_thread` to pull the full conversation (root + all replies)
- **Mention graph**: Every `@username` / Pachca `<@id>` in a message body is extracted at ingest into a `mentions` table; `who_mentioned` answers "who pinged whom" with alias resolution (`target: "me"` for the current user)
- **Email (IMAP)**: Mail accounts join the ingest pipeline as folder-per-channel sources. Threading uses `Message-ID` / `References` so `get_thread` returns the full conversation across folders (INBOX + Sent). Recipient-aware internal/external classification (a message is internal only when sender **and** every recipient is internal). `send_message` for an email source **drafts** a reply into the configured Drafts folder via IMAP APPEND — no SMTP setup needed; you review and click Send in your mail client
- **Permalinks**: Every search result and digest entry links back to the original message
- **MCP server**: Claude-accessible tools for search, summarize, digest, decisions, threads, issue lookup, and file access
- **Send messages**: Opt-in `send_message` tool (all sources) gated by a per-source `allow_send` flag and a read-before-send guard — the act half of the read→act loop. Telegram business chats are supported automatically: the bot's `business_connection_id` is captured at ingest and reused on reply. Every send (success and failure) is logged to the `sent_messages` audit table
- **CLI utilities**: `./bin/memorandum health` (ingest status), `./bin/memorandum aliases refresh` (append-only — emits stubs for senders not yet in `user_aliases`, with per-source breakdown; `--in-place` preserves comments via `ruamel.yaml`), `./bin/memorandum prune` (dry-run preview of retention pruning; `--commit` to actually delete), `./bin/memorandum dashboard` (live terminal TUI — storage / ingest / source health / mentions / send activity / MCP tool usage in one screen; refreshes every 5s)

![Dashboard Screenshort](assets/dashboard.png)

## Quick Start

### 1. Setup (macOS/Linux)

```bash
./setup.sh
```

On Linux, `setup.sh` installs CPU-only PyTorch (FlagEmbedding's transitive dep) from the [PyTorch CPU index](https://download.pytorch.org/whl/cpu) so the ~1.3 GB CUDA bundle is skipped. macOS torch is already CPU.

### 2. Configure

Copy `config.example.yaml` to `config.yaml` and add your sources:

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

display_timezone: "Europe/Moscow"   # timestamps in MCP output

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

### 3. First-time Ingest

```bash
./run_ingest.sh --hours 720  # Fetch last 30 days
```

### 4. Start Scheduler (runs every 15 minutes)

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

### 5. Register MCP Server

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
