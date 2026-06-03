# Memorandum - Project Description for AI Assistants

## Purpose

Memorandum is a personal message aggregation system that collects messages from Mattermost, Telegram, and Pachca into a searchable database, making them accessible to AI assistants via an MCP server.

## Architecture

```
Mattermost A ──┐
Mattermost B ──┤
Telegram A   ──┼──▶ per-source Filter ──▶ SQLite + ChromaDB ──▶ MCP Server ──▶ Claude
Telegram B   ──┤
Pachca A     ──┘
```

Multiple named sources of the same type are fully supported. Each source has its own credentials, filters, and logical name stored in `messages.source`.

## Key Components

### Connectors (`connectors/`)
- **`factory.py`** — `build_connector(source_type, source_name, src_cfg, …)` is the single construction point used by both `pipeline.ingest` and `mcp_server.server._build_live_connector`. Returns `None` for unknown types (callers log + skip). Adding a fifth connector means one new branch here, nothing else.
- **`_common.py`** — shared constants (`MAX_TEXT_PREVIEW_SIZE` = 5KB inline preview, `DEFAULT_TEXT_EXTENSIONS` = which attachment extensions are read inline) and `ConnectorProtocol`, the structural contract every source implements. Re-exported from `connectors/__init__.py`. A new connector satisfies it by shape — no inheritance needed; `tests/test_connector_protocol.py` checks all four shipped classes pass `isinstance(inst, ConnectorProtocol)`.

**Unified send-side surface (R10).** Every connector exposes the same two methods the MCP dispatcher relies on:

  - `send_message(channel, text, reply_to=None) -> message_id` — `reply_to` is whatever the platform calls the parent (Mattermost's root post id / Telegram message id / Pachca parent message id / email Message-ID); each connector coerces and renames internally. Platform extras live inside the connector — Telegram looks up `business_connection_id` off the channel row when none is passed explicitly; email derives recipients from the local DB parent row.
  - `message_url(channel, message_id) -> str | None` — builds the just-sent permalink. Returns `None` when the chat/folder has no link surface (Telegram private chats, email drafts, channels the token can't resolve).

The dispatcher in `mcp_server/tools/channels.py::_send_message` is now a flat two-call sequence — no per-source switch.
- **MattermostConnector**: REST API with Personal Access Token. Per-channel incremental sync via `last_update_at` (ms timestamp) stored in the `channels` table.
- **TelegramConnector**: Bot API (`getUpdates`). Collects `message` (groups/supergroups), `channel_post` (channels), and `business_message` update types. Skips direct messages sent to the bot itself (`chat.type == "private"` + `message` type). Global update-offset sync — offset persisted in the `channels` table under `id="__offset__"` per source. Sender full names (first + last) cached during normalization. File attachments shown inline as `[photo, file_id=...]`, `[document: name, file_id=...]`, etc. for on-demand retrieval via `get_attached_file`.
- **PachcaConnector**: REST API (`api.pachca.com/api/shared/v1`) with Personal Access Token. Token verified via `GET /profile` (there is no `/users/me`). Per-chat incremental sync via newest message ID stored as `last_update_at` in the `channels` table. Cursor-based pagination for both `/chats` and `/messages`. Sender name taken from the message's `display_name`; `thread_id`←`thread.id`, `reply_to_id`←`parent_message_id`. Personal (1-on-1) chats have no `name`, so the display name is the partner's name (resolved from `member_ids` minus the current user) and the `channels.name` column falls back to the chat id (never empty). Attachments (`Message.files`) are embedded inline as `[image|file: name, file_id=<id>]` and downloaded into the file cache at ingest (signed URLs expire), retrievable via `get_attached_file`.
- **EmailConnector**: IMAP via `imap_tools` (username/password; OAuth2 deferred). One mailbox per source. Polls configured `folders:` (default `["INBOX"]`); each folder maps to one channel (`channel_id = "folder:<folder_path>"`, `display_name = folder_path` or remap via `channel_names:`). Incremental sync is per-folder UIDVALIDITY + last-seen UID stored in `channels.extra` (`{"uidvalidity": N, "last_uid": M}`); a UIDVALIDITY change forces a full rescan of that folder. Each email becomes one message: `id = "{source}:{Message-ID}"` (UID-based fallback when Message-ID absent), `sender` = From display name, `sender_id` / `sender_email` = From address (lowercased), `text` = `Subject: ...\n\n<plain body>` with attachment markers appended, `thread_id` = first reference from `References:` (or own Message-ID for orphans), `reply_to_id` = `In-Reply-To`. Raw block keeps `from`, `to`/`cc` (list of `{name, email}` dicts), `subject`, `message_id`, `in_reply_to`, `references`, `folder`, `uid`. Attachments cached by sha1 of the payload (mirrors Pachca/Mattermost), with text files inlined first 5KB. `send_message` does NOT send via SMTP — it builds an RFC 5322 reply (reply-all minus self by default, derived from the parent's From/To/Cc; `Re:` subject; `In-Reply-To` + extended `References`; fresh Message-ID; `text/plain` body) and IMAP-APPENDs it to the configured `drafts_folder` with `\Draft \Seen` flags. `fetch_new` raises `NotImplementedError` and the MCP `_get_new_messages` handler punts with a clear message — IMAP polling is too heavy for live reads in v1.

### Pipeline (`pipeline/`)
- **ingest.py**: Iterates `config["sources"]`, creates one connector + FilterEngine per enabled source, fetches, filters, and stores. Per-source exceptions are caught and recorded; a failing source does not abort the run. Records each run to the `ingest_runs` table with status `ok`/`partial`/`error`. The orchestrator `run_ingest()` is a thin top-level that calls phase helpers (`_initialize_sources`, `_collect_messages`, `_enrich_messages`, `_cache_senders`, `_store_messages`, `_finalize_run`) — each in its own private function for testability.
- **format.py**: Canonical message renderer — `format_message`, `format_timestamp`, `make_message_url`, `get_display_tz`, `ext_marker`. Shared by `mcp_server.server` (re-exports for back-compat) and any future exporter so display style doesn't drift between surfaces.
- **health.py**: Shared health computation (`build_health_report`) and formatting (`format_health_text`). No MCP or connector imports — used by both the CLI and the `get_health` MCP tool so they never drift.
- **filter_engine.py**: Per-source rules — `skip_senders` (matches sender id, display name, AND email address), `skip_channels`, `only_channels`, `skip_patterns`. Email-only keys: `skip_folders` (skip whole folder by name; also matches `channel_id` of the form `folder:<name>`) and `skip_subjects` (regex against the email Subject). No global `skip_sources` (use `enabled: false` instead).

### Scheduling (systemd-based for Linux)
- **bin/memorandum-sync**: Bash script with `flock` for exclusive execution
- **systemd/memorandum-collect.service**: Oneshot systemd service
- **systemd/memorandum-collect.timer**: Triggers every 15 minutes

### Storage (`storage/`)
- **db.py**: SQLite for structured metadata and full-text search
- **vector_store.py**: ChromaDB for semantic embedding search

### MCP Server (`mcp_server/`)

The package is split for navigability:

- **`server.py`** (~280 lines) — `Server` instance, the two decorated entry points (`list_tools`, `call_tool`), shared singletons (`_db` / `_vs` / `_config` / `_resolver`) with their lazy accessors, the `_build_live_connector` helper, `_invalidate_config_cache`, the dispatcher (looks `name` up in `TOOL_HANDLERS`), and `main`. Re-exports every handler/helper by name for back-compat with the test suite's `from mcp_server.server import _<name>` imports.
- **`schemas.py`** — every `Tool(...)` declaration in one place (`tool_schemas()`). What Claude sees when it introspects the server.
- **`projectors.py`** — `TOOL_ARG_PROJECTORS` + `args_summary_for`. Each entry redacts a tool's `arguments` dict to a safe-and-useful shape before it lands in the `tool_calls` audit table (e.g. `send_message` records `text_len`, never the body).
- **`tools/`** — one module per domain, each exporting handler functions plus their domain-local helpers. The package-level `__init__.py` builds the flat `TOOL_HANDLERS` mapping the dispatcher reads:
  - `tools/search.py` — `_search_messages`
  - `tools/digests.py` — `_summarize_channel`, `_summarize_messages`, channel-description helpers
  - `tools/channels.py` — `_list_channels`, `_get_new_messages`, `_send_message`, send helpers
  - `tools/threads.py` — `_get_thread`, `_find_by_issue`, `_who_mentioned`
  - `tools/identity.py` — `_get_user_aliases` + the three `*_user_alias*` write tools + edit guards
  - `tools/files.py` — `_get_attached_file` + cache / Telegram / Mattermost download helpers
  - `tools/info.py` — `_get_stats`, `_get_health`

Tools access shared state via `from mcp_server import server as _srv` then `_srv.get_db()` at call time. The runtime attribute lookup means `unittest.mock.patch("mcp_server.server.get_db")` keeps working through the indirection.

**Concurrency — single tool at a time, by design.** Tool handlers are `async def` (the MCP SDK requires it) but their bodies are entirely synchronous: sqlite, Chroma + BGE-M3 encode, `requests`. None of the heavy calls go through `await` — they block the event loop for their full duration (1–2 s for a semantic search, hundreds of ms for a live HTTP fetch, microseconds for a small SQL query). The practical consequence is that the server processes one tool call at a time even if a client pipelines them. Today the workload is single-client and serial so this is invisible; if you ever see latency complaints or want to handle parallel tool use, wrap the slow calls (`vs.semantic_search`, the connectors' HTTP roundtrips) in `asyncio.to_thread(...)` — `storage.db.Database` already has the RLock to handle threaded sqlite, and Chroma reads are documented safe.

Tools exposed to Claude:
- `search_messages`: Keyword or semantic search. `source` parameter takes a source name (e.g., `company_mattermost`). Supports `mentions_me` boolean filter.
- `summarize_channel`: Get recent messages from a channel.
- `summarize_messages`: Digest of messages from a flexible time range (hours/days), grouped by `[source] channel`.
- `list_channels`: List known channels (`id`, name, source, description) from the `channels` table (excludes the Telegram `__offset__` sentinel). Use it to find the channel id to pass to `get_new_messages` and to read the channel's purpose/topic. Only channels seen by a prior ingest appear.
- `get_new_messages`: Fetch messages **newer than what's already in the DB** for one channel (the gap since the last ingest), live from the source. Builds a read-only connector on demand (`db_callback=db.get_channel`, `db=None` — reads saved sync-state, writes nothing) and calls the connector's `fetch_new(channel, limit)`, then drops any id already stored (`db.exists`). Works for all three source types. Telegram uses a single read-only `getUpdates` at the saved offset **without advancing it**, so the scheduler still consumes and stores those updates (no consume-once loss); it can raise a "busy" message on a `409` clash with a concurrent ingest. `channel` is the channel id (required). Returns only not-yet-stored messages, oldest→newest. Internal/external is classified live in the handler (same layered rule as ingest — see [Internal/external classification](#internalexternal-classification)), so live messages are tagged `[external]` too. This is the read-before-send primitive `send_message` relies on.
- `get_thread`: Reconstruct a full conversation thread (root + replies) by `thread_id`, ordered by timestamp. Matches replies via `messages.thread_id` plus the root via id suffix (`{source}:{thread_id}`). Optional `channel` filter; replies annotated with `reply_to_id`. The `thread_id` to pass is surfaced in formatted message output as a `🧵 thread:{id}` marker whenever a message is a reply.
- `get_attached_file`: Retrieve file content by `file_id`. For Telegram, `file_id` is embedded in message text (e.g. `[photo, file_id=AgAC...]`). Tries cache first, then downloads from Telegram Bot API or Mattermost API. Text files returned inline; binary files (photos, PDFs) as `[base64]:...`.
- `get_stats`: Message counts per configured source.
- `get_user_aliases`: Returns configured alias groups and the current user's aliases. Each entry can carry optional `role`, `team`, `reports_to` (another canonical_name), and `responsible_for` (channels/projects/issue prefixes); these render inline (role/team) and on a follow-on line (reports_to / responsible_for). `[internal]` marks company staff. Read this early in a session to ground references to people — it carries who-is-who context not present in any message line.
- `find_by_issue`: Find messages referencing a YouTrack issue id (e.g. `PL-15491`). Matches both messages whose links resolve to the issue (`raw.urls` populated at ingest) and messages from channels whose name carries the id (`channels.extra.issue_ids`). Issue detection is config-driven via `youtrack.base_url` + `youtrack.project_prefixes`; omit the section to disable detection (URLs are still extracted as generic `"other"`).
- `get_health`: Last ingest run status (`ok`/`partial`/`error`), per-source oldest/last message timestamps and counts, and any recorded errors. Delegates to `pipeline.health`.
- `send_message`: Send a **text** message to a channel (Mattermost/Telegram/Pachca) **or draft an email reply** (email sources). Args: `source`, `channel` (id, not name), `text`, optional `reply_to` (Mattermost root post id / Telegram message id / Pachca parent message id / email original Message-ID). Builds a connector on demand and dispatches by source type; returns JSON `{success, message_id, url}` (`url` via `make_message_url` when resolvable). For email the result also carries `draft: true` plus a note that the message landed in the user's Drafts folder. Two safety rails: (1) **default-deny** — refuses unless the source config sets `allow_send: true`; (2) **read-before-send** — the tool description instructs the agent to call `get_new_messages` for the same channel immediately before sending and to cancel + reconsider if any new messages appeared (anti-stale guard); skipped only for email, since `get_new_messages` punts for IMAP and the human reviews the draft anyway. For Telegram business (private) chats the handler looks up the chat's stored `business_connection_id` (`channels.extra`) and passes it to `sendMessage`, which Telegram requires to reply in a private chat. Mattermost surfaces a clear error on a `429` rate limit (~5 posts/30s). **Email** specifically: `reply_to` is REQUIRED (the parent's `raw.message_id`); the connector looks up the parent in the local DB, derives reply-all To/Cc minus self, builds an RFC 5322 message with proper `In-Reply-To` / `References` / `Re:` subject, and APPENDs it to the configured Drafts folder via IMAP — no SMTP, no live send. Every send attempt — success **and** failure — is logged to the `sent_messages` table (`sent_at`, `source`, `channel`, `reply_to`, `text`, `success`, `message_id`, `error`) via `db.record_sent_message` for audit/debugging. Sending files is not yet supported (see the open file-attachment issue).
- `who_mentioned`: Find messages where a person was @-mentioned. Args: `target` (canonical name, any alias, or `"me"` for the current user), optional `by` (canonical/alias of the author), optional `source` / `since` / `until` / `limit`. Both `target` and `by` are resolved through `AliasResolver` before hitting the DB, so `@bobw` and `Bob Wilson` produce the same query. Rows come from the `mentions` table — populated at ingest by `pipeline.ingest.extract_mentions()` which recognizes `@username` (dots/underscores allowed), Pachca `<@user_id>`, and the auto-converted `@nickname`; broadcast sentinels (`@here`/`@channel`/`@all`/`@everyone`) and email addresses are skipped. Each row stores raw token + canonical form + best-effort `mentioned_sender_id` (lookup against `senders` by `source` + `username`).
- `upsert_user_alias` / `remove_user_alias` / `update_user_alias_strings`: The **agent-writable memory layer about people**. All three write to the `user_aliases:` block in `config.yaml` via `ruamel.yaml` round-trip (operator comments / key order survive), gated by top-level `allow_alias_edits: true` (default true), and hard-refuse canonicals that collide with any `my_aliases` value. After each write the MCP server's cached config is invalidated so the next read tool sees the change. The shared YAML surface lives in `cli/alias_writer.py` (`apply_upsert` / `apply_remove` / `apply_alias_string_change`) and is used by both this MCP path and the `memorandum aliases refresh --in-place` CLI so the two write paths can't drift. Soft caps (`max_entries=500`, `max_aliases_per_entry=50`, `max_list_fields=50`) keep a chatty session from bloating the file. `upsert_user_alias` merges fields into an existing canonical (list fields unioned, scalars overwritten); `update_user_alias_strings` refuses to "steal" an alias owned by another canonical (names the conflicting owner) and refuses to leave an entry with an empty aliases list. Audit trail is `git diff config.yaml`; there's deliberately no separate edits-log table.

All output includes permalinks (only for Telegram channel/supergroup chats — private chats have no usable link) and timestamps converted to `display_timezone`. External (non-company) senders are tagged `[external]` after the name; internal senders are unmarked (see the `internal` column).

## Configuration

`config.yaml` uses a `sources:` dictionary. Each entry has:
- `type`: `mattermost`, `telegram`, or `pachca`
- `enabled`: `true`/`false`
- `internal`: optional `true` — every sender from this source is a company/internal user
- `allow_send`: optional `true` (default `false`) — gates the `send_message` tool; default-deny
- `url` (Mattermost only) and `token` (all types)
- `filters`: per-source `skip_senders`, `skip_channels`, `only_channels`, `skip_patterns`

Top-level keys: `sqlite_path`, `chroma_path`, `attachments_path`, `display_timezone`, `schedule_minutes`, `text_extensions`, `my_aliases`, `user_aliases`, `internal_domains` (list of bare email domains whose owners are treated as internal — promotes any sender/recipient whose email matches), `embedding` (optional model + tuning block — see README → "Swapping the embedding model"), `ingest` (optional concurrency block — `fetch_workers` and `max_fetch_workers`; see [Parallel fetch](#parallel-fetch) below), `youtrack` (optional: `base_url` + `project_prefixes` list — drives both message-URL classification and channel-name issue-id parsing via shared helpers in `pipeline.ingest`).

### Parallel fetch

`run_ingest` fans the per-source `fetch_messages` calls out across a `ThreadPoolExecutor` — sources are I/O-bound (REST roundtrips, IMAP fetches) so wall-clock collapses from ~sum(per-source) to ~max(per-source). The pool size is `ingest.fetch_workers` if set (1 = legacy strictly-sequential path with no executor); auto otherwise (one worker per enabled source, hard-capped by `ingest.max_fetch_workers`, default 8). Per-source error isolation is preserved end-to-end — a connector raising lands in `source_errors` with the usual `{source, error}` shape and never aborts the run. **Writes stay strictly sequential**: the enrichment + insert + sender-cache passes that follow the fetch run on a single thread because the embedding model isn't multi-thread-friendly and SQLite writes serialize at the file lock anyway. The helpers `_fetch_one` / `_fetch_all` / `_resolve_worker_count` live in `pipeline.ingest`.

### Internal/external classification

Layered precedence — broadest → most specific (first match wins later in the list):
1. Source `internal: true` flag — coarse, "everyone here is staff".
2. `internal_domains:` — promotes any sender whose cached email's domain matches (case-insensitive, exact-domain — no wildcards in v1).
3. Per-alias `internal: true|false` in `user_aliases` — wins over both above. `false` lets you **demote** an otherwise-internal sender (e.g. a contractor on `@mycompany.com`); `true` lets you **promote** an external-domain sender (an embedded partner). Distinguished from "absent" by presence of the key.
4. `my_aliases` — the current user is always internal.

`AliasResolver.is_internal(sender, email=None, source_internal=False)` implements this; both `pipeline/ingest.py` (enrichment loop) and `mcp_server/server.py::_get_new_messages` (live path) look up the sender's email from the `senders` cache before calling it, so the domain rule fires whenever email is known (Mattermost / Pachca — Telegram has none). `is_domain_internal(email)` is exposed separately for per-recipient classification used by the email connector.

## Message IDs

- Mattermost: `{source_name}:{post_id}` (e.g., `company_mattermost:abc123xyz`)
- Telegram: `{source_name}:{chat_id}:{message_id}` (chat-scoped to avoid collisions)
- Pachca: `{source_name}:{message_id}` (message IDs are globally unique integers)
- Email: `{source_name}:{Message-ID}` (e.g. `work_email:<abc123@example.com>`); when the email lacks `Message-ID`, a `<uid-{folder}-{uid}@{source}>` synthetic id is used so re-ingest stays idempotent

The `source` field in `messages` and `channels` always stores the **source name** (e.g., `company_mattermost`), never the type.

## File Handling

### Mattermost
Text files are downloaded and embedded inline during ingest (first 5KB). Binary files are downloaded on demand via `get_attached_file`.

### Telegram
File attachment metadata is embedded in message text during ingest as:
- `[photo, file_id=AgAC...]`
- `[document: filename.pdf, file_id=BQACAgIA...]`
- `[voice message, file_id=AwACAgIA...]`
- `[audio: track.mp3, file_id=...]`
- `[video: clip.mp4, file_id=...]`

Text documents are downloaded and shown inline. All other files are downloaded on demand via `get_attached_file`, which calls `getFile` → downloads → caches → returns content. Text files returned as text; binary files as `[base64]:...`.

### Pachca
Each `Message.files` entry is embedded inline as `[image: name, file_id=<id>]` (no long URL), so attachment-only messages are never blank. Because Pachca download URLs are signed and short-lived, the file is downloaded into the cache **at ingest** (while the URL is valid) as `{file_id}{ext}`, and `get_attached_file` serves it from cache by id. Text files also get their first 5KB inlined. File metadata (`id`, `name`, `file_type`) is kept in `raw.files`.

Storage location: `data/attachments/{file_id}{ext}` for all sources (configurable via `attachments_path:` in `config.yaml`). Despite the historical "cache" naming inside the code, this is durable storage — Pachca / Telegram URLs expire, so deleting these files loses attachments permanently. Use `./bin/memorandum prune` for safe, content-addressed cleanup instead of manual deletion.

## Incremental Sync

**Mattermost**: per-channel `last_update_at` (ms timestamp) in the `channels` table. Each run only fetches posts newer than the saved value.

**Telegram**: global `update_id` offset stored in `channels` table under `(source, "__offset__")`. Restarts resume from the next unprocessed update, no duplicates. Collects groups, supergroups, channels (`channel_post`), and business messages; skips bot DMs.

**Pachca**: per-chat newest message ID stored as `last_update_at` (integer) in the `channels` table. Each run fetches messages newest-first and stops when `msg["id"] <= last_update_at`. On first run, fetches the last N messages (default 200) per chat.

Use `--force` to ignore saved state and re-fetch from `--hours` back.

## Database Schema

### messages
`id, source, channel_id, sender, canonical_sender, sender_id, timestamp, text, thread_id, reply_to_id, mentions_me, internal, tags, raw`
- `canonical_sender`: resolved identity from `user_aliases` config (same as `sender` when no alias matches)
- `mentions_me`: 1 if message text contains any `my_aliases` alias
- `internal`: 1 if the sender is a company/internal user — computed in ingest's enrichment pass via `AliasResolver.is_internal(sender, email, source_internal)`. See [Internal/external classification](#internalexternal-classification) for the layered precedence (source flag → `internal_domains` → per-alias verdict → `my_aliases`). External senders are tagged `[external]` in MCP output.

### channels
`id, source, name, display_name, description, team_id, team_name, channel_type, extra, last_update_at, updated_at`
- PK: `(source, id)`
- `description`: human-written purpose/topic — Mattermost `purpose` and `header` joined with " — "; Telegram chat `description` (fetched once per chat per process via `getChat`, cached); Pachca has no equivalent. `upsert_channel` uses `COALESCE` so a connector that doesn't supply one never blanks an existing value. Returned by `list_channels()` and surfaced in the `list_channels` / `summarize_channel` / `summarize_messages` MCP tools (truncated to 300 chars).
- Telegram uses this table for chat metadata and the global offset sentinel (`id="__offset__"`)
- Telegram business (private) chats store their `business_connection_id` in `extra` (JSON) at ingest; `send_message` reads it back via `db.get_channel_row` to reply, since private chats require it
- Pachca uses `last_update_at` to store the newest seen message ID (integer), not a timestamp

### senders
`sender_id, source, username, full_name, email, phone, avatar_url, extra, updated_at`
- PK: `(source, sender_id)`

### sender_aliases
`id, canonical_name, alias, sender_id` — synced from config on each ingest run via `db.upsert_aliases()`

### ingest_runs
`id, started_at, finished_at, status, sources_checked, messages_new, messages_fetched, errors`
- `status`: `ok` (no errors), `partial` (some sources failed but others succeeded), `error` (all sources failed)
- `errors`: JSON array of `{source, error}` dicts — exception message truncated to 300 chars, no tokens

### sent_messages
`id, sent_at, source, channel, reply_to, text, success, message_id, error`
- Append-only audit log of every outbound `send_message` attempt (success and failure), written via `db.record_sent_message()`
- `success`: 1 if sent, 0 if it failed; `message_id` is the source-assigned id on success (NULL on failure); `error` holds the failure string

### mentions
`id, message_id, source, sender_id, sender_canonical, mentioned_token, mentioned_canonical, mentioned_sender_id, created_at`
- One row per `@mention` (or Pachca `<@id>`) found in a message body at ingest. Written by `pipeline.ingest._insert_mention_rows()` only for **newly inserted** messages so re-ingest doesn't double-write.
- `mentioned_token` is the raw form as written (`@john.doe` or `<@12345>`); `mentioned_canonical` is the alias-resolved canonical name (NULL if no alias group matches); `mentioned_sender_id` is a best-effort lookup against `senders` by `(source, username)`.
- Direction is derived at query time (filter by `mentioned_canonical` for "received", by `sender_canonical` for "sent") — no row duplication.
- Backs the `who_mentioned` MCP tool. Consistent with the existing `messages.mentions_me` flag — both are populated in the same enrichment loop.

### prune_runs
`id, started_at, finished_at, cutoff_ts, messages_deleted, mentions_deleted, vectors_deleted, files_deleted, sent_deleted, runs_deleted, error`
- One row per housekeeping run. Doubles as the throttle marker — `Database.last_prune_at()` returns `MAX(finished_at) WHERE error IS NULL`, and `pipeline.housekeeping.run_housekeeping` short-circuits when that's younger than `retention.prune_interval_hours`.
- `vectors_deleted` is the count attempted against chroma (chroma deletes are idempotent — missing-id is not an error). `files_deleted` is the file-cache mark-and-sweep count.
- `error` carries the truncated message when a partial failure occurred (SQL committed but vector store or file cache had a degraded path). The next run retries.
- Written even on partial errors so the operator sees them in the audit trail; never written on a hard SQL-prune failure (in that case the whole transaction rolled back and nothing changed).

### tool_calls
`id, called_at, tool_name, args_summary, duration_ms, success, error`
- One row per MCP `call_tool()` invocation. Written by the wrapper in `mcp_server.server.call_tool` via `Database.log_tool_call()`; logging failures are swallowed so they NEVER abort the tool response.
- `args_summary` is a JSON string built by `_args_summary_for(tool_name, args)` — per-tool redaction map projects sensitive payloads down. **`send_message` records `source`, `channel`, `text_len`, `has_reply_to` — NEVER the body**. `search_messages` caps `query` at 120 chars. Alias-write tools record only `canonical_name`. Tools without sensitive args store them verbatim.
- Bounded by an amortized 30-day prune: every 100 writes the inserter drops rows older than `_TOOL_CALLS_KEEP_DAYS` (30). No separate housekeeping pass needed.
- Backs the "MCP tool usage" panel in `memorandum dashboard`.

## Dashboard

`pipeline/dashboard.py::build_dashboard_snapshot(db, vs, config)` returns the full shape-stable snapshot dict (one key per panel). No `rich` / no MCP / no connector imports — same architectural pattern as `pipeline/health.py` and `pipeline/housekeeping.py`. Reusable as the data source for a future `dashboard_json` MCP tool without duplication.

`cli/dashboard.py` builds the `rich.layout.Layout` (header / row1 storage+ingest+send+tools / row2 source-health+top-channels+top-senders+mentions / row3 messages-per-day+24h-histogram / latest) and runs the `rich.live.Live` refresh loop. Read-only `Database(path, read_only=True)` — opens via `mode=ro` URI; a brief writable bootstrap connect runs SCHEMA first so existing DBs predating new tables (e.g. `tool_calls`) get migrated even when the consumer can't write. The renderer falls back to a vertical compact stack when `console.size.width < 100` or `height < 30`.

Refresh defaults to 5s. `--once` renders a single frame and returns (handy for `watch` / cron snapshots / debugging). Ctrl-C exits via a quiet `KeyboardInterrupt` catch — no traceback.

## Retention / housekeeping

`pipeline/housekeeping.py::run_housekeeping(db, vs, attachments_path, retention_days, prune_interval_hours, dry_run=False)` is the single orchestrator. Lives on the same architectural pattern as `pipeline/health.py` — no MCP / no connector imports — so both the ingest end-of-run hook and the `memorandum prune` CLI route through it.

Cross-store fan-out, in order:
1. SQLite transaction (`Database.prune(cutoff_iso)`): deletes mentions for old messages → messages → sent_messages → ingest_runs, atomically. Returns the deleted message-id list (the caller needs it for chroma).
2. Chroma bulk delete (`VectorStore.delete_many(ids)`): chunked at 5000 ids per call. Per-chunk failures logged + degraded, never raised.
3. Attachments mark-and-sweep: `Database.referenced_file_ids()` regex-scans `messages.text` for `file_id=<...>` markers in the post-prune state; any file in `attachments_path` (default `data/attachments/`) whose stem isn't in that set is unlinked. Content-addressed safe — a file_id referenced by ANY surviving message is kept.
4. `Database.record_prune_run(...)` writes the audit row.

`channels` / `senders` / `sender_aliases` are NEVER pruned (they carry incremental-sync state and historical identity). The vector store and file cache are caller-orchestrated outside the SQL transaction — that way chroma or filesystem misbehavior never blocks the atomic SQL prune.

Dry-run path uses `Database.count_prune_candidates(cutoff_iso)` which mirrors `prune()` shape without writing; the file-cache projection is conservative (may under-report `files_deleted`).

The ingest-end hook (`pipeline/ingest.py`) runs housekeeping ONLY when `run_status in ("ok", "partial")`. Any housekeeping exception is caught and logged — never aborts ingest.

## CLI Commands

Two packages, two roles. `pipeline/` is the **ingest engine** (runs under systemd via `bin/memorandum-sync`); `cli/` is **user-facing utilities** (what a human types).

```bash
# Ingest engine — pipeline/ only.
python -m pipeline [--hours N] [--config config.yaml] [--force]

# User-facing utilities — cli/.
python -m cli health [--config config.yaml] [--json]
python -m cli aliases refresh [--config config.yaml] [--in-place] [--json]
python -m cli prune [--dry-run | --commit] [--days N] [--config config.yaml] [--json]
python -m cli dashboard [--refresh N] [--config config.yaml] [--once] [--no-color]

# Or via the wrapper (resolves the venv automatically):
./bin/memorandum health
./bin/memorandum aliases refresh
./bin/memorandum prune                  # dry-run preview (default)
./bin/memorandum prune --commit         # actually delete
./bin/memorandum dashboard              # live TUI; Ctrl-C exits
./bin/memorandum dashboard --once       # one-frame snapshot
```

Exit codes for `health`: 0=ok, 1=partial/error, 2=never ran.

The library code lives in `pipeline.health` (`build_health_report`, `format_health_text`) and is shared with the MCP server's `get_health` tool — same output, same logic. `cli/health.py` is just the command-line shell.

`aliases refresh` is **append-only**: it scans `messages.sender` for senders not already covered by a `canonical_name` or `aliases` entry in `user_aliases` (case-insensitive), and emits stub entries sorted by message count (most-active first). `--in-place` appends to `config.yaml` using `ruamel.yaml` round-trip so existing comments and key order survive. Existing entries are never edited, reordered, or removed.

`python -m pipeline health` is deprecated and exits with a redirect message pointing at `python -m cli health`. New CLI verbs land under `cli/`.

## Key Design Decisions

1. **Pull-based (polling)**: No webhook endpoint needed
2. **Named sources**: `messages.source` stores the logical name, enabling multiple instances of the same type
3. **Two-tier storage**: SQLite + ChromaDB for different search modes
4. **Local embeddings**: `BAAI/bge-m3` via FlagEmbedding (~4GB, multilingual, ~2-2.5GB RAM) — *default*; the model and tuning are configurable via the `embedding:` block in `config.yaml` (see README → "Swapping the embedding model")
5. **File caching**: Download once, serve from cache
6. **Systemd-based scheduling**: Timer + oneshot service for production Linux, bash with flock for lock protection
7. **Lock protection**: `/tmp/memorandum-sync.lock` prevents overlapping runs
8. **Ingest observability**: Every run is recorded in `ingest_runs`; a failing source is isolated and logged, not fatal to the whole run

## Environment Requirements

- Python 3.11+
- Virtual environment (`.venv`)
- Mattermost Personal Access Token and/or Telegram Bot Token and/or Pachca Personal Access Token
- ~4.5GB disk for model + data (default BGE-M3; smaller with an alternate model)
- ~2-2.5GB RAM for embeddings (default BGE-M3; ~300MB for `BAAI/bge-small-en-v1.5`)

## Testing

The project has a pytest test suite in `tests/`. Run it before and after any code change:

```bash
pytest tests/ -v --tb=short
```

Coverage report (excludes VectorStore — BGE-M3 model too heavy for CI):

```bash
pytest tests/ --cov=. --cov-report=term-missing --ignore=storage/vector_store.py
```

**Key rules for tests:**
- `storage.vector_store` is mocked globally in `tests/conftest.py` — never instantiate the real `VectorStore` in tests.
- Connector tests use the `responses` library to mock all HTTP calls. Never make real network requests in tests.
- Each connector has its own `tests/test_{connector}_connector.py`.
- `tests/test_ingest.py` mocks connector classes with `@patch('pipeline.ingest.{ConnectorClass}')`.

---

## Change tracking

The decision log lives in [`CHANGELOG.md`](CHANGELOG.md) — every landed
feature with a short rationale and the file paths it touched. Read it
backwards from any feature you're modifying to find the original WHY.

For planned work and bug reports, use GitHub Issues (templates provided in
`.github/ISSUE_TEMPLATE/`). For design questions or "how should I approach
X", use Discussions.

The project does not maintain a separate "task system" — issues + the
changelog cover the same ground without a custom convention to learn.
