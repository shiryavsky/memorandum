# How to add a new connector

Memorandum already speaks Mattermost, Telegram, Pachca, and IMAP. This guide is the practical reference for adding a fifth — Slack, Discord, Matrix, Rocket.Chat, Jira comments, anything that has a "list of channels, each with a stream of messages" shape. Read it once end-to-end before you start writing; the surface to satisfy is small, but the *order* and the *invariants* matter.

The guide is structured around the questions you'll actually run into:

1. [Does my source even fit this model?](#1-does-my-source-even-fit-this-model)
2. [The contract — what methods the rest of the system calls](#2-the-contract--what-methods-the-rest-of-the-system-calls)
3. [The message dict shape — what `fetch_messages` must return](#3-the-message-dict-shape--what-fetch_messages-must-return)
4. [The channel-row shape — what to upsert into `channels`](#4-the-channel-row-shape--what-to-upsert-into-channels)
5. [The sender info shape — what `get_sender_info` returns](#5-the-sender-info-shape--what-get_sender_info-returns)
6. [Incremental sync — the per-channel cursor pattern](#6-incremental-sync--the-per-channel-cursor-pattern)
7. [File attachments — caching + inline markers](#7-file-attachments--caching--inline-markers)
8. [Wiring into the four call sites](#8-wiring-into-the-four-call-sites)
9. [Tests you must write](#9-tests-you-must-write)
10. [Documentation you must update](#10-documentation-you-must-update)
11. [Gotchas you will hit](#11-gotchas-you-will-hit)

> Throughout this doc, replace `acme` with whatever your source is called. Filenames, class names, config types — all consistent.

---

## 1. Does my source even fit this model?

Memorandum's storage assumes:

- A source has **channels** (or folders, or chats, or DMs) — discrete buckets a message can belong to.
- A message has a **sender**, a **timestamp**, **text**, and a **stable id** that doesn't change after the message is posted.
- Replies link to a **thread root**; the system already handles that uniformly via `thread_id` + `reply_to_id`.
- Messages can be **fetched incrementally** — given some kind of cursor (timestamp, message-id-greater-than, UID range, update-offset), the source can return only what's new since.

If your source maps cleanly onto those — great, follow this guide. If not, list what's off (IMAP didn't fit cleanly either — see [the email-connector entry in `CHANGELOG.md`](../CHANGELOG.md) for how that was reasoned through, especially the "folder ≠ conversation" point) and open an issue before coding.

**Out of scope for a single connector**:
- Cross-source threading (a thread that spans Mattermost + email) — that's an MCP-layer concern.
- A second backend (e.g. SMTP send for the email connector) — separate task.
- Schema changes to `messages` / `channels` — connectors must fit the existing shape; if you genuinely need a new column, stop and write a task first.

---

## 2. The contract — what methods the rest of the system calls

A connector is a class. The rest of the system only calls these methods:

```python
class AcmeConnector:
    def __init__(self, *, source_name: str, db, db_callback, text_extensions,
                 youtrack_cfg, ...): ...
    def connect(self) -> None: ...                # verify auth, prep state
    def disconnect(self) -> None: ...             # release any sockets/sessions
    def fetch_messages(self,
                       since: datetime | None,
                       force: bool = False,
                       default_since_ms: int | None = None) -> dict: ...
    def fetch_new(self, channel: str, limit: int = 50) -> list[dict]: ...
    def get_sender_info(self, sender_id: str) -> dict: ...
    def send_message(self, channel, text: str, **kwargs) -> str | int: ...
```

That's it. There's no abstract base class — Python ducktyping is enough, and the type annotations live in the dispatch sites (`pipeline.ingest`, `mcp_server.server`). Don't invent extra public methods; if you need helpers, prefix them with `_` (look at `_resolve_chat`, `_fetch_chat_messages`, `_get_user` in the existing connectors).

### Required vs optional methods

| Method | Required for | If you don't implement it |
|---|---|---|
| `__init__`, `connect`, `disconnect` | Always | Won't construct / connect |
| `fetch_messages` | Always | Scheduled ingest does nothing for your source |
| `get_sender_info` | Always (or stub it) | `senders` table never gets populated → mention sender_id resolution fails, `who_mentioned` weakens |
| `fetch_new` | If you want `get_new_messages` MCP live reads | The MCP handler will call it and crash unless you `raise NotImplementedError` — punt cleanly like `EmailConnector` does |
| `send_message` | If you want `send_message` MCP tool | The dispatcher in `_send_message` will refuse for unknown source types — you get nothing for free |

### Constructor convention

Mirror the existing connectors' kwargs so `pipeline.ingest.run_ingest` can construct yours the same way:

```python
def __init__(
    self,
    *,
    source_name: str,
    # Auth — whatever your source needs.
    token: str = None,
    # Filters — only_channels / skip_channels are read by the connector itself
    # (the FilterEngine runs LATER, on the returned messages).
    only_channels: list = None,
    skip_channels: list = None,
    # Incremental-sync state plumbing (read-only callback into the DB).
    db_callback: Callable = None,    # = db.get_channel; returns last_update_at
    db = None,                       # full DB for upsert_channel; None ⇒ read-only
    # Shared options.
    text_extensions: set = None,
    file_cache_dir: str = "data/file_cache",
    youtrack_cfg: dict = None,
):
```

`db_callback=None` and `db=None` together mean **live-fetch mode** (used by `_build_live_connector` for `get_new_messages` and `send_message`) — your connector must not write anything when `db` is `None`. The existing connectors all respect this; mirror the pattern.

---

## 3. The message dict shape — what `fetch_messages` must return

`fetch_messages` returns one dict:

```python
{
    "messages": list[dict],          # the actual rows (see below)
    "messages_count": int,           # len(messages) typically, or higher if you filtered before returning
    "channels_scanned": int,         # how many channels you talked to
    "channels_skipped": int,         # how many you skipped (skip_channels / only_channels)
}
```

Each message dict must look like this — these keys flow straight into `messages` table columns and the vector store:

```python
{
    "id":             "acme:{stable_unique_id}",   # f"{source_name}:{...}" — PRIMARY KEY
    "source":         source_name,                  # e.g. "work_acme"
    "channel_id":     "channel-or-folder-id",       # joins to channels.id
    "sender":         "display name as shown",      # may be the canonical user name
    "sender_id":      "stable-sender-id",           # joins to senders.sender_id
    "timestamp":      "2026-05-30T12:34:56+00:00",  # UTC ISO 8601
    "text":           "message body, plain text",   # see attachment-marker convention below
    "thread_id":      "root-message-id" or None,    # populated on replies; None on root/standalone
    "reply_to_id":    "parent-message-id" or None,  # immediate parent if your source distinguishes
    "tags":           [],                           # rarely used; safe to leave as []
    "raw":            {…},                          # any source-specific JSON you want preserved
}
```

### About `id`

It MUST be unique and stable. The format is `f"{source_name}:{native_id}"` — the leading `source_name:` namespaces ids across sources so two Mattermost instances can't collide. If your source's native id can repeat across channels (e.g. Telegram message ids reset per chat), include the chat id too: `f"{source_name}:{chat_id}:{message_id}"`.

If your source doesn't always give a stable id (rare — IMAP sometimes lacks `Message-ID`), synthesize one deterministically from the fields you do have (`<uid-{folder}-{uid}@{source}>` in `email_connector.py`). Never use `uuid.uuid4()` here — re-ingesting the same message must produce the same id or you'll get duplicates.

### About `timestamp`

UTC ISO 8601 only. Some sources hand you epoch milliseconds — convert. The vector store filters use this string and rely on lexicographic ordering; non-ISO formats silently break time-range queries.

### About `thread_id` / `reply_to_id`

- A standalone message (not a reply, not a root): both NULL.
- A thread root: `thread_id=None`, `reply_to_id=None` (the root is identified by *being pointed to*, not by pointing).
- A reply: `thread_id` = id of the thread's root (the deepest ancestor your source exposes), `reply_to_id` = id of the immediate parent if your source distinguishes that from the root.

Mattermost doesn't expose `parent_id` in modern API responses → `reply_to_id` stays None there. That's fine — `get_thread` groups by `thread_id` and doesn't require `reply_to_id`.

### About `text`

The vector store embeds this and SQL keyword search greps it. Two conventions:

1. **Attachment markers go inline as `[attachment: name.ext, file_id=<id>]`** so the agent reads "Subject: X" → "[attachment: contract.pdf, file_id=…]" → body and can call `get_attached_file(file_id=…)`. Email's `_normalize` (in `email_connector.py`) places these *right after* the subject line so they survive the 200-char preview truncation in `format_message`.
2. **Text-typed attachments inline their first 5KB** after the marker (see `MAX_TEXT_PREVIEW_SIZE` in any connector) so the body is searchable without a follow-up fetch.

### About `raw`

Free-form JSON. Stash whatever the source-specific permalink builder will need (`team_name`, `post_id`, `chat_id`, `message_id`, `subject`, …). `make_message_url(msg, config)` in `mcp_server/server.py` reads from here per-source.

### Optional fields the system also recognizes

| Key | Where it gets used |
|---|---|
| `sender_email` | Picked up by `pipeline.ingest`'s enrichment to feed `AliasResolver.is_internal(email=…)` — saves a `senders` lookup |
| `_recipient_emails` | Email connector emits this; ingest uses it for the "internal only if sender AND every recipient is internal" rule. Pop in your connector if not relevant. |
| `_mentions` | Set by ingest's enrichment, not by the connector. Don't write it from a connector. |

---

## 4. The channel-row shape — what to upsert into `channels`

Inside `fetch_messages` (or wherever you iterate channels), upsert each one via `self._db.upsert_channel(...)` if `self._db` is not None:

```python
self._db.upsert_channel({
    "id":             channel_id_native,         # stable per source
    "source":         self.source_name,
    "name":           "url-safe-name",           # used in some permalinks
    "display_name":   "Friendly display name",   # what list_channels shows
    "description":    "purpose / topic / header" or None,
    "team_id":        None,                      # if your source has teams
    "team_name":      None,
    "channel_type":   "O" | "P" | "D" | "G" | source-specific,
    "extra":          {…},                       # arbitrary per-source JSON
    "last_update_at": int_cursor,                # see "Incremental sync" below
})
```

`extra` is the place to stash per-channel cursors that don't fit `last_update_at` (e.g. IMAP `{"uidvalidity": N, "last_uid": M}`, Telegram business chats' `{"business_connection_id": "..."}`).

**Never delete `channels` rows.** They hold the incremental-sync state — losing one forces a full rescan next ingest.

---

## 5. The sender info shape — what `get_sender_info` returns

Called once per (source, sender_id) when ingest encounters a new sender. Return:

```python
{
    "sender_id":  "stable-id",
    "source":     self.source_name,
    "username":   "handle as used in @mentions",   # CRUCIAL for mention resolution
    "full_name":  "Real Name" or None,             # displayed by some tools
    "email":      "addr@host" or None,             # feeds the internal_domains classification rule
    "phone":      None,                            # rarely meaningful
    "avatar_url": None,                            # not used today; kept for future
    "extra":      {…},                             # anything source-specific
}
```

### `username` is load-bearing

`pipeline.ingest._insert_mention_rows` calls `db.find_sender_id_by_username(source, lookup)` where `lookup` is the bare handle from a `@handle` mention. The match is case-insensitive but **exact** — so make sure your `username` field is the same string the source itself puts in `@mentions`. If your source has multiple "name" fields (login, nickname, display name), pick the one the `@` syntax addresses.

### If your source doesn't have usernames

Some sources only expose user_ids (Pachca shipped `<@123>` syntax, then auto-converted to `@nickname`). Fall back to:
- `username = nickname` if a nickname is exposed.
- `username = sender_id` as last resort (with a note that mentions won't resolve by handle).

For email, `username = sender_id = email_address.lower()` — every email *is* its address.

### Senders are cached BEFORE messages are inserted

This was a real bug we fixed (see git log + the mention-graph entry in `CHANGELOG.md`). `pipeline.ingest.run_ingest` calls `get_sender_info` + `db.upsert_sender` for every unique `(source, sender_id)` pair in the batch **before** it starts the `_insert_mention_rows` loop. That means: if a sender is in the same batch as a message that mentions them, the mention will resolve. **Do not perform per-message side effects in `get_sender_info` that assume the sender will be cached later** — the order is now: cache senders first, insert messages + mentions second.

---

## 6. Incremental sync — the per-channel cursor pattern

The convention: on first ingest, fetch the recent N messages (`default_since_ms` or your own "first run" bound). On every subsequent ingest, only fetch messages with cursor > last-seen cursor. Persist the new cursor via `channels.last_update_at` (or `channels.extra` for richer state).

The two pieces:

```python
# Read the previous cursor:
last = self._db_callback(self.source_name, channel_id) if self._db_callback else None
# Walk the source from `last` to now (or from since/default_since_ms on first run).
# After successful fetch:
if self._db is not None:
    self._db.upsert_channel({
        ...,
        "last_update_at": int(new_high_water_mark),
    })
```

`db_callback` is `db.get_channel` in scheduled mode and `None` in `--force` / live-fetch mode. Respect both:

- `db_callback=None`: ignore saved state, scan from `since` / `default_since_ms`.
- `db=None`: don't write the new cursor (live-fetch mode is read-only).

Examples to copy from:
- **`mattermost_connector.py`** — per-channel `last_update_at` is a millisecond timestamp.
- **`pachca_connector.py`** — per-chat `last_update_at` is the newest message id (Pachca message ids are monotonic).
- **`telegram_connector.py`** — uses a per-source `__offset__` sentinel row in `channels` because Telegram's offset is global, not per-chat.
- **`email_connector.py`** — per-folder `extra = {"uidvalidity": N, "last_uid": M}`; a UIDVALIDITY change forces a full rescan.

---

## 7. File attachments — caching + inline markers

If your source has attachments, follow the established pattern so the existing `get_attached_file` MCP tool serves them without modification:

1. **Cache at ingest** to `data/file_cache/{file_id}{ext}`. The `file_id` should be stable — content-addressed (`sha1(payload)[:24]`) is the cleanest default (see `email_connector._attachment_marker`); some sources hand you their own stable id (Telegram), use that.
2. **Inline a marker** in `text`: `[attachment: <filename>, file_id=<id>]`. Variants the existing connectors use: `[photo, file_id=…]`, `[document: name, file_id=…]`, `[voice message, file_id=…]`. Pick one that reads naturally and stay consistent.
3. **For text-typed attachments** (extension in `text_extensions`), append the first 5KB of the file after the marker so the body is searchable.

`get_attached_file(file_id)` in `mcp_server/server.py` does `for p in cache_dir.iterdir(): if p.stem == file_id` — that's why the on-disk name is `{file_id}{ext}`. Don't put `file_id` in a subdirectory or under a different stem unless you're also patching the lookup.

If your source has expiring signed URLs (Pachca files), download eagerly at ingest. If your source gives stable URLs forever (Mattermost), it's fine to defer the download to first `get_attached_file` call — see how each handles it.

---

## 8. Wiring into the four call sites

After your connector class exists, four sites need the dispatch:

### 8a. `pipeline/ingest.py`

Add an import:

```python
from connectors.acme_connector import AcmeConnector
```

Add a branch in the `for source_name, source_cfg in get_sources(config)` loop alongside the existing `mattermost` / `telegram` / `pachca` / `email` branches:

```python
elif source_type == "acme":
    try:
        connector = AcmeConnector(
            source_name=source_name,
            token=source_cfg["token"],
            # your kwargs from source_cfg
            db_callback=db.get_channel if not force else None,
            db=db,
            text_extensions=text_extensions,
            youtrack_cfg=youtrack_cfg,
        )
        connector.connect()
        filter_engine = FilterEngine(source_filters)
        connectors.append((source_name, connector, filter_engine))
        logger.info(f"[{source_name}] Acme connector initialized")
    except Exception as e:
        logger.error(f"[{source_name}] Failed to initialize: {e}")
        source_errors.append({"source": source_name, "error": str(e)[:300]})
```

The fetch phase runs your connector in a thread pool automatically (see the "Parallelize per-source fetch" entry in `CHANGELOG.md`). The enrichment / insert / sender-cache / `record_ingest_run` passes that follow are unchanged — nothing else to wire there.

### 8b. `mcp_server/server.py::_build_live_connector`

If you want `get_new_messages` (live gap reads) OR `send_message` to work for your source, add a branch:

```python
if source_type == "acme":
    return AcmeConnector(
        source_name=source, token=src_cfg["token"],
        db_callback=db_callback, db=None,           # None ⇒ read-only mode for live fetch
        text_extensions=text_extensions,
        youtrack_cfg=youtrack_cfg,
    )
```

Return `None` for source types you don't support; the handler will surface a friendly "source type X is not supported" message.

### 8c. `mcp_server/server.py::_send_message`

If your connector implements `send_message`, add a dispatch branch in `_send_message` alongside the existing `mattermost` / `telegram` / `pachca` / `email` branches. Follow the same shape:

```python
elif stype == "acme":
    message_id = connector.send_message(channel, text, reply_to=reply_to)
```

The `allow_send: true` config gate is enforced *before* this branch — you get that for free.

### 8d. `mcp_server/server.py::_get_new_messages`

If you can't do live reads cheaply (IMAP punts), refuse early — *before* `_build_live_connector` runs — with a clear message. See the `email` branch around line 870. Otherwise nothing to do; the generic path calls `connector.fetch_new(channel, limit)` and works.

### 8e. `mcp_server/server.py::make_message_url`

If your source has permalinks, add a branch that builds them from `msg["raw"]`:

```python
if source_type == "acme":
    base = config["sources"][source].get("url", "").rstrip("/")
    cid = raw.get("channel_id"); mid = raw.get("message_id")
    if not (base and cid and mid):
        return None
    return f"{base}/c/{cid}/m/{mid}"
```

If permalinks aren't possible (Telegram private chats, etc.) just return `None`; the formatter omits the link.

---

## 9. Tests you must write

Mirror the layout of `tests/test_pachca_connector.py` or `tests/test_email_connector.py`. The HTTP layer should always be mocked (`responses` for `requests`-based connectors; a manual `MagicMock` for non-HTTP libraries — `imap_tools` is a good example).

Minimum coverage:

| What | Why |
|---|---|
| `connect()` succeeds on a 200, raises on auth failure | Catches token misconfig early |
| `fetch_messages()` returns the documented dict shape with realistic data | The shape contract is what the rest of the system relies on |
| `fetch_messages()` with `force=True` ignores saved cursor | `--force` behavior matters for backfills |
| `fetch_messages()` writes the new high-water mark to `channels` | Incremental sync regression guard |
| `fetch_messages()` skips messages older than the cursor | Same |
| Filter integration: `skip_channels` / `only_channels` actually exclude / include | Connector-side filters |
| `get_sender_info()` returns the documented dict | Mention resolution depends on this |
| `send_message()` posts to the right endpoint with the right body | Caught a real bug in Telegram's business-chat handling once |
| `send_message()` surfaces upstream errors with a useful message (rate limits, etc.) | Operator debuggability |
| `fetch_new()` returns only unseen messages, or `raise NotImplementedError` | Whichever you chose |

Plus integration tests in `tests/test_ingest.py` and `tests/test_server.py`:
- `pipeline.ingest.run_ingest` instantiates `AcmeConnector` when `type: acme` is in config.
- `_send_message` dispatches to the acme connector for `type: acme` sources.
- `_get_new_messages` either works or punts cleanly for `type: acme`.

`tests/conftest.py` already mocks `storage.vector_store` globally so the BGE-M3 model never loads. Don't try to test the embedding pipeline; you'd be testing the framework, not your connector.

---

## 10. Documentation you must update

Three files:

1. **`config.example.yaml`** — add a fully-commented `your_acme_source:` entry inside `sources:`, alongside the existing examples. Include all the source-specific keys (token, optional ones, filters) with brief inline comments. Set `enabled: false` so a clean config doesn't accidentally try to connect.
2. **`README.md`** — three places:
   - Top-of-file paragraph: add Acme to the source list.
   - "Multi-source collection" feature bullet: same.
   - "Available Tools (MCP)" / project-structure section: add `acme_connector.py` to the connectors tree.
3. **`AGENTS.md`** — add a paragraph to the `### Connectors (`connectors/`)` section describing your connector's auth, sync model, permalink shape, attachment handling, and any source-specific quirks. Keep it dense; this is reference, not tutorial.

If your connector pulls in a new library, also update **`requirements.txt`** with a pinned version.

---

## 11. Gotchas you will hit

- **`db_callback` vs `db`**: callback is read-only state lookup, `db` is the full handle. Live-fetch mode (`db=None`) must NOT write anything. Forgetting this means `get_new_messages` advances cursors and the next scheduled ingest skips messages.
- **HTTP `Session` reuse**: keep one per connector, not per call. Connectors are constructed once per ingest run; a per-call session reopens the TLS handshake on every channel.
- **Rate limits**: catch the source's specific 429 / quota error and surface a clear message. Don't retry blindly; the scheduler runs again in 15 minutes anyway.
- **Pagination cursors**: prefer "newest-first, stop when you hit `last_seen_id`" over "oldest-first, paginate to end" — the latter degenerates into "re-pull years of history" the first time the cursor is wrong (this exact bug bit the email connector — see git log).
- **Timezone**: store UTC. The display layer converts to `display_timezone`. Naïve local-time strings break the `messages.timestamp` range filters.
- **`only_channels` semantics**: must be source-internal (you skip channels the user didn't whitelist), not post-filter. The `FilterEngine` runs after fetch and only does message-level filtering. Fetching everything just to drop it wastes API quota.
- **Idempotent re-ingest**: re-running ingest must not duplicate messages. The PRIMARY KEY on `messages.id` prevents row-level dupes, but if your `id` includes a timestamp (don't) or anything else volatile, re-ingest produces ghosts.
- **`text` must be non-empty for vector storage**. `vs.insert` skips empty-text messages silently. If your source has system events / join notifications with no body, either drop them in your normalize step or accept they live in SQLite but not in semantic search.
- **The `__offset__` sentinel pattern** (used by Telegram): if your source uses a global cursor instead of per-channel, write a special channel row with `id="__offset__"`. The MCP read tools have explicit filters that exclude rows where `id = '__offset__'` so the sentinel doesn't show up in `list_channels` output.
- **Logging prefix**: every `logger.info` / `warning` / `error` line in a connector should start with `[{self.source_name}]`. Concurrent fetch interleaves log lines; the prefix is the only way to tell whose log this is.

---

## When in doubt, copy

The four existing connectors collectively cover almost every quirk you'll encounter:

| For an example of… | Read |
|---|---|
| REST API with token auth and per-channel sync | `mattermost_connector.py` |
| Long-polling / global update offset / business chats | `telegram_connector.py` |
| Cursor-based pagination, 1-on-1 chats, expiring file URLs | `pachca_connector.py` |
| Folder model, MIME parsing, draft-via-APPEND for send | `email_connector.py` |

Pick the closest fit, copy its skeleton, then adapt. The shape converges naturally because the contract is small.
