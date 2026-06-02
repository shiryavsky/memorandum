"""SQLite database for message metadata storage."""
import functools
import re
import sqlite3
import json
import threading
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id               TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    channel_id       TEXT,
    sender           TEXT,
    canonical_sender TEXT,
    sender_id        TEXT,
    timestamp        TEXT,
    text             TEXT,
    thread_id        TEXT,
    reply_to_id      TEXT,
    mentions_me      INTEGER DEFAULT 0,
    internal         INTEGER DEFAULT 0,   -- 1 = company/internal sender, 0 = external
    tags             TEXT,   -- JSON array
    raw              TEXT    -- JSON object
);
CREATE INDEX IF NOT EXISTS idx_timestamp       ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_source          ON messages(source);
CREATE INDEX IF NOT EXISTS idx_channel_id      ON messages(channel_id);
CREATE INDEX IF NOT EXISTS idx_sender_id       ON messages(sender_id);
CREATE INDEX IF NOT EXISTS idx_mentions_me     ON messages(mentions_me);

CREATE TABLE IF NOT EXISTS sender_aliases (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL,
    alias          TEXT NOT NULL,
    sender_id      TEXT,           -- optional: links to senders.sender_id
    UNIQUE(alias)
);
CREATE INDEX IF NOT EXISTS idx_sender_alias ON sender_aliases(alias);

CREATE TABLE IF NOT EXISTS senders (
    sender_id   TEXT NOT NULL,
    source      TEXT NOT NULL,
    username    TEXT,
    full_name   TEXT,
    email       TEXT,
    phone       TEXT,
    avatar_url  TEXT,
    extra       TEXT,   -- JSON object for source-specific data
    updated_at  TEXT,
    PRIMARY KEY (source, sender_id)
);

CREATE TABLE IF NOT EXISTS channels (
    id            TEXT NOT NULL,
    source        TEXT NOT NULL,
    name          TEXT,          -- URL-safe channel name (e.g., "dev-bagi-trikolora")
    display_name  TEXT,          -- Friendly display name (e.g., "Dev / Баги Триколора")
    description   TEXT,          -- Human-written purpose/topic/header (Mattermost purpose+header, Telegram description)
    team_id       TEXT,
    team_name     TEXT,
    channel_type  TEXT,          -- e.g., "D" for DM, "O" for open, "P" for private
    extra         TEXT,          -- JSON object for source-specific metadata
    last_update_at INTEGER NOT NULL,
    updated_at    TEXT,
    PRIMARY KEY (source, id)
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL,   -- 'ok', 'partial', 'error'
    sources_checked  INTEGER DEFAULT 0,
    messages_new     INTEGER DEFAULT 0,
    messages_fetched INTEGER DEFAULT 0,
    errors           TEXT             -- JSON array of {source, error} dicts
);

CREATE TABLE IF NOT EXISTS sent_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at     TEXT NOT NULL,        -- UTC ISO timestamp of the send attempt
    source      TEXT NOT NULL,
    channel     TEXT NOT NULL,        -- channel id the message was sent to
    reply_to    TEXT,                 -- parent/root message id, if a threaded reply
    text        TEXT,
    success     INTEGER DEFAULT 0,    -- 1 = sent, 0 = failed
    message_id  TEXT,                 -- id returned by the source (NULL on failure)
    error       TEXT                  -- error string on failure
);
CREATE INDEX IF NOT EXISTS idx_sent_at      ON sent_messages(sent_at);
CREATE INDEX IF NOT EXISTS idx_sent_source  ON sent_messages(source);

CREATE TABLE IF NOT EXISTS mentions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id           TEXT NOT NULL,
    source               TEXT NOT NULL,
    sender_id            TEXT,
    sender_canonical     TEXT,
    mentioned_token      TEXT NOT NULL,
    mentioned_canonical  TEXT,
    mentioned_sender_id  TEXT,
    created_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_mentions_message   ON mentions(message_id);
CREATE INDEX IF NOT EXISTS idx_mentions_sender    ON mentions(sender_id);
CREATE INDEX IF NOT EXISTS idx_mentions_mentioned ON mentions(mentioned_sender_id);
CREATE INDEX IF NOT EXISTS idx_mentions_canonical ON mentions(mentioned_canonical);

CREATE TABLE IF NOT EXISTS prune_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    cutoff_ts         TEXT,
    messages_deleted  INTEGER DEFAULT 0,
    mentions_deleted  INTEGER DEFAULT 0,
    vectors_deleted   INTEGER DEFAULT 0,
    files_deleted     INTEGER DEFAULT 0,
    sent_deleted      INTEGER DEFAULT 0,
    runs_deleted      INTEGER DEFAULT 0,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS idx_prune_finished ON prune_runs(finished_at);

CREATE TABLE IF NOT EXISTS tool_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at     TEXT NOT NULL,
    tool_name     TEXT NOT NULL,
    args_summary  TEXT,           -- short, redacted JSON form of args
    duration_ms   INTEGER,
    success       INTEGER DEFAULT 1,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_called_at ON tool_calls(called_at);
CREATE INDEX IF NOT EXISTS idx_tool_calls_name      ON tool_calls(tool_name);
"""


def _synchronized(fn):
    """Hold ``self._lock`` for the whole call.

    The single ``sqlite3.Connection`` we keep on ``self.conn`` is shared by
    every Database method, but Python's ``sqlite3`` cursor/statement state is
    not thread-safe even with ``check_same_thread=False`` — concurrent use
    from the ingest's per-source ThreadPoolExecutor surfaces as
    ``SQLITE_MISUSE`` (errno 21, "bad parameter or other API misuse"). Wrap
    every method that touches ``self.conn`` with this decorator. ``_lock`` is
    a reentrant lock so methods that call each other (e.g. ``log_tool_call``
    → ``_prune_tool_calls``, ``get_or_fetch_sender`` → ``get_sender`` /
    ``upsert_sender``) don't deadlock.
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


class Database:
    """SQLite database for storing message metadata."""

    def __init__(self, path: str, read_only: bool = False):
        """Open the database.

        `read_only=True` opens via the SQLite URI mode=ro so accidental writes
        from a read-only consumer (the dashboard) raise instead of corrupting
        the file. We still ensure SCHEMA is current via a brief writable open
        beforehand — older DBs predating a column / table addition (e.g. the
        `tool_calls` table from TASK-026) wouldn't otherwise have it, and the
        read-only consumer can't add it.
        """
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.read_only = read_only
        # Re-entrant lock: every public method that touches `self.conn` takes
        # it via @_synchronized. See the decorator's docstring for the why.
        self._lock = threading.RLock()
        if read_only:
            # One-shot writable connect: apply schema + migrations, then close
            # and reopen the real handle as read-only.
            if Path(path).exists():
                bootstrap = sqlite3.connect(path)
                try:
                    bootstrap.executescript(SCHEMA)
                    bootstrap.commit()
                finally:
                    bootstrap.close()
            self.conn = sqlite3.connect(
                f"file:{path}?mode=ro", uri=True, check_same_thread=False
            )
        else:
            self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if not read_only:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
            self._migrate()
        # Counter for amortized tool_calls pruning — see log_tool_call.
        self._tool_calls_since_prune = 0

    @_synchronized
    def _migrate(self) -> None:
        """Add columns to existing tables that predate the current schema."""
        for col, ddl in [
            ("canonical_sender", "TEXT"),
            ("mentions_me", "INTEGER DEFAULT 0"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {ddl}")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
        for col, ddl in [
            ("description", "TEXT"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE channels ADD COLUMN {col} {ddl}")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

    @_synchronized
    def exists(self, msg_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM messages WHERE id=?", (msg_id,)
        ).fetchone()
        return row is not None

    @_synchronized
    def insert(self, msg: dict) -> bool:
        """Insert a new message. Returns True if inserted, False if duplicate."""
        cursor = self.conn.execute("""
            INSERT OR IGNORE INTO messages
            (id, source, channel_id, sender, canonical_sender, sender_id, timestamp,
             text, thread_id, reply_to_id, mentions_me, internal, tags, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            msg["id"],
            msg["source"],
            msg.get("channel_id"),
            msg.get("sender"),
            msg.get("canonical_sender"),
            msg.get("sender_id"),
            msg.get("timestamp"),
            msg.get("text"),
            msg.get("thread_id"),
            msg.get("reply_to_id"),
            1 if msg.get("mentions_me") else 0,
            1 if msg.get("internal") else 0,
            json.dumps(msg.get("tags", [])),
            json.dumps(msg.get("raw", {}))
        ))
        self.conn.commit()
        return cursor.rowcount > 0

    @_synchronized
    def resolve_channel(
        self, channel: str, source: Optional[str] = None
    ) -> Optional[dict]:
        """Resolve a channel id, name, or display_name to the channel row.

        When `source` is omitted, name/id collisions across sources silently
        pick the first match — callers that have a source hint should pass it.
        """
        clauses = ["(c.display_name = ? OR c.name = ? OR c.id = ?)"]
        params: list = [channel, channel, channel]
        if source:
            clauses.append("c.source = ?")
            params.append(source)
        row = self.conn.execute(
            "SELECT c.* FROM channels c WHERE " + " AND ".join(clauses) + " LIMIT 1",
            params,
        ).fetchone()
        return dict(row) if row else None

    @_synchronized
    def search(
        self,
        query: Optional[str] = None,
        source: Optional[str] = None,
        channel: Optional[str] = None,
        sender: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        mentions_me: bool = False,
        limit: int = 50
    ) -> list[dict]:
        """Search messages. channel filter matches channels.display_name or channels.name.
        sender filter matches both original sender and canonical_sender.
        """
        clauses = []
        params = []

        if query:
            clauses.append("m.text LIKE ?")
            params.append(f"%{query}%")
        if source:
            clauses.append("m.source = ?")
            params.append(source)
        if channel:
            clauses.append("(c.display_name = ? OR c.name = ? OR c.id = ?)")
            params.extend([channel, channel, channel])
        if sender:
            clauses.append("(m.sender = ? OR m.canonical_sender = ?)")
            params.extend([sender, sender])
        if since:
            clauses.append("m.timestamp >= ?")
            params.append(since)
        if until:
            clauses.append("m.timestamp <= ?")
            params.append(until)
        if mentions_me:
            clauses.append("m.mentions_me = 1")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        rows = self.conn.execute(f"""
            SELECT
                m.id, m.source, m.channel_id,
                COALESCE(c.display_name, c.name, m.channel_id) AS channel,
                c.name AS channel_name,
                m.sender, m.canonical_sender, m.sender_id, m.timestamp,
                m.text, m.thread_id, m.reply_to_id, m.mentions_me, m.internal, m.tags, m.raw
            FROM messages m
            LEFT JOIN channels c ON c.source = m.source AND c.id = m.channel_id
            {where}
            ORDER BY m.timestamp DESC LIMIT ?
        """, params + [limit]).fetchall()

        return [dict(row) for row in rows]

    @_synchronized
    def get_thread(self, thread_id: str, channel: Optional[str] = None,
                   limit: int = 50) -> list[dict]:
        """Return all messages in a thread (root + replies), ordered by timestamp.

        thread_id is the root post id. Matches replies whose thread_id equals it,
        plus the root message itself (id is '{source}:{thread_id}'). Optional channel
        filter matches channels.display_name, channels.name, or messages.channel_id.
        """
        clauses = ["(m.thread_id = ? OR m.id = ? OR m.id LIKE ?)"]
        params = [thread_id, thread_id, f"%:{thread_id}"]
        if channel:
            clauses.append("(c.display_name = ? OR c.name = ? OR m.channel_id = ?)")
            params.extend([channel, channel, channel])
        where = "WHERE " + " AND ".join(clauses)
        rows = self.conn.execute(f"""
            SELECT
                m.id, m.source, m.channel_id,
                COALESCE(c.display_name, c.name, m.channel_id) AS channel,
                c.name AS channel_name,
                m.sender, m.canonical_sender, m.sender_id, m.timestamp,
                m.text, m.thread_id, m.reply_to_id, m.mentions_me, m.internal, m.tags, m.raw
            FROM messages m
            LEFT JOIN channels c ON c.source = m.source AND c.id = m.channel_id
            {where}
            ORDER BY m.timestamp ASC LIMIT ?
        """, params + [limit]).fetchall()
        return [dict(row) for row in rows]

    @_synchronized
    def find_by_issue_id(self, issue_id: str, limit: int = 100) -> list[dict]:
        """Return messages referencing an issue id — either directly via ``raw.urls`` or
        indirectly via the channel name (``channels.extra.issue_ids``).

        Uses SQLite ``LIKE`` against the JSON-serialized columns, which is good enough for
        issue ids (uppercase letters + dash + digits, very low false-match probability) and
        avoids depending on the JSON1 extension.
        """
        if not issue_id:
            return []
        like = f'%"{issue_id}"%'
        rows = self.conn.execute("""
            SELECT
                m.id, m.source, m.channel_id,
                COALESCE(c.display_name, c.name, m.channel_id) AS channel,
                c.name AS channel_name,
                m.sender, m.canonical_sender, m.sender_id, m.timestamp,
                m.text, m.thread_id, m.reply_to_id, m.mentions_me, m.internal, m.tags, m.raw
            FROM messages m
            LEFT JOIN channels c ON c.source = m.source AND c.id = m.channel_id
            WHERE m.raw LIKE ? OR c.extra LIKE ?
            ORDER BY m.timestamp DESC
            LIMIT ?
        """, (like, like, limit)).fetchall()
        return [dict(row) for row in rows]

    @_synchronized
    def get_by_ids(self, ids: list[str]) -> list[dict]:
        """Fetch full message rows (same columns as search()) for the given IDs."""
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(f"""
            SELECT
                m.id, m.source, m.channel_id,
                COALESCE(c.display_name, c.name, m.channel_id) AS channel,
                c.name AS channel_name,
                m.sender, m.canonical_sender, m.sender_id, m.timestamp,
                m.text, m.thread_id, m.reply_to_id, m.mentions_me, m.internal, m.tags, m.raw
            FROM messages m
            LEFT JOIN channels c ON c.source = m.source AND c.id = m.channel_id
            WHERE m.id IN ({placeholders})
        """, ids).fetchall()
        return [dict(row) for row in rows]

    @_synchronized
    def count(self, source: Optional[str] = None) -> int:
        if source:
            row = self.conn.execute(
                "SELECT COUNT(*) as count FROM messages WHERE source = ?", (source,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) as count FROM messages").fetchone()
        return row["count"] if row else 0

    # === Senders ===

    @_synchronized
    def get_sender(self, source: str, sender_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM senders WHERE source = ? AND sender_id = ?",
            (source, sender_id)
        ).fetchone()
        return dict(row) if row else None

    @_synchronized
    def upsert_sender(self, sender_info: dict) -> bool:
        from datetime import datetime, timezone
        self.conn.execute("""
            INSERT OR REPLACE INTO senders
            (sender_id, source, username, full_name, email, phone, avatar_url, extra, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            sender_info.get("sender_id"),
            sender_info.get("source"),
            sender_info.get("username"),
            sender_info.get("full_name"),
            sender_info.get("email"),
            sender_info.get("phone"),
            sender_info.get("avatar_url"),
            json.dumps(sender_info.get("extra", {})),
            datetime.now(timezone.utc).isoformat()
        ))
        self.conn.commit()
        return True

    @_synchronized
    def get_or_fetch_sender(self, source: str, sender_id: str,
                            fetch_callback: callable = None) -> dict:
        sender = self.get_sender(source, sender_id)
        if sender is None and fetch_callback:
            try:
                sender = fetch_callback(source, sender_id)
                if sender:
                    self.upsert_sender(sender)
            except Exception:
                pass
        if sender is None:
            sender = {
                "sender_id": sender_id,
                "source": source,
                "username": sender_id,
                "full_name": None,
                "email": None,
                "phone": None,
                "avatar_url": None,
                "extra": {},
                "updated_at": None
            }
        return sender

    @_synchronized
    def find_sender_id_by_username(self, source: str, username: str) -> Optional[str]:
        """Look up a sender_id by (source, username). Case-insensitive on username."""
        if not source or not username:
            return None
        row = self.conn.execute(
            "SELECT sender_id FROM senders WHERE source = ? AND LOWER(username) = LOWER(?) LIMIT 1",
            (source, username)
        ).fetchone()
        return row["sender_id"] if row else None

    @_synchronized
    def get_senders(self, source: Optional[str] = None, limit: int = 100) -> list[dict]:
        if source:
            rows = self.conn.execute(
                "SELECT * FROM senders WHERE source = ? ORDER BY updated_at DESC LIMIT ?",
                (source, limit)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM senders ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # === Aliases ===

    @_synchronized
    def upsert_aliases(self, user_aliases: list[dict]) -> None:
        """Replace all alias mappings with the provided list (synced from config)."""
        self.conn.execute("DELETE FROM sender_aliases")
        for group in user_aliases:
            canonical = group.get("canonical_name", "")
            for alias in group.get("aliases", []):
                self.conn.execute(
                    "INSERT OR REPLACE INTO sender_aliases (canonical_name, alias) VALUES (?, ?)",
                    (canonical, alias.lower())
                )
        self.conn.commit()

    @_synchronized
    def get_aliases(self) -> list[dict]:
        """Return all alias groups as [{canonical_name, aliases}] dicts."""
        rows = self.conn.execute(
            "SELECT canonical_name, alias FROM sender_aliases ORDER BY canonical_name, alias"
        ).fetchall()
        groups: dict[str, list[str]] = {}
        for row in rows:
            groups.setdefault(row["canonical_name"], []).append(row["alias"])
        return [{"canonical_name": c, "aliases": aliases} for c, aliases in groups.items()]

    # === Channels ===

    @_synchronized
    def get_channel(self, source: str, channel_id: str) -> Optional[int]:
        """Return last_update_at timestamp for the channel (used for incremental sync)."""
        row = self.conn.execute(
            "SELECT last_update_at FROM channels WHERE source = ? AND id = ?",
            (source, channel_id)
        ).fetchone()
        return row["last_update_at"] if row else None

    @_synchronized
    def get_channel_row(self, source: str, channel_id: str) -> Optional[dict]:
        """Return the full channel row as a dict with `extra` parsed from JSON, or None."""
        row = self.conn.execute(
            "SELECT * FROM channels WHERE source = ? AND id = ?",
            (source, channel_id)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        if result.get("extra"):
            try:
                result["extra"] = json.loads(result["extra"])
            except (json.JSONDecodeError, TypeError):
                result["extra"] = {}
        return result

    @_synchronized
    def upsert_channel(self, channel_info: dict) -> None:
        """Insert or update channel metadata and sync state."""
        from datetime import datetime, timezone
        self.conn.execute("""
            INSERT INTO channels
                (id, source, name, display_name, description, team_id, team_name,
                 channel_type, extra, last_update_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, id) DO UPDATE SET
                name           = COALESCE(excluded.name, name),
                display_name   = COALESCE(excluded.display_name, display_name),
                description    = COALESCE(excluded.description, description),
                team_id        = COALESCE(excluded.team_id, team_id),
                team_name      = COALESCE(excluded.team_name, team_name),
                channel_type   = COALESCE(excluded.channel_type, channel_type),
                extra          = COALESCE(excluded.extra, extra),
                last_update_at = excluded.last_update_at,
                updated_at     = excluded.updated_at
        """, (
            channel_info["id"],
            channel_info["source"],
            channel_info.get("name"),
            channel_info.get("display_name"),
            channel_info.get("description"),
            channel_info.get("team_id"),
            channel_info.get("team_name"),
            channel_info.get("channel_type"),
            json.dumps(channel_info["extra"]) if channel_info.get("extra") else None,
            channel_info["last_update_at"],
            datetime.now(timezone.utc).isoformat()
        ))
        self.conn.commit()

    # === Sent messages ===

    @_synchronized
    def record_sent_message(self, sent: dict) -> int:
        """Log an outbound message (success or failure) for audit/debugging. Returns row id."""
        from datetime import datetime, timezone
        cursor = self.conn.execute("""
            INSERT INTO sent_messages
                (sent_at, source, channel, reply_to, text, success, message_id, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            sent.get("sent_at") or datetime.now(timezone.utc).isoformat(),
            sent["source"],
            sent["channel"],
            sent.get("reply_to"),
            sent.get("text"),
            1 if sent.get("success") else 0,
            str(sent["message_id"]) if sent.get("message_id") is not None else None,
            sent.get("error"),
        ))
        self.conn.commit()
        return cursor.lastrowid

    # === Mentions ===

    @_synchronized
    def insert_mentions(self, rows: list[dict]) -> int:
        """Bulk-insert mention rows. Returns number of rows inserted."""
        if not rows:
            return 0
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        payload = [
            (
                r["message_id"],
                r["source"],
                r.get("sender_id"),
                r.get("sender_canonical"),
                r["mentioned_token"],
                r.get("mentioned_canonical"),
                r.get("mentioned_sender_id"),
                r.get("created_at") or now,
            )
            for r in rows
        ]
        self.conn.executemany("""
            INSERT INTO mentions
                (message_id, source, sender_id, sender_canonical,
                 mentioned_token, mentioned_canonical, mentioned_sender_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, payload)
        self.conn.commit()
        return len(payload)

    @_synchronized
    def get_mentions(
        self,
        mentioned_canonical: Optional[str] = None,
        mentioned_sender_id: Optional[str] = None,
        sender_canonical: Optional[str] = None,
        source: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return mention rows joined to their messages.

        Filters are ANDed; pass a value to narrow. At least one of `mentioned_canonical`,
        `mentioned_sender_id`, or `sender_canonical` should be set in practice — passing
        none returns the most recent N mentions across everyone.
        """
        clauses = []
        params: list = []
        if mentioned_canonical:
            clauses.append("mn.mentioned_canonical = ?")
            params.append(mentioned_canonical)
        if mentioned_sender_id:
            clauses.append("mn.mentioned_sender_id = ?")
            params.append(mentioned_sender_id)
        if sender_canonical:
            clauses.append("mn.sender_canonical = ?")
            params.append(sender_canonical)
        if source:
            clauses.append("mn.source = ?")
            params.append(source)
        if since:
            clauses.append("m.timestamp >= ?")
            params.append(since)
        if until:
            clauses.append("m.timestamp <= ?")
            params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(f"""
            SELECT
                mn.id AS mention_id, mn.mentioned_token, mn.mentioned_canonical,
                mn.mentioned_sender_id, mn.sender_canonical,
                m.id, m.source, m.channel_id,
                COALESCE(c.display_name, c.name, m.channel_id) AS channel,
                c.name AS channel_name,
                m.sender, m.canonical_sender, m.sender_id, m.timestamp,
                m.text, m.thread_id, m.reply_to_id, m.mentions_me, m.internal, m.tags, m.raw
            FROM mentions mn
            JOIN messages m ON m.id = mn.message_id
            LEFT JOIN channels c ON c.source = m.source AND c.id = m.channel_id
            {where}
            ORDER BY m.timestamp DESC
            LIMIT ?
        """, params + [limit]).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def get_mentions_for_identity(
        self,
        canonicals: Optional[list[str]] = None,
        tokens: Optional[list[str]] = None,
        sender_ids: Optional[list[str]] = None,
        sender_canonical: Optional[str] = None,
        source: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return mentions matching ANY of (canonicals, tokens, sender_ids).

        Built for "who mentioned this person" queries where the same identity may
        be stored under any of three columns: `mentioned_canonical` (alias-resolved),
        `mentioned_token` (raw `@handle` / `<@id>`), `mentioned_sender_id` (best-effort
        sender lookup). Each list is matched case-insensitively (tokens use LOWER()),
        ORed together; the other filters (`sender_canonical`/`source`/`since`/`until`)
        are ANDed.
        """
        identity_clauses: list[str] = []
        params: list = []
        if canonicals:
            placeholders = ",".join("?" * len(canonicals))
            identity_clauses.append(f"mn.mentioned_canonical IN ({placeholders})")
            params.extend(canonicals)
        if tokens:
            placeholders = ",".join("?" * len(tokens))
            identity_clauses.append(f"LOWER(mn.mentioned_token) IN ({placeholders})")
            params.extend(t.lower() for t in tokens)
        if sender_ids:
            placeholders = ",".join("?" * len(sender_ids))
            identity_clauses.append(f"mn.mentioned_sender_id IN ({placeholders})")
            params.extend(sender_ids)

        if not identity_clauses:
            return []

        clauses = ["(" + " OR ".join(identity_clauses) + ")"]
        if sender_canonical:
            clauses.append("mn.sender_canonical = ?")
            params.append(sender_canonical)
        if source:
            clauses.append("mn.source = ?")
            params.append(source)
        if since:
            clauses.append("m.timestamp >= ?")
            params.append(since)
        if until:
            clauses.append("m.timestamp <= ?")
            params.append(until)

        where = "WHERE " + " AND ".join(clauses)
        rows = self.conn.execute(f"""
            SELECT
                mn.id AS mention_id, mn.mentioned_token, mn.mentioned_canonical,
                mn.mentioned_sender_id, mn.sender_canonical,
                m.id, m.source, m.channel_id,
                COALESCE(c.display_name, c.name, m.channel_id) AS channel,
                c.name AS channel_name,
                m.sender, m.canonical_sender, m.sender_id, m.timestamp,
                m.text, m.thread_id, m.reply_to_id, m.mentions_me, m.internal, m.tags, m.raw
            FROM mentions mn
            JOIN messages m ON m.id = mn.message_id
            LEFT JOIN channels c ON c.source = m.source AND c.id = m.channel_id
            {where}
            ORDER BY m.timestamp DESC
            LIMIT ?
        """, params + [limit]).fetchall()
        return [dict(r) for r in rows]

    # === Ingest runs ===

    @_synchronized
    def record_ingest_run(self, run: dict) -> None:
        self.conn.execute("""
            INSERT INTO ingest_runs
                (started_at, finished_at, status, sources_checked, messages_new, messages_fetched, errors)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            run.get("started_at"),
            run.get("finished_at"),
            run.get("status", "ok"),
            run.get("sources_checked", 0),
            run.get("messages_new", 0),
            run.get("messages_fetched", 0),
            json.dumps(run.get("errors", [])),
        ))
        self.conn.commit()

    @_synchronized
    def get_last_ingest_run(self) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["errors"] = json.loads(result.get("errors") or "[]")
        return result

    @_synchronized
    def get_source_health(self) -> list[dict]:
        """Return per-source message stats: oldest/last timestamp and count."""
        rows = self.conn.execute("""
            SELECT source,
                   MIN(timestamp) AS oldest_message,
                   MAX(timestamp) AS last_message,
                   COUNT(*)       AS count
            FROM messages
            GROUP BY source
        """).fetchall()
        return [dict(r) for r in rows]

    # === Retention / pruning (TASK-028) ===
    #
    # The whole-row contract: ``prune(cutoff_iso)`` deletes everything WHERE
    # timestamp < cutoff_iso from messages + their mentions + sent_messages +
    # ingest_runs, in one transaction. It does NOT touch channels / senders /
    # sender_aliases (those carry incremental-sync state and historical
    # identity) and it does NOT touch the file cache / vector store (those are
    # caller-orchestrated — see pipeline/housekeeping.py). The vector + file
    # fan-out runs OUTSIDE this transaction so the SQL stays a single atomic
    # unit even when chroma or the filesystem misbehave.
    _FILE_ID_RE = re.compile(r"file_id=([A-Za-z0-9_\-]+)")

    @_synchronized
    def referenced_file_ids(self) -> set:
        """Scan messages.text for inline `file_id=<...>` markers and return the set.

        Tolerant on the id alphabet (letters / digits / underscore / hyphen) so
        it matches sha1-prefix ids (Pachca, Email), Mattermost post-style ids,
        AND Telegram base64-like Bot API ids. Used by the file-cache mark-and-
        sweep to decide what to keep.
        """
        out: set = set()
        for row in self.conn.execute(
            "SELECT text FROM messages WHERE text LIKE '%file_id=%'"
        ):
            out.update(self._FILE_ID_RE.findall(row["text"] or ""))
        return out

    @_synchronized
    def count_prune_candidates(self, cutoff_iso: str) -> dict:
        """Return what `prune(cutoff_iso)` WOULD delete without touching anything.

        Same dict shape as `prune()`; used by the dry-run path so the operator
        can review counts before committing.
        """
        msg_ids = [
            r["id"] for r in self.conn.execute(
                "SELECT id FROM messages WHERE timestamp < ?", (cutoff_iso,)
            ).fetchall()
        ]
        if msg_ids:
            placeholders = ",".join("?" * len(msg_ids))
            mn = self.conn.execute(
                f"SELECT COUNT(*) c FROM mentions WHERE message_id IN ({placeholders})",
                msg_ids,
            ).fetchone()["c"]
        else:
            mn = 0
        sent = self.conn.execute(
            "SELECT COUNT(*) c FROM sent_messages WHERE sent_at < ?", (cutoff_iso,)
        ).fetchone()["c"]
        runs = self.conn.execute(
            "SELECT COUNT(*) c FROM ingest_runs WHERE started_at < ?", (cutoff_iso,)
        ).fetchone()["c"]
        return {
            "message_ids": msg_ids,
            "messages_deleted": len(msg_ids),
            "mentions_deleted": mn,
            "sent_deleted": sent,
            "runs_deleted": runs,
        }

    @_synchronized
    def prune(self, cutoff_iso: str) -> dict:
        """Delete everything older than `cutoff_iso` in one transaction.

        Order matters: mentions first (no FK so we'd otherwise see orphan rows
        between the two deletes), then messages, then the secondary tables.
        Returns counts AND the deleted message id list (the caller needs the
        ids for the vector store fan-out).
        """
        # Collect ids BEFORE we delete — the caller needs them for chroma.
        msg_ids = [
            r["id"] for r in self.conn.execute(
                "SELECT id FROM messages WHERE timestamp < ?", (cutoff_iso,)
            ).fetchall()
        ]

        with self.conn:  # context manager = atomic transaction
            mentions_deleted = 0
            if msg_ids:
                # SQLite caps parameter count (default 999 on older builds);
                # chunk to be safe.
                for i in range(0, len(msg_ids), 500):
                    batch = msg_ids[i:i + 500]
                    placeholders = ",".join("?" * len(batch))
                    cur = self.conn.execute(
                        f"DELETE FROM mentions WHERE message_id IN ({placeholders})",
                        batch,
                    )
                    mentions_deleted += cur.rowcount
            cur = self.conn.execute(
                "DELETE FROM messages WHERE timestamp < ?", (cutoff_iso,)
            )
            messages_deleted = cur.rowcount
            cur = self.conn.execute(
                "DELETE FROM sent_messages WHERE sent_at < ?", (cutoff_iso,)
            )
            sent_deleted = cur.rowcount
            cur = self.conn.execute(
                "DELETE FROM ingest_runs WHERE started_at < ?", (cutoff_iso,)
            )
            runs_deleted = cur.rowcount
        return {
            "message_ids": msg_ids,
            "messages_deleted": messages_deleted,
            "mentions_deleted": mentions_deleted,
            "sent_deleted": sent_deleted,
            "runs_deleted": runs_deleted,
        }

    @_synchronized
    def last_prune_at(self) -> Optional[str]:
        """Most recent successful prune's finished_at, or None if never pruned."""
        row = self.conn.execute(
            "SELECT MAX(finished_at) AS fin FROM prune_runs WHERE error IS NULL"
        ).fetchone()
        return row["fin"] if row and row["fin"] else None

    @_synchronized
    def record_prune_run(self, payload: dict) -> int:
        """Append one row to prune_runs. Returns the new row id."""
        cur = self.conn.execute("""
            INSERT INTO prune_runs (
                started_at, finished_at, cutoff_ts,
                messages_deleted, mentions_deleted, vectors_deleted, files_deleted,
                sent_deleted, runs_deleted, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            payload.get("started_at"),
            payload.get("finished_at"),
            payload.get("cutoff_ts"),
            int(payload.get("messages_deleted", 0)),
            int(payload.get("mentions_deleted", 0)),
            int(payload.get("vectors_deleted", 0)),
            int(payload.get("files_deleted", 0)),
            int(payload.get("sent_deleted", 0)),
            int(payload.get("runs_deleted", 0)),
            payload.get("error"),
        ))
        self.conn.commit()
        return cur.lastrowid

    # === Channels ===

    @_synchronized
    def list_channels(self, source: Optional[str] = None) -> list[dict]:
        """Return known channels (id, source, name, display_name, channel_type) for discovery.

        Excludes the Telegram offset sentinel row (id='__offset__').
        """
        if source:
            rows = self.conn.execute(
                "SELECT id, source, name, display_name, description, channel_type FROM channels "
                "WHERE source = ? AND id != '__offset__' ORDER BY display_name",
                (source,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, source, name, display_name, description, channel_type FROM channels "
                "WHERE id != '__offset__' ORDER BY source, display_name"
            ).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def get_channels(self, source: Optional[str] = None) -> list[str]:
        """Return list of channel display names."""
        if source:
            rows = self.conn.execute(
                "SELECT COALESCE(display_name, name, id) AS name FROM channels"
                " WHERE source = ? ORDER BY name",
                (source,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT COALESCE(display_name, name, id) AS name FROM channels ORDER BY name"
            ).fetchall()
        return [r["name"] for r in rows]

    # === Tool calls log (TASK-026) ===

    # Amortized prune cadence — every Nth log_tool_call attempts to drop rows
    # older than 30 days. Keeps the table bounded without adding a write per
    # call; a missed prune (e.g. read-only DB) just delays cleanup by N rows.
    _TOOL_CALLS_PRUNE_EVERY = 100
    _TOOL_CALLS_KEEP_DAYS = 30

    @_synchronized
    def log_tool_call(
        self,
        tool_name: str,
        args_summary: Optional[str] = None,
        duration_ms: Optional[int] = None,
        success: bool = True,
        error: Optional[str] = None,
    ) -> None:
        """Append one tool_calls row. Best-effort — swallows failures to stderr
        so a logging hiccup never breaks an MCP tool call. No-op on a read-only
        connection."""
        if self.read_only:
            return
        import sys as _sys
        from datetime import datetime, timezone
        try:
            self.conn.execute("""
                INSERT INTO tool_calls
                    (called_at, tool_name, args_summary, duration_ms, success, error)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                tool_name,
                args_summary,
                int(duration_ms) if duration_ms is not None else None,
                1 if success else 0,
                error,
            ))
            self.conn.commit()
        except Exception as e:
            print(f"log_tool_call failed: {e}", file=_sys.stderr)
            return
        self._tool_calls_since_prune += 1
        if self._tool_calls_since_prune >= self._TOOL_CALLS_PRUNE_EVERY:
            self._tool_calls_since_prune = 0
            self._prune_tool_calls()

    @_synchronized
    def _prune_tool_calls(self) -> int:
        """Drop tool_calls rows older than _TOOL_CALLS_KEEP_DAYS. Best-effort."""
        import sys as _sys
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self._TOOL_CALLS_KEEP_DAYS)).isoformat()
        try:
            cur = self.conn.execute(
                "DELETE FROM tool_calls WHERE called_at < ?", (cutoff,)
            )
            self.conn.commit()
            return cur.rowcount
        except Exception as e:
            print(f"_prune_tool_calls failed: {e}", file=_sys.stderr)
            return 0

    # === Dashboard read queries (TASK-026) ===
    # All SQL lives here so pipeline/dashboard.py stays pure formatting.

    @_synchronized
    def latest_messages(self, limit: int = 15) -> list[dict]:
        """Most recent messages across all sources, newest first."""
        rows = self.conn.execute("""
            SELECT m.id, m.source, m.channel_id,
                   COALESCE(c.display_name, c.name, m.channel_id) AS channel,
                   m.sender, m.canonical_sender, m.timestamp,
                   m.text, m.mentions_me, m.internal
            FROM messages m
            LEFT JOIN channels c ON c.source = m.source AND c.id = m.channel_id
            ORDER BY m.timestamp DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def top_channels_since(self, since_iso: str, limit: int = 10) -> list[dict]:
        """Channels with the most messages since `since_iso`, sorted desc."""
        rows = self.conn.execute("""
            SELECT m.source, m.channel_id,
                   COALESCE(c.display_name, c.name, m.channel_id) AS channel,
                   COUNT(*) AS count
            FROM messages m
            LEFT JOIN channels c ON c.source = m.source AND c.id = m.channel_id
            WHERE m.timestamp >= ?
            GROUP BY m.source, m.channel_id
            ORDER BY count DESC
            LIMIT ?
        """, (since_iso, limit)).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def top_senders_since(self, since_iso: str, limit: int = 10) -> list[dict]:
        """Senders with the most messages since `since_iso`. Uses canonical_sender
        when available so aliases of the same person collapse into one row."""
        rows = self.conn.execute("""
            SELECT COALESCE(NULLIF(canonical_sender, ''), sender) AS sender,
                   source,
                   COUNT(*) AS count,
                   MAX(internal) AS internal
            FROM messages
            WHERE timestamp >= ?
            GROUP BY COALESCE(NULLIF(canonical_sender, ''), sender), source
            ORDER BY count DESC
            LIMIT ?
        """, (since_iso, limit)).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def messages_per_day(self, days: int = 30) -> list[dict]:
        """Daily counts for the trailing N days, oldest → newest.

        Returns rows of {date: 'YYYY-MM-DD', count: int}. Days with zero
        messages are absent — the caller fills the gaps. SQLite's substr() on
        the timestamp text avoids a date-parsing detour and works on the
        ISO-8601 strings the ingest writes.
        """
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.conn.execute("""
            SELECT SUBSTR(timestamp, 1, 10) AS date, COUNT(*) AS count
            FROM messages
            WHERE timestamp >= ?
            GROUP BY date
            ORDER BY date ASC
        """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def hour_of_day_histogram(self, days: int = 14, tz_name: str = "UTC") -> list[int]:
        """Average messages per hour-of-day across the trailing N days.

        Returns a list of 24 ints — index 0 = midnight, index 23 = 11pm — in
        the requested timezone. We compute in Python (over a single SELECT)
        rather than in SQL because IANA timezone conversion isn't portable
        across SQLite builds.
        """
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT timestamp FROM messages WHERE timestamp >= ?", (cutoff,)
        ).fetchall()
        buckets = [0] * 24
        for r in rows:
            ts = r["timestamp"] or ""
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                buckets[dt.astimezone(tz).hour] += 1
            except (ValueError, TypeError):
                continue
        return buckets

    @_synchronized
    def send_stats_since(self, since_iso: str) -> dict:
        """Counts for the sent_messages table since `since_iso`.

        Returns ``{total, success, failure, per_source: [{source, total, success, failure}, ...]}``.
        """
        total_row = self.conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failure
            FROM sent_messages WHERE sent_at >= ?
        """, (since_iso,)).fetchone()
        per_source = [dict(r) for r in self.conn.execute("""
            SELECT source,
                   COUNT(*) AS total,
                   SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failure
            FROM sent_messages WHERE sent_at >= ?
            GROUP BY source
            ORDER BY total DESC
        """, (since_iso,)).fetchall()]
        return {
            "total":   total_row["total"] or 0,
            "success": total_row["success"] or 0,
            "failure": total_row["failure"] or 0,
            "per_source": per_source,
        }

    @_synchronized
    def tool_call_stats_since(self, since_iso: str) -> list[dict]:
        """Per-tool aggregates since `since_iso`, sorted by total desc."""
        rows = self.conn.execute("""
            SELECT tool_name,
                   COUNT(*) AS total,
                   SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failure,
                   AVG(duration_ms) AS avg_ms
            FROM tool_calls
            WHERE called_at >= ?
            GROUP BY tool_name
            ORDER BY total DESC
        """, (since_iso,)).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def recent_mentions_me(self, limit: int = 3) -> list[dict]:
        """Most recent messages flagged mentions_me=1, newest first."""
        rows = self.conn.execute("""
            SELECT m.id, m.source, m.channel_id,
                   COALESCE(c.display_name, c.name, m.channel_id) AS channel,
                   m.sender, m.timestamp, m.text
            FROM messages m
            LEFT JOIN channels c ON c.source = m.source AND c.id = m.channel_id
            WHERE m.mentions_me = 1
            ORDER BY m.timestamp DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def mentions_me_counts(self) -> dict:
        """Mentions-me counts for the last 1h / 24h / 7d windows."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        windows = {
            "h1":  (now - timedelta(hours=1)).isoformat(),
            "d1":  (now - timedelta(days=1)).isoformat(),
            "d7":  (now - timedelta(days=7)).isoformat(),
        }
        out: dict = {}
        for key, cutoff in windows.items():
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE mentions_me = 1 AND timestamp >= ?",
                (cutoff,),
            ).fetchone()
            out[key] = row["c"] if row else 0
        return out

    @_synchronized
    def recent_ingest_runs(self, limit: int = 20) -> list[dict]:
        """Last N ingest_runs rows, newest first — used to compute cadence."""
        rows = self.conn.execute(
            "SELECT * FROM ingest_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            d["errors"] = json.loads(d.get("errors") or "[]")
            out.append(d)
        return out

    @_synchronized
    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
