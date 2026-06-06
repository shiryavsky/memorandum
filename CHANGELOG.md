# Changelog & Decision Log

This file replaces the historical `tasks/` folder. Each entry below is one
landed change, ordered roughly newest-first. The framing is half changelog
("what shipped"), half decision log ("why it was built that way and where
the code lives") — so a contributor can read backwards from any feature
they're touching and find the original rationale.

For day-to-day "what changed in v1.2 vs v1.1" you can `git log --oneline`.
This document captures the WHY that doesn't fit in commit messages.

---

## Ingest lookback becomes an explicit `ingest.lookback_minutes` knob

The default `since` window used by new channels (no saved state yet) and
by `--force` runs is now a single config value: `ingest.lookback_minutes`
(default **75** = 15-min systemd timer cadence + 1h safety overlap, so a
run that starts late still covers the previous window).

- **Why.** The lookback was previously decided in two non-obvious places.
  `bin/memorandum-sync` extracted `schedule_minutes` from `config.yaml`
  and computed `HOURS = schedule_minutes * 2 / 60` (→ ~30 min); Python's
  `run_ingest` had its own hardcoded 20-min `timedelta` fallback if
  `since` wasn't passed; the CLI's `--hours` default was `0.33`. Three
  numbers that all meant "the default lookback," only one of which the
  operator could change from `config.yaml` — and that key (`schedule_minutes`)
  wasn't even read by any Python code, so most callers were already on
  the 20-min path. One knob, one number, one place.
- **Why 75 and not 30.** The previous ~30-min effective default left
  almost no margin — a systemd run that started 20 minutes late
  (load, lock contention, network blip) would already be skating the
  edge of the prior window. 1h of overlap on top of the 15-min cadence
  is cheap (connectors dedupe on `db.exists`, so re-fetched messages
  are dropped before insert) and survives realistic operational delays.
- **Why a CLI override stayed.** `python -m pipeline --hours N` still
  wins when set — useful for backfills and one-off debugging. When
  unset, the CLI passes `since=None` to `run_ingest`, which then
  resolves from the config.

Touchpoints: `config.py` (`get_ingest_settings` returns
`lookback_minutes`, min-clamped to 1, falls back to 75 on bogus
values), `pipeline/ingest.py` (single fallback site reads
`ingest_settings["lookback_minutes"]`; CLI `--hours` default is now
`None` so config wins), `bin/memorandum-sync` (dropped the
`schedule_minutes` extraction and the `* 2` math; only forwards
`--hours` when the operator sets it), `config.example.yaml` (new
`lookback_minutes: 75` line in the `ingest:` block with the rationale),
`AGENTS.md` (top-level key list updated), `systemd/memorandum-collect.timer`
(stale "schedule_minutes is NOT used here" warning removed).

---

## Credentials move out of `config.yaml` into `/etc/memorandum/secrets.yaml`

**Breaking change.** Per-source tokens / passwords are no longer kept
inline in `config.yaml`. They live in a separate YAML file, default
`/etc/memorandum/secrets.yaml` (`chmod 600`), and are shallow-merged
into `config["sources"][name]` at load time. Override the path via
`secrets_path:` in `config.yaml` or `MEMORANDUM_SECRETS_PATH`.

- **Why.** The MCP server runs as the user and can read anywhere
  the user can; the **agent side** (Claude Desktop / Claude Code /
  any filesystem-capable MCP server the user has wired in) is
  typically allowlisted to the project / home directory by its own
  sandbox. Putting credentials under `/etc/` puts them physically
  outside that allowlist — a misbehaving or future filesystem tool
  can't grep them, and the agent's own path-traversal can't escape
  its sandbox. This is a harness boundary, not a UNIX-permissions
  one, but it's the boundary the agent actually respects.
- **Why no backward-compat for inline tokens.** Keeping the inline
  path "for a release or two" mostly invites the operator to never
  migrate. A hard break with a clear connect-time error and a
  short README migration recipe is friendlier than a soft warning
  no one reads.

**Migration (one-time):**

```bash
sudo mkdir -p /etc/memorandum
sudo install -m 600 -o $USER secrets.example.yaml /etc/memorandum/secrets.yaml
sudo $EDITOR /etc/memorandum/secrets.yaml   # paste your tokens
# then strip `token:` / `password:` lines from config.yaml
```

A missing secrets file is fine — connectors that need a credential
fail at connect-time with the existing clear error. Unknown source
names in the secrets file are logged + ignored (typo defense, rather
than silently creating a phantom source on every load).

- **Loader shape.** `config.load_config` reads `config.yaml`,
  resolves `secrets_path` (config key → env var → default), reads
  the secrets file if present, and merges `sources[name]` entries
  into the main config. Everything else (filters, urls, retention,
  user_aliases) keeps its existing shape.
- **What stayed in `config.yaml`.** Source structure (`type`,
  `url`, `host`, `port`, `username`, `enabled`, `allow_send`,
  `internal`, `filters`), plus the unrelated config (display
  timezone, retention, ingest concurrency, YouTrack, aliases).
- **What's in `secrets.yaml`.** Just `token:` per chat source and
  `password:` per email source. Top-level shape mirrors
  `sources[name]`. A new `secrets.example.yaml` ships in the repo
  (no real credentials).

Touchpoints: `config.py` (loader + merge), `config.example.yaml`
(inline tokens removed, secrets pointer added), `secrets.example.yaml`
(new), READMEs in 4 languages (setup recipe + "Why a separate secrets
file" section), `AGENTS.md` (Configuration section). 639 tests pass;
four new tests cover the merge / env-var / missing-file / unknown-
source paths.

---

## Connectors: unified send-side surface + structural protocol

Two related changes that together let the MCP dispatcher stop knowing
which source it's talking to.

**Unified `send_message` + `message_url`.** Before R10, each connector
had its own reply-target kwarg — Mattermost's `root_id`, Telegram's
`reply_to`, Pachca's `parent_message_id`, email's `reply_to` — and the
dispatcher in `tools/channels.py` had a four-arm switch to bridge the
generic ``args["reply_to"]`` to each platform's name. URL building lived
in a `_sent_message_url` helper that knew the permalink shape of three
sources by hand. Both got pulled into the connectors:

- `send_message(channel, text, reply_to=None)` — every connector takes
  the same kwarg. Each renames + coerces internally (Mattermost
  emits `root_id` on the wire; Telegram and Pachca cast the value
  to `int`).
- `message_url(channel, message_id) -> str | None` — every connector
  builds its own permalink (or returns `None` for Telegram private
  chats / email drafts / unresolved Mattermost channels).
- `_build_live_connector` now passes the live DB handle to every
  connector (was `db=None` for non-email before). The MCP server is
  no longer special-casing the email path; connectors guard their
  own writes during read-only flows the way they always did.
- Platform-specific glue moved into the connector that owns it:
  Telegram's `business_connection_id` lookup off the channel row now
  happens inside `TelegramConnector.send_message` itself when no
  bcid is passed explicitly.

The dispatcher collapses to:

```python
message_id = connector.send_message(channel, text, reply_to=reply_to)
url = connector.message_url(channel, message_id)
```

**Structural protocol (`ConnectorProtocol`).** A PEP-544
`@runtime_checkable` Protocol in `connectors/_common.py` documents the
surface every source implements — `connect` / `disconnect` /
`fetch_messages` / `fetch_new` / `send_message` / `message_url` /
`get_sender_info`. No inheritance; the four shipped connectors satisfy
it by shape. `factory.build_connector` now declares
`-> Optional[ConnectorProtocol]` as its return type. A new
`tests/test_connector_protocol.py` parametrizes over the four classes
and asserts `isinstance(instance, ConnectorProtocol)` — catches a
missing or renamed method before it lands.

**On the contract details.** ``fetch_new`` is allowed to raise
`NotImplementedError` (Email does — IMAP polling is too heavy for an
interactive tool call). The MCP `get_new_messages` handler catches that
and punts; an empty list is reserved for "no new messages." This stays
explicit instead of being collapsed to `None` — callers already
special-case the raise.

Touchpoints: all four connectors, `connectors/_common.py`,
`connectors/factory.py`, `connectors/__init__.py`,
`mcp_server/server.py::_build_live_connector`,
`mcp_server/tools/channels.py`, plus connector-level test updates and
the new protocol contract test. 635 tests pass.

---

## MCP server split: `schemas.py`, `projectors.py`, `tools/`

`mcp_server/server.py` had grown to 1717 lines — `Server` setup, 16
`Tool()` schemas (~415 lines), 16 async tool handlers and their helpers
(~960 lines), the `tool_calls` args projectors (~50 lines), the
dispatcher, and `main`. Everything was discoverable only by scrolling.
Refactored into a small package:

- **`mcp_server/server.py`** (now ~280 lines) — `Server` instance,
  decorated `list_tools` / `call_tool`, the lazy accessors
  (`get_db` / `get_vs` / `get_config` / `get_resolver`),
  `_invalidate_config_cache`, `_build_live_connector`, the dispatcher,
  and `main`. Re-exports every moved handler by name so
  `from mcp_server.server import _<handler>` test imports keep working.
- **`mcp_server/schemas.py`** — `tool_schemas()` returns the list of
  `Tool(...)` declarations. Adding/editing a description no longer
  means scrolling past every handler body.
- **`mcp_server/projectors.py`** — `TOOL_ARG_PROJECTORS` +
  `args_summary_for()`. Each entry redacts a tool's args before they
  land in `tool_calls` so the dashboard's MCP-usage panel doesn't leak
  message bodies, search queries, or alias payloads.
- **`mcp_server/tools/`** — one module per domain, each owning its
  handlers and domain-local helpers; the package `__init__.py`
  collects them into a flat `TOOL_HANDLERS` mapping the dispatcher
  reads:
  - `search.py`   — `_search_messages`
  - `digests.py`  — `_summarize_channel`, `_summarize_messages`
  - `channels.py` — `_list_channels`, `_get_new_messages`, `_send_message`
  - `threads.py`  — `_get_thread`, `_find_by_issue`, `_who_mentioned`
  - `identity.py` — `_get_user_aliases` + the three alias-write tools
  - `files.py`    — `_get_attached_file` + download helpers
  - `info.py`     — `_get_stats`, `_get_health`

- **Why a runtime indirection for shared state.** Tools import the
  server module (`from mcp_server import server as _srv`) and call
  `_srv.get_db()` at runtime rather than binding the function at
  import time. The attribute lookup makes
  `unittest.mock.patch("mcp_server.server.get_db")` continue to work
  through the move — all 72 existing test patches stayed unchanged.
- **Why `_build_live_connector` and `_invalidate_config_cache` stayed
  in `server.py`.** The first is patched at that import path by
  multiple tests; the second mutates server's module globals.

626 tests pass unchanged at every commit of the split (extracted in
three steps: schemas, projectors, then handlers). Touchpoints:
`mcp_server/server.py`, the three new modules above, and the
`mcp_server/tools/` package.

---

## Refactor pass: smaller modules, registries, fewer god functions

A batch of medium-sized cleanups that landed together; each commit is
independently green against the 626-test suite, but the rationale is
shared.

- **Shared connector constants → `connectors/_common.py`.**
  `MAX_TEXT_PREVIEW_SIZE = 5 * 1024` was repeated verbatim in all four
  connector files. `DEFAULT_TEXT_EXTENSIONS` was defined twice
  (Mattermost + Telegram) and absent from Pachca + Email — those two
  fell back to `set()`, so a caller that didn't pass `text_extensions`
  got no inline text previews. One source of truth now; Pachca and
  Email default to the same set as the others.
- **Canonical message renderer → `pipeline/format.py`.**
  `format_message`, `format_timestamp`, `make_message_url`,
  `get_display_tz`, and `ext_marker` lived on `mcp_server/server.py`.
  They're not MCP-specific — the dashboard and a future Markdown
  exporter need them too. Moved out; `mcp_server.server` re-imports
  the same names so existing test imports keep working without churn.
- **`AliasResolver` caching** — `get_resolver()` in `mcp_server.server`
  builds the resolver lazily and keys the cache on `id(config)`, so
  `_invalidate_config_cache` and test patches of `get_config()` both
  trigger a rebuild without anyone remembering to null out an extra
  field.
- **Dispatcher registry** — the 36-line `if/elif` ladder in
  `_dispatch_tool` was replaced by a `{name: handler}` dict. Adding a
  tool is now one line. Mirrors the projector dict that already lived
  beside it.
- **`build_connector` factory in `connectors/factory.py`** — both
  `pipeline.ingest` and `mcp_server.server._build_live_connector` had
  near-identical four-arm `if source_type == …` ladders. One factory
  drives both call sites; ingest's connect/connector-init split is
  also cleaner now (build, connect, attach FilterEngine are separate
  try blocks). Tests that patched
  `pipeline.ingest.<X>Connector` now patch
  `connectors.factory.<X>Connector` — the import moved with the call
  site.
- **`run_ingest` split into phases.** The 320-line function became a
  ~75-line orchestrator that calls private phase helpers
  (`_initialize_sources`, `_collect_messages`, `_enrich_messages`,
  `_cache_senders`, `_store_messages`, `_finalize_run`). The shared
  `stats` dict still flows through in-place mutation so the contract
  with existing tests stays identical.

---

## Project audit: six small bug fixes

A focused sweep prompted by a project-wide read-through. None of these
were the kind of thing that would surface as a loud failure — they're
exactly the cases where the symptom is "intermittent" or "didn't do
anything when I expected it to."

1. **`daily_digest` was wired but unreachable.** The dispatcher had
   an arm for it and the args projector had a redaction entry, but
   `list_tools()` never published a `Tool(...)` declaration — clients
   never saw it. Removed entirely; `summarize_messages(hours=24)`
   covers the same surface.
2. **`connectors/__init__.py` re-exported only Mattermost + Telegram.**
   `from connectors import PachcaConnector` (or `EmailConnector`)
   raised `ImportError` despite the classes living in the package.
   Aligned the `__all__` list.
3. **`pipeline.scheduler --systemd` did nothing on macOS.** `main()`
   set `os.environ["SYSTEMD_MODE"]` and called `run_scheduler`, but
   `_is_systemd_mode()` only inspected `DISABLE_SYSTEMD_CHECK` and
   `/run/systemd/system` — never the env var. The flag was dead.
   Print instructions and exit directly.
4. **The fallback scheduler had no flock.** Polling-mode `run_ingest`
   shared SQLite and Chroma with whatever `bin/memorandum-sync` or
   `reindex-chroma` might be doing, but never took the lock. Acquires
   the same `/tmp/memorandum-sync.lock` per tick; skips with a
   warning if it's held.
5. **`_invalidate_config_cache` only cleared `_config`.** `_db` and
   `_vs` were built from `_config["sqlite_path"]` /
   `_config["chroma_path"]` — a path change in the new YAML would be
   silently ignored, the next `get_db()` returning the old handle.
   Now closes the DB and resets both handles (and `_resolver`) so
   the next call reopens against the current config.
6. **`export/` package was aspirational only.** A single function
   with `TODO: implement this` and no caller. A stub that implies a
   feature exists is worse than no module — deleted.

---

## Removed `find_decisions` MCP tool

The tool ran a semantic search against the vector store using a hardcoded
English seed string — `"decision conclusion agreed action item todo {topic}"`
— and returned the top-30 nearest embeddings unfiltered. In practice this
was unreliable to the point of being misleading, and the failure modes are
structural rather than tunable:

- **Language mismatch.** Teams using the tool talk in Russian (and other
  non-English languages); real decision phrases like «решили», «договорились»,
  «беру на себя» live far from the English seeds in BGE-M3's vector space.
  The model is multilingual, but cross-lingual NN retrieval against
  contextual phrases is lossy.
- **Marker-free decisions.** Most real decisions in chat don't contain
  `decision:` / `action item:` style markers — they look like ordinary
  messages ("ок, давай так"). There's no surface a vector search can latch
  onto.
- **No rerank, no LLM filter.** Top-30 by cosine across a 26K+ corpus is a
  noise sample, not a curated set.
- **Better in practice without it.** `summarize_messages` /
  `summarize_channel` return windowed digests that the calling LLM reads
  whole, and the LLM extracts decisions from semantic context — i.e. it
  does the judgement the vector search couldn't. Keeping `find_decisions`
  alongside that path just invited callers to use the worse tool.

The contract was wrong, not the implementation — wrapping the same query
in a better prompt or a multilingual seed list would still be pattern
matching dressed up as decision detection. Removing the surface is the
honest fix; callers compose `summarize_*` + `search_messages` themselves.

- **Touchpoints:** `mcp_server/server.py` (tool schema, dispatcher arm,
  `_args_summary_for` projector, `_find_decisions` implementation),
  `AGENTS.md`, README in 4 languages.

---

## Thread-safe `Database` (eliminates `SQLITE_MISUSE` under fan-out ingest)

The `memorandum-collect` log was periodically showing `bad parameter or
other API misuse` — that's SQLite errno 21 (`SQLITE_MISUSE`), surfaced when
multiple threads use the same `sqlite3.Connection` without external
serialization. We open a single connection in `Database.__init__` with
`check_same_thread=False`, but the pipeline fans out per-source fetches via
`ThreadPoolExecutor` in `pipeline/ingest.py`, and the connectors call
`db.get_channel` / `db.upsert_channel` / `db.get_channel_row` / `db.get_by_ids`
from inside `fetch_messages` — i.e. from the worker threads. When two
workers hit `self.conn.execute()` at the same instant, sqlite3's
cursor/statement state goes inconsistent and errno 21 fires. Intermittent
because it only triggers on actual interleaving.

- **Why a single lock and not per-thread connections:** the workload is
  network-bound, the DB is not the bottleneck; a `RLock` is the smallest
  diff that eliminates the race and keeps the existing call sites
  untouched. WAL + `threading.local()` connections would be cleaner
  long-term but require per-thread bootstrap and careful close semantics,
  with no observable win at current scale.
- **Why `RLock` not `Lock`:** several methods are reentrant —
  `log_tool_call` calls `_prune_tool_calls` when the counter ticks over;
  `get_or_fetch_sender` calls `get_sender` and `upsert_sender` in
  sequence. Plain `Lock` would deadlock the moment a thread tries to
  acquire it twice.
- **What changed:**
  - `storage/db.py`: added `_synchronized` decorator that takes
    `self._lock`; applied to every method that touches `self.conn` (46 in
    total). `_lock` is a `threading.RLock` created in `__init__` before
    the connection is opened.
  - `__exit__` now routes through `self.close()` (decorated) instead of
    calling `self.conn.close()` directly, so context-manager teardown is
    also serialized.
- **`check_same_thread=False` is kept on purpose** — it suppresses
  Python's safety assertion; the lock provides the actual thread safety.
- **Verification:** 8 threads × 200 mixed read/write iterations completes
  in ~0.5s with zero errors; without the lock the same pattern reliably
  surfaces SQLITE_MISUSE on a loaded box. Full test suite (626 tests)
  passes unchanged — the decorator wrapper preserves signatures via
  `functools.wraps`.
- **Touchpoints:** `storage/db.py`.

---

## Channel filter unifies id / name / display_name + `reindex-chroma`

`search_messages` (semantic mode) was returning zero hits when a `channel`
filter was passed — a 26K-vector Chroma collection somehow matched none of
them. Two compounding bugs:

1. `vector_store.insert` was writing `channel: ""` for every row. The
   message dicts emitted by all four connectors carry `channel_id`, not
   `channel`, so `msg.get("channel") or ""` always evaluated to the empty
   string. Every embedding's `channel` metadata was therefore blank, and
   any non-empty filter trivially excluded the entire collection.
2. `_search_messages` was passing the user's `channel` argument through
   verbatim. For Chroma the metadata key is the channel **id**, so a
   user asking for `ekaterina.firsova` (a Mattermost `name`) or
   `Ekaterina Firsova` (a `display_name`) couldn't possibly match — only
   the raw id would. The keyword path's `db.search` had a similar gap:
   its WHERE matched `display_name` and `name` but not `id`.

- **Fix:**
  - `vector_store.py`: fall back to `channel_id` so existing connector
    output produces the right metadata on insert.
  - `db.py`: extended `search()`'s channel clause to also match `c.id`
    (mirroring `get_thread`); added `resolve_channel()` helper.
  - `mcp_server/server.py`: resolve channel once at the top of
    `_search_messages`, pinning `source` from the resolved row to
    disambiguate cross-source name collisions. Both semantic and keyword
    branches now use the canonical id.
- **Operational consequence:** the insert-side fix only helps **new**
  rows. The existing 26K embeddings still hold `channel: ""` until they
  get re-embedded.
- **New CLI verb — `./bin/memorandum reindex-chroma`:**
  - Acquires the same `/tmp/memorandum-sync.lock` that
    `bin/memorandum-sync` uses (via `fcntl.flock(LOCK_EX|LOCK_NB)`), so a
    sync run blocks the reindex cleanly instead of racing it.
  - `shutil.rmtree` on the configured `chroma_path`, then rebuilds
    `VectorStore` and streams every row from SQLite directly with a raw
    SELECT (no JOIN — the friendly `COALESCE(display_name, name,
    channel_id)` that `db.search` synthesizes would otherwise leak into
    the metadata). Combined with the insert-side fix, channel metadata
    is now correct end-to-end.
  - Exit codes: 0 ok, 1 partial (some rows failed), 2 lock held / config
    missing.
- **Touchpoints:** `storage/vector_store.py`, `storage/db.py`,
  `mcp_server/server.py`, `cli/__main__.py`, `cli/reindex.py` (new),
  README in 4 languages.

---

## Attachments path is now configurable (renamed from "file cache")

New top-level config key `attachments_path:` (parallel to `sqlite_path` /
`chroma_path`) controls where downloaded message attachments live on disk.
Default changed from `data/file_cache/` → `data/attachments/`. The internal
parameter `file_cache_dir` was renamed `attachments_path` across all
connectors and the housekeeping orchestrator for consistency.

- **Why:** two problems with the old setup. (1) The path was undocumented
  and partially hardcoded — `mcp_server/server.py::_get_attached_file` had
  `Path("data/file_cache")` baked in, so the MCP server couldn't actually
  read from a custom location even if you set it elsewhere. (2) The "cache"
  naming implied it was safe to delete, but it isn't: Pachca file URLs
  expire shortly after the message is posted, Telegram bot file URLs time
  out after ~1 hour. Deleting the directory loses attachments referenced by
  surviving messages.
- **What changed:**
  - `config.yaml` accepts `attachments_path: "data/attachments"` (new default).
  - `_get_attached_file` in the MCP server now reads the config — bug fix.
  - The 4 connectors take `attachments_path=` instead of `file_cache_dir=`.
  - `run_housekeeping` takes `attachments_path=` instead of `file_cache_dir=`.
  - Internal sweep helpers (`_file_cache_sweep`, `_file_cache_projection`,
    `DEFAULT_FILE_CACHE_GRACE_SECONDS`) keep their names — they describe the
    mark-and-sweep mechanism, not the data, and renaming had no user-visible
    value. The `file_cache_grace_minutes:` retention config key likewise
    stays — it's about the grace period, not the path.
- **Migration for existing installs:** either rename the directory and rely
  on the new default —

  ```bash
  mv data/file_cache data/attachments
  ```

  — or keep the old layout by pinning the path in `config.yaml`:

  ```yaml
  attachments_path: "data/file_cache"
  ```

- **Touchpoints:** `config.example.yaml`, `pipeline/ingest.py`,
  `pipeline/housekeeping.py`, `cli/prune.py`, `mcp_server/server.py`, all 4
  files in `connectors/`, the corresponding tests, `README.md`, `AGENTS.md`,
  `connectors/CONTRIBUTING.md`.

---

## CPU-only torch on Linux install

`setup.sh` now pre-installs torch from the PyTorch CPU index
(`https://download.pytorch.org/whl/cpu`) on Linux before resolving the rest
of `requirements.txt`.

- **Why:** `FlagEmbedding` depends on `torch`, and pip's default torch wheel
  on Linux ships the full CUDA runtime bundle (cudnn, nccl, cusparselt,
  cuda-toolkit, etc.) — ~1.3 GB of NVIDIA wheels that are dead weight on a
  CPU-only server. First-run installs were pulling all of it down even
  though Memorandum runs BGE-M3 fine on CPU.
- **How:** a `uname -s` guard runs `pip install --index-url …/whl/cpu torch`
  before `pip install -r requirements.txt`. Once CPU torch is satisfied, the
  resolver doesn't fetch the bundled-CUDA wheel via FlagEmbedding's transitive
  dep. macOS is untouched (torch there is already CPU).
- **Migration for existing broken venvs:** `pip uninstall -y torch nvidia-* cuda-toolkit && pip install --index-url https://download.pytorch.org/whl/cpu torch && pip install -r requirements.txt`,
  or wipe `.venv/` and re-run `./setup.sh`.
- **Touchpoints:** `setup.sh`.

---

## Live terminal dashboard

`memorandum dashboard` — full-screen `rich` TUI refreshing every 5s, showing
storage / ingest / source health / mentions / send activity / MCP tool usage
in one view. Designed to live in a tmux pane.

- **Why:** answering "is ingest healthy? am I being @-mentioned a lot today?
  is the vector store in sync with SQLite?" used to mean four terminals and
  manual SQL. One screen is dramatically faster.
- **Architecture:** `pipeline/dashboard.py` is pure data (no rich imports),
  `cli/dashboard.py` is the rich `Layout` + `Live` refresh loop on top. The
  data layer is reusable as a future `dashboard_json` MCP tool without
  duplication.
- **Prerequisite:** the MCP server now logs every `call_tool()` invocation
  into a new `tool_calls` table with a per-tool args-summary redaction map.
  `send_message` records `source` / `channel` / `text_len` / `has_reply_to` —
  **never** the body. Bounded by amortized 30-day prune.
- **Read-only DB connection** via SQLite `mode=ro` URI; bootstrap-writable
  connect runs schema first so existing DBs predating new tables get migrated
  even when the consumer can't write.
- **`--mock` flag** renders a hard-coded snapshot for README screenshots —
  no DB / Chroma / config needed.
- **Bar charts:** 5-row Unicode partial-block bars (`▁▂▃▄▅▆▇█`), 90 days
  daily + 24h hour-of-day. Current-hour caret on a separate row below the
  labels (NOT a yellow fill on the bar — that bar shows a 14-day average,
  not "now").
- **Touchpoints:** `cli/dashboard.py`, `pipeline/dashboard.py`,
  `storage/db.py` (`tool_calls` table + 8 read helpers + `read_only=True`),
  `mcp_server/server.py::call_tool` (timer + logger wrapper + redaction map).

---

## Data retention / housekeeping

After each successful ingest, prune messages / mentions / sent_messages /
ingest_runs older than `retention.retention_days`. Cross-store fan-out:
SQLite transaction → chroma delete-by-id → file-cache mark-and-sweep.

- **Why:** without a prune path, the DB and file cache grow monotonically
  forever — Pachca attachments alone can fill a disk in a year.
- **Cross-store coordination:** a message lives in 4 places (SQLite row,
  mentions rows, chroma vector, cache file). SQLite delete is the source
  of truth; chroma and the filesystem are caller-orchestrated outside the
  transaction so external failures stay degraded, not fatal.
- **Content-addressed file sweep:** a file_id referenced by ANY surviving
  message is kept, even if the message that originally added it is being
  pruned. Mark-and-sweep over the cache dir, not a reference-count table —
  simpler, no extra writes at ingest time.
- **Split file-deletion counts** (`files_with_deleted_messages` vs
  `files_orphans_swept`) so the operator can tell retention from
  filter/duplicate cleanup at a glance.
- **1h grace period** on the file sweep — protects a just-downloaded Pachca
  file (expiring URL) from being reaped before its message lands.
- **`channels` and `senders` are NEVER pruned** — they hold incremental-sync
  state and historical identity. Always keep.
- **Opt-in:** retention is disabled unless the operator adds a `retention:`
  block. Avoids silently deleting data on first install.
- **Operator preview** via `memorandum prune --dry-run` (default) /
  `--commit` (actually delete) / `--days N` override / `--json`.
- **Throttled** to once per `prune_interval_hours` (default 24h) so it
  doesn't run on every 15-minute ingest tick.
- **Touchpoints:** `storage/db.py` (`prune_runs` table + `prune` + helpers),
  `pipeline/housekeeping.py`, `cli/prune.py`, `pipeline/ingest.py` (end-of-run
  hook).

---

## Agent-writable `user_aliases` via MCP

Three MCP tools (`upsert_user_alias`, `remove_user_alias`,
`update_user_alias_strings`) let the agent persist what it learns about
people directly into `config.yaml`.

- **Why:** the alias system started read-only. When the agent notices "Jane
  moved from Platform to Mobile" it had no way to remember that. Now it's a
  durable memory layer about people.
- **Storage location:** `config.yaml` (not a DB table). Single source of
  truth, operator can still hand-edit, `git diff` is the audit trail.
- **YAML round-trip via `ruamel.yaml`** preserves operator comments / key
  order / quoting. The shared `cli/alias_writer.py` surface is used by both
  the MCP tools and the existing `memorandum aliases refresh --in-place` CLI
  so the two write paths can't drift.
- **Cache invalidation:** after a write, the MCP server's cached `_config`
  is reset so the next read sees the new state immediately.
- **Hard refusal** on `my_aliases` targets — identity is operator territory.
- **Default-allow** (`allow_alias_edits: true`), opposite of `send_message`'s
  default-deny, because the worst case here is a `git checkout`-able config
  edit, not a real message to a real person.
- **Soft caps** (`max_entries=500`, `max_aliases_per_entry=50`,
  `max_list_fields=50`) stop a chatty session from bloating the file.
- **Touchpoints:** `cli/alias_writer.py`, `mcp_server/server.py` (3 new
  tools + handlers + redaction), `config.py::get_alias_edit_settings`.

---

## Email (IMAP) connector

Mail accounts join the ingest pipeline as folder-per-channel sources.

- **Why:** email was the last major source not yet wired in. Modeling needed
  care — email doesn't fit the chat-channel abstraction cleanly.
- **Three orthogonal layers:** folder = channel (default, configurable);
  thread = first-class via `Message-ID` / `In-Reply-To` / `References` (so
  `get_thread` just works for email); recipients = message-level metadata
  (parsed `raw.to` / `raw.cc`) so internal/external classification
  generalizes to "internal only when sender AND every recipient is internal".
- **`send_message` = save a draft, not real send.** IMAP is read-only; SMTP
  is a separate set of credentials. Instead, the connector builds a proper
  RFC 5322 reply (reply-all minus self, `Re:` subject, `In-Reply-To`,
  `References`) and APPENDs to the configured Drafts folder via IMAP. User
  reviews + clicks Send in their mail client. Human-in-the-loop by default.
- **Attachment caching** is content-addressed (`sha1(payload)[:24]`), with
  text-typed attachments inlining the first 5KB.
- **Subject + plain body** form the searchable text; attachment markers are
  hoisted ABOVE the body so they survive the 200-char preview truncation.
- **`fetch_new` raises `NotImplementedError`** — IMAP polling for live reads
  is heavier than chat-API calls; the MCP handler punts cleanly with
  "next scheduled ingest will pick this up".
- **Library:** `imap_tools` (much friendlier than stdlib `imaplib`).
- **Touchpoints:** `connectors/email_connector.py`, `pipeline/ingest.py`
  (recipient-aware classification), `pipeline/filter_engine.py`
  (`skip_folders` / `skip_subjects` / address-aware `skip_senders`).

---

## Parallelize per-source fetch in ingest

Sources are fetched concurrently via a `ThreadPoolExecutor` — wall-clock
collapses from sum-of-sources to max-of-sources.

- **Why:** ingest was iterating sources serially; each `fetch_messages` is
  dominated by network roundtrips. With 3+ sources, total wall-clock could
  easily exceed 90 seconds while the CPU sat idle.
- **Threads, not asyncio:** all four connectors are I/O-bound (`requests` /
  `imap_tools`); the GIL releases on blocking I/O so threads get linear
  speed-up. Swapping to async would mean rewriting three connectors and
  finding an async IMAP library — large diff, small payoff.
- **Writes stay sequential.** Enrichment + insert + sender-cache + audit row
  run single-threaded. The embedding model isn't multi-thread-friendly, and
  SQLite writes serialize at the file lock anyway. Concurrency where it
  pays, sequential where it's correct.
- **Per-source error isolation preserved** — a connector raising lands in
  `source_errors` with the usual `{source, error}` shape; other sources'
  results still aggregate.
- **`fetch_workers=1` takes a true sequential path** (no executor) — keeps
  debugging clean and matches legacy behavior.
- **Default workers = min(num_sources, max_fetch_workers=8)**. The cap stops
  a 30-source config from opening 30 concurrent HTTP fan-outs.
- **Fetch summary log line** sorts sources slowest-first so the dominating
  source is at the head — that's the one to look at when a run feels slow.
- **Touchpoints:** `pipeline/ingest.py` (`_fetch_one` / `_fetch_all` /
  `_resolve_worker_count` / `_log_fetch_summary`), `config.py::get_ingest_settings`.

---

## Domain-based internal/external classification

A top-level `internal_domains:` list promotes any sender/recipient whose
email domain matches to "internal" by default.

- **Why:** email needs this most — without it, classifying recipients would
  mean adding every colleague to `user_aliases` by hand. With domain rules,
  `user_aliases` is reserved for the exceptions (contractors on `@gmail`,
  embedded partners on the client side).
- **Layered precedence** — broadest → most specific (later wins):
  1. Source `internal:` flag
  2. `internal_domains:` match
  3. Per-alias `internal: true|false` (can promote OR demote)
  4. `my_aliases` (current user is always internal)
- **`AliasResolver.is_internal(sender, email, source_internal)`** implements
  this; ingest + MCP live path look up `senders.email` and pass it in.
- **Exact-domain match only** in v1 (no `*.subdomain` wildcards) — explicit
  is better than surprise.
- **Touchpoints:** `pipeline/alias_resolver.py` (refactored to layered
  verdict), `pipeline/ingest.py` + `mcp_server/server.py::_get_new_messages`
  (email lookup), `config.py::get_internal_domains`.

---

## Mention graph (`mentions` table + `who_mentioned` MCP tool)

Every `@username` / Pachca `<@id>` in a message body is extracted at ingest
into a `mentions` table. `who_mentioned(target)` answers "who pinged whom"
with alias resolution; `target="me"` is the current user.

- **Why:** messages were isolated entries; the system couldn't answer
  "who's been pinging me this week" without a SQL query.
- **Source-agnostic extraction** in `pipeline/ingest.extract_mentions` —
  handles `@username` (dots/underscores allowed), Pachca `<@user_id>`,
  auto-converted `@nickname`; skips `@here/@channel/@all/@everyone` and
  email addresses (via lookbehind).
- **Three identifying columns** stored per row: raw token (`@john.doe`),
  alias-resolved canonical, best-effort `mentioned_sender_id` (looked up
  against `senders` by `source` + `username`).
- **Sender cache must precede mention insertion** (was a bug — the original
  ordering left every `mentioned_sender_id` NULL). Now `pipeline/ingest`
  caches senders BEFORE the message+mention insert loop.
- **`who_mentioned target="me"` widens the query** across all three
  identifying columns — matches even when only `mentioned_token` is set,
  which happens on the first ingest before senders are cached.
- **Touchpoints:** `storage/db.py` (`mentions` table +
  `get_mentions_for_identity`), `pipeline/ingest.py` (`extract_mentions` +
  `_insert_mention_rows`), `mcp_server/server.py` (`_who_mentioned` handler).

---

## Configurable embedding model

The embedding model and tuning are now config-driven; defaults preserve the
original BGE-M3 behavior exactly.

- **Why:** model + flags were hardcoded. A multilingual user paying for
  ~4GB on disk + ~2-2.5GB RAM couldn't swap in a 130MB English-only model
  for laptops; a GPU user couldn't bump `batch_size`.
- **Dimensionality guard:** Chroma stores vectors at a fixed dimension per
  collection. Switching to a different-dim model silently breaks similarity
  unless every doc is re-embedded. New `_check_dim()` peeks at one row on
  first insert and raises a clear error pointing at the swap recipe.
- **Numpy-array safety:** chroma's `peek()` returns the embeddings column as
  a `numpy.ndarray`. The original `if not embs:` triggered numpy's "ambiguous
  truth value" guard — replaced with explicit `is None` + `len(...) == 0`.
- **Migration recipe in README:** rename `collection_name` to keep old
  vectors around, OR delete `data/chroma/` and re-ingest.
- **Touchpoints:** `storage/vector_store.py`, `pipeline/ingest.py` +
  `mcp_server/server.py` (thread `config["embedding"]` through).

---

## CLI namespace (`cli/`) + `memorandum aliases refresh`

New `cli/` package hosts the user-facing verbs. `aliases refresh` is an
append-only stub generator for senders not yet covered by `user_aliases`.

- **Why:** `pipeline/` had grown into both the ingest engine AND a grab-bag
  of CLI utilities. Split: `pipeline/` is the engine (runs under systemd),
  `cli/` is what humans type.
- **Append-only contract:** existing alias entries are sacred — never
  edited, reordered, or removed. `--in-place` uses `ruamel.yaml` round-trip
  to preserve comments / key order / quoting.
- **Per-source breakdown** in the comment annotation: "seen 460 times in
  work_telegram" or "seen 460 times (work_telegram: 300, mattermost: 160)".
- **`bin/memorandum`** wrapper resolves the venv and dispatches.
- **Touchpoints:** `cli/__main__.py`, `cli/aliases.py`, `cli/health.py`,
  `cli/alias_writer.py`, `bin/memorandum`.

---

## Role / team / relations metadata on `user_aliases`

Each `user_aliases` entry can carry optional `role`, `team`, `reports_to`,
and `responsible_for` — surfaced via `get_user_aliases` so the agent grounds
who-is-who context early in a session.

- **Why:** alias entries used to be just "this is the same person under
  these names". The agent had no way to know "Jane is Backend lead on
  Platform team and owns the `dev-pl-*` channels".
- **`responsible_for`** lists channels / project prefixes / issue prefixes
  the person owns — used by the agent to route questions.
- **`reports_to` is free-form text**, not a foreign key. Validating against
  existing canonicals would refuse legitimate forward references (agent
  learns about Alex first, then his manager Jane later).
- **`get_user_aliases` output** renders role/team inline, reports_to /
  responsible_for on follow-on lines. `[internal]` marks staff.
- **Touchpoints:** `config.example.yaml` (schema), `pipeline/alias_resolver.py`
  (stores `_meta`), `mcp_server/server.py::_build_aliases_text`.

---

## YouTrack issue-id parsing

Issue ids like `PL-15491` are parsed from message URLs AND channel names;
`find_by_issue` MCP tool returns everything referencing a given id.

- **Why:** "what was the discussion about PROJ-1248" was a manual grep —
  even though the data was right there in URLs.
- **Shared regex helpers** (`build_youtrack_issue_regex`, `extract_urls`,
  `parse_channel_issue_ids`) live in `pipeline/ingest.py`; connectors
  lazy-import to avoid circular dependency.
- **Config-driven:** `youtrack.base_url` identifies the host;
  `youtrack.project_prefixes` enumerates known prefixes. Omit the section
  to disable detection — URLs are still extracted as generic `"other"`.
- **Channel-name issue ids** are stored in `channels.extra.issue_ids` at
  ingest, so `find_by_issue` finds messages from channels named for the
  issue even when no message body links it.
- **Touchpoints:** `pipeline/ingest.py` (regex helpers), `storage/db.py::find_by_issue_id`,
  `mcp_server/server.py::_find_by_issue`.

---

## Channel descriptions

Mattermost `purpose` / `header` and Telegram chat `description` captured at
ingest, surfaced in `list_channels` / `summarize_channel` / `summarize_messages`.

- **Why:** the agent had no way to know what `#dev-pl-mobile` was for. Now
  every list/summary includes the channel's purpose so context is grounded
  from the first tool call.
- **`channels.description` column** holds the combined string.
- **Touchpoints:** all four connectors (capture at normalize-time),
  `storage/db.py` (column + migration), `mcp_server/server.py` (formatters).

---

## Live channel reads (`get_new_messages` MCP tool)

Fetches messages newer than the DB straight from the source — the read half
of the read→act loop.

- **Why:** the agent could only see what the last scheduled ingest had
  captured (up to 15 minutes stale). For a real-time reply workflow it needs
  the up-to-the-second tail.
- **Read-only connector** built on demand (`db=None`, `db_callback` =
  read-only state lookup) so the live fetch doesn't accidentally advance
  cursors and starve the scheduled ingest.
- **Telegram** uses a single read-only `getUpdates` at the saved offset
  WITHOUT advancing it — the scheduler still picks up + stores those
  updates. Can raise a "busy" message on a 409 from a concurrent ingest.
- **De-dup against the DB** before returning — drops anything already
  stored.
- **Live internal/external classification** in the handler (same rule as
  ingest) so live messages get `[external]` tagged too.
- **IMAP punts** with a clear "live fetch not supported for IMAP" message —
  polling is too heavy for a read-before-send guard.
- **Touchpoints:** `connectors/*` (`fetch_new` on each),
  `mcp_server/server.py` (`_build_live_connector` + `_get_new_messages`).

---

## Health / status reporting

`memorandum health` CLI + `get_health` MCP tool — last ingest run status,
per-source oldest/last message freshness, recorded errors.

- **Why:** ingest runs once every 15 minutes; without an at-a-glance view,
  noticing "Pachca's been silent for 6 hours" required SQL.
- **Shared module** `pipeline/health.build_health_report()` powers both the
  CLI and the MCP tool — same output, same logic.
- **`ingest_runs` audit table** added: every run records `status`
  (`ok`/`partial`/`error`), counts, errors as JSON.
- **Exit codes** for the CLI: 0 (ok), 1 (partial/error), 2 (no run yet) —
  scriptable as `./bin/memorandum health && echo healthy`.
- **Touchpoints:** `pipeline/health.py`, `storage/db.py::record_ingest_run`,
  `cli/health.py`, `mcp_server/server.py::_get_health`.

---

## Test coverage

Comprehensive pytest suite across config / DB / filter engine / all
connectors / MCP server handlers / ingest orchestrator.

- **Why:** the codebase was past the size where ad-hoc testing missed
  regressions. Especially the cross-source enrichment logic.
- **`storage.vector_store` globally mocked** in `conftest.py` so BGE-M3
  never loads in CI. Saves ~2GB of RAM per run.
- **HTTP layer mocked** with `responses` (REST connectors) or `MagicMock`
  (imap_tools). No live network in CI.
- **Real SQLite** (in-memory or tmp_path) — tests run against the real
  `Database` class, not a stub.
- **Touchpoints:** entire `tests/` folder.

---

## Internal vs external sender marker

`messages.internal` column + automatic tagging of external senders as
`[external]` in MCP output.

- **Why:** mixed-tenant chats (Pachca/Telegram where colleagues + clients
  coexist) made every quote-from-a-discussion ambiguous without manually
  tracking who's who.
- **Per-source `internal: true` flag** for sources that are all-staff (e.g.
  company Mattermost).
- **Per-alias `internal: true|false`** for explicit per-person overrides.
- **Later extended** by domain rules (see "Domain-based internal/external"
  above) for email.
- **Touchpoints:** `pipeline/alias_resolver.py::is_internal`,
  `pipeline/ingest.py` (enrichment), `mcp_server/server.py::_ext_marker`.

---

## Thread reconstruction (`get_thread`)

Reply messages carry a `🧵 thread:{id}` marker in search results; pass it
to `get_thread` to pull the full conversation (root + all replies).

- **Why:** Mattermost / Pachca / Telegram threads spread across many rows;
  search would return individual replies with no obvious way to see the
  parent context.
- **Generic across sources** — uses `messages.thread_id` (set at ingest
  from each source's native concept of "thread root"). Email's
  Message-ID / References threading rides the same path.
- **Optional channel filter** lets the operator narrow to a specific room
  when thread_ids collide (rare but possible across sources).
- **Touchpoints:** `storage/db.py::get_thread`, `mcp_server/server.py::_get_thread`.

---

## Pachca connector

REST API (`api.pachca.com/api/shared/v1`) with Personal Access Token. Per-chat
incremental sync via newest-message-id stored as `last_update_at`.

- **Why:** the third major chat platform the user wanted to aggregate.
- **Token verification** via `GET /profile` (Pachca doesn't have a
  `/users/me` endpoint).
- **Personal (1-on-1) chats** have no `name` field — display name is built
  from the partner's name (resolved from `member_ids` minus current user),
  and `channels.name` falls back to the chat id so it's never empty.
- **Attachments downloaded at ingest** because Pachca's signed URLs expire.
  Cached by sha1 hash; retrievable via `get_attached_file`.
- **`thread_id` ← `thread.id`**, `reply_to_id` ← `parent_message_id`.
- **Touchpoints:** `connectors/pachca_connector.py`.

---

## Telegram connector via Bot API

Bot API (`getUpdates`). Collects group / channel / business messages.
Global update-offset sync. Skips direct messages to the bot itself.

- **Why:** Telegram is the second-most-used chat platform among the target
  users. Bot API is the right interface (vs the user-account API, which
  would require account credentials and is policy-fraught).
- **Skip bot DMs** (`chat.type == "private"` + `message` type) — those are
  noise from the operator testing the bot, not real conversation.
- **Business / secretary chats** are supported: the bot's
  `business_connection_id` is captured at ingest and reused on reply.
- **File attachments** inline as `[photo, file_id=...]` /
  `[document: name, file_id=...]` for on-demand retrieval via
  `get_attached_file` — photos cached at ingest, others on first access.
- **Sender full names** (first + last) cached during normalization since
  Telegram's username field is often empty.
- **Permalinks** only resolvable for channel / supergroup chats —
  `https://t.me/c/{chat_id}/{message_id}`. Private chats have no usable URL.
- **Touchpoints:** `connectors/telegram_connector.py`.

---

## User name aliases

Canonical identity mapping across sources via `user_aliases:` — unifies the
same person appearing under different names ("jane" / "Jane Smith" / "jane.smith").

- **Why:** Mattermost handle ≠ Telegram handle ≠ Pachca display name. Search
  for "Jane" would miss her Mattermost messages tagged "jsmith".
- **`AliasResolver`** loads from config; provides `resolve()` (alias →
  canonical) and `mentions_me()` (text contains any my_aliases entry).
- **`my_aliases`** is a separate concept — the operator's "I am this person"
  declaration. Used for the `mentions_me` flag on messages and the
  `mentions_me=true` filter on `search_messages`.
- **`sender_aliases` table** stores the flattened mapping, rewritten from
  config on each ingest run via `db.upsert_aliases`.
- **Touchpoints:** `pipeline/alias_resolver.py`, `storage/db.py`,
  `pipeline/ingest.py` (canonical_sender enrichment).

---

## `send_message` MCP tool (all sources)

Single MCP tool that sends a text message to Mattermost / Telegram / Pachca
(email gets draft semantics — see Email above).

- **Why:** the agent could only READ — the read→act loop was missing its
  act half.
- **Default-deny** via per-source `allow_send: true` flag. Sending is
  visible to other people; opt-in protects against fat-finger automation.
- **Read-before-send guard:** the tool description tells the agent to call
  `get_new_messages` for the channel immediately before sending — if any
  new messages appeared, cancel and reconsider. Anti-stale guard.
- **Telegram business chats** use the stored `business_connection_id`
  (required by Telegram for replying in a private chat).
- **Mattermost 429 rate limit** (~5 posts / 30s) surfaces a clear error,
  not a silent failure.
- **`sent_messages` audit table** logs every attempt (success + failure)
  with the text — invaluable for debugging "did that ever actually send?".
- **Touchpoints:** all four connectors (`send_message` method),
  `mcp_server/server.py::_send_message`,
  `storage/db.py::record_sent_message`.

---

## Normalize `channels` table

Channels became a first-class table with `id` + `source` + `display_name` +
`name` + per-source `extra` (JSON) instead of an implicit `messages.channel_id`.

- **Why:** without a real channels table, every `list_channels` call did a
  `SELECT DISTINCT` on the messages table — slow and missed empty channels.
  Also couldn't store channel-level metadata like incremental-sync state.
- **`extra` JSON column** holds per-source state: Telegram's `__offset__`
  sentinel, IMAP's `{uidvalidity, last_uid}`, business chats'
  `business_connection_id`.
- **`channels.last_update_at`** is the primary incremental-sync cursor.
- **Touchpoints:** `storage/db.py` (schema + `upsert_channel`),
  every connector (`upsert_channel` calls during fetch).

---

## Multi-source config

Multiple instances of the same source type are supported — `sources:` is a
dict of named entries, each with its own type / credentials / filters.

- **Why:** the operator wanted to connect to two separate Mattermost servers
  (work + client). The original config schema assumed one of each type.
- **`messages.source` stores the logical NAME** (e.g. `company_mattermost`),
  not the type. Two `mattermost` sources can't collide.
- **Per-source `filters:`** dict (skip_senders / skip_channels /
  only_channels / skip_patterns) replaces the previous global filter list.
- **Per-source `enabled: false`** to disable without removing.
- **Touchpoints:** `config.py` (schema), `pipeline/ingest.py` (iterates
  enabled sources), every connector (takes `source_name` parameter).

---

## Add `message_url` to MCP output

Every search result / digest entry includes a permalink back to the
original message.

- **Why:** the agent could quote a message but not link the operator to
  the source. Click-through to context was a manual search.
- **`make_message_url(msg, config)`** dispatches by source type. Each
  connector populates the URL components in `raw` at ingest
  (`team_name` + `post_id` for Mattermost, `chat_id` + `message_id` for
  Telegram/Pachca, etc.).
- **Telegram private chats** intentionally have no URL — Bot API doesn't
  expose one. `make_message_url` returns `None`; formatters skip.
- **Touchpoints:** `mcp_server/server.py::make_message_url`, all four
  connectors (URL-component fields in `raw`).

---

## Store URL-safe `channel_name`

Mattermost channels gained two name fields: `display_name` (friendly,
"Dev / Баги Триколора") and `name` (URL-safe slug, "dev-bagi-trikolora").

- **Why:** Mattermost has both fields natively; `display_name` is what
  humans recognize but `name` is what URLs use. Storing only one broke
  either display or linking.
- **`channels.name` column** stores the URL-safe form; `display_name`
  stays the human-readable one. `messages.channel` in search output uses
  `display_name` for readability.
- **Permalinks** built from `channels.name` (URL-safe).
- **Touchpoints:** `storage/db.py` (schema), `connectors/mattermost_connector.py`
  (normalize), `mcp_server/server.py::make_message_url`.

---

## Rejected ideas

### Markdown export for Obsidian
Considered but rejected. The premise was "export everything to Obsidian
vaults for personal knowledge management" — but the existing MCP tools
already let an LLM client browse the data interactively, which is more
flexible than a frozen markdown dump. A markdown export would also be
high-maintenance (per-source link rewriting, attachment handling, vault
structure choices) for a use case that didn't have a clear primary user.
If a real workflow surfaces it, file an issue.

### Per-channel retention policies
Considered while designing retention (`retention_days`). Rejected because
it invites the "wait, why is dev-pl still 2 years deep when general is 90
days" debugging session. One horizon everywhere is the right default.
Per-channel rules can ship later if a real workflow demands them.

### Soft-delete / tombstone scheme for retention
Considered, rejected. Recovery is "restore from backup" — operator
responsibility. Soft-delete in a personal aggregator is bloat.

### Process-based parallel ingest
Considered while designing parallel fetch. Rejected because each worker
process would re-load BGE-M3 (~2GB RAM per process). Threads inside one
process get the I/O concurrency for free.

### Backend-agnostic vector store
Considered while making the embedding model configurable. Rejected for v1:
the FlagEmbedding-compatible model swap covers the immediate "I want a
smaller / faster model" use case. Pluggable backends (sentence-transformers,
OpenAI embeddings) would change the load path AND likely require a
requirements split.
