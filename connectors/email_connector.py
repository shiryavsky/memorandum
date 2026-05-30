"""IMAP email connector — polls configured folders and emits normalized messages.

Same interface as the chat connectors (`connect`, `disconnect`, `fetch_messages`,
`fetch_new`, `get_sender_info`, `send_message`) so `pipeline.ingest` and the MCP
server can dispatch by source type without special-casing.

Key design choices (see the email-connector entry in CHANGELOG.md):
- Folder → channel (`channel_id = f"folder:{folder_path}"`).
- Thread = Message-ID / References (root walked back via References).
- `send_message` for email = IMAP APPEND to the Drafts folder, NOT SMTP send.
"""
import email.utils
import logging
import re
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Optional

from imap_tools import AND, MailBox, MailMessage, MailMessageFlags
from imap_tools.consts import MailBoxFolderStatusOptions

logger = logging.getLogger(__name__)


MAX_TEXT_PREVIEW_SIZE = 5 * 1024  # first 5KB inlined for text attachments
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_WS_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    """Cheap HTML → text. Doesn't pull beautifulsoup4 just for this — tags + ws."""
    if not html:
        return ""
    out = _HTML_TAG_RE.sub(" ", html)
    return _HTML_WS_RE.sub(" ", out).strip()


def _ensure_iter_addresses(values) -> list[tuple[str, str]]:
    """imap_tools returns EmailAddress namedtuples; map to (name, email) pairs."""
    out: list[tuple[str, str]] = []
    for v in values or ():
        name = getattr(v, "name", "") or ""
        addr = getattr(v, "email", "") or ""
        if addr:
            out.append((name, addr.lower()))
    return out


def _parse_references(raw: Optional[str]) -> list[str]:
    """Pull <id>-style refs out of a References / In-Reply-To header value."""
    if not raw:
        return []
    return re.findall(r"<[^<>\s]+>", raw)


class EmailConnector:
    """Connector for IMAP mail. Auth: username + password (OAuth2 deferred)."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        source_name: str = "email",
        port: int = 993,
        folders: Optional[list[str]] = None,
        skip_folders: Optional[list[str]] = None,
        channel_names: Optional[dict] = None,
        drafts_folder: str = "Drafts",
        from_address: Optional[str] = None,
        default_limit: int = 200,
        db_callback: Optional[Callable] = None,
        db=None,
        text_extensions: Optional[set] = None,
        file_cache_dir: str = "data/file_cache",
        youtrack_cfg: Optional[dict] = None,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.source_name = source_name
        self._folders = list(folders) if folders else ["INBOX"]
        self._skip_folders = set(skip_folders or [])
        self._channel_names = dict(channel_names or {})
        self._drafts_folder = drafts_folder
        # IMAP usernames are usually the email address itself; allow override.
        self.from_address = (from_address or username or "").lower()
        self._default_limit = default_limit
        self._db_callback = db_callback
        self._db = db
        self._text_extensions = text_extensions or set()
        self._file_cache_dir = file_cache_dir
        self._youtrack_cfg = youtrack_cfg or {}

        Path(file_cache_dir).mkdir(parents=True, exist_ok=True)
        self._mailbox: Optional[MailBox] = None
        # address (lowercased) → display name seen on a recent From header.
        # Populated during _normalize; consumed by get_sender_info so the
        # senders.full_name column carries something useful for email.
        self._sender_name_cache: dict[str, str] = {}

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        self._mailbox = MailBox(self.host, port=self.port).login(self.username, self.password)
        logger.info(f"[{self.source_name}] Connected to {self.host} as {self.username}")

    def disconnect(self) -> None:
        if self._mailbox is None:
            return
        try:
            self._mailbox.logout()
        except Exception as e:
            logger.debug(f"[{self.source_name}] IMAP logout error: {e}")
        self._mailbox = None

    # ── Channel/folder helpers ───────────────────────────────────────────────

    def _channel_id(self, folder: str) -> str:
        return f"folder:{folder}"

    def _display_name(self, folder: str) -> str:
        return self._channel_names.get(folder, folder)

    def _folder_for_channel(self, channel_id: str) -> Optional[str]:
        if channel_id.startswith("folder:"):
            return channel_id[len("folder:"):]
        return None

    # ── Sender info ──────────────────────────────────────────────────────────

    def get_sender_info(self, sender_id: str) -> dict:
        """`sender_id` IS the email address. Display name comes from From headers
        seen during this fetch (cached in `_sender_name_cache`); the local part
        of the address is the fallback so `senders.full_name` is never null when
        we have something better than the bare address to show."""
        addr = (sender_id or "").lower()
        display = self._sender_name_cache.get(addr) or (addr.split("@", 1)[0] if "@" in addr else addr)
        return {
            "sender_id": addr,
            "source": self.source_name,
            "username": addr,
            "full_name": display,
            "email": addr,
            "phone": None,
            "avatar_url": None,
            "extra": {},
        }

    # ── fetch_messages (scheduled ingest) ────────────────────────────────────

    def fetch_messages(
        self,
        since: Optional[datetime] = None,
        force: bool = False,
        default_since_ms: Optional[int] = None,
    ) -> dict:
        """Iterate configured folders, fetch new UIDs per folder, normalize and return."""
        messages: list[dict] = []
        scanned = 0
        skipped = 0

        if self._mailbox is None:
            raise RuntimeError("EmailConnector not connected — call .connect() first")

        for folder in self._folders:
            if folder in self._skip_folders:
                skipped += 1
                logger.debug(f"[{self.source_name}] Skip folder (skip_list): {folder}")
                continue

            try:
                folder_msgs = self._fetch_folder(folder, since=since, force=force)
            except Exception as e:
                logger.warning(f"[{self.source_name}/{folder}] fetch failed: {e}")
                continue

            scanned += 1
            messages.extend(folder_msgs)

        return {
            "messages": messages,
            "messages_count": len(messages),
            "channels_scanned": scanned,
            "channels_skipped": skipped,
        }

    def fetch_new(self, channel: str, limit: int = 50) -> list[dict]:
        """Live (gap-read) fetch is not supported for IMAP — punt cleanly.

        IMAP polling is heavier than a chat HTTP call (open, select, fetch by UID)
        and isn't worth the complexity for v1. The scheduler picks up new email on
        its normal cadence.
        """
        raise NotImplementedError(
            "Live fetch not supported for IMAP sources; "
            "the next scheduled ingest will pick up new email."
        )

    # ── Per-folder fetch ─────────────────────────────────────────────────────

    def _fetch_folder(
        self,
        folder: str,
        since: Optional[datetime] = None,
        force: bool = False,
    ) -> list[dict]:
        """Fetch new mail in one folder and persist the new high-water mark.

        First-run / forced-rescan / UIDVALIDITY-changed paths use IMAP's `SINCE`
        date predicate so we don't drag in years of history. (IMAP `ALL` returns
        oldest-UID first, so an un-dated `ALL` + `limit=N` gives the WRONG N.)
        Incremental runs use the saved UID watermark and ignore date — the UID
        range alone is the correct gap.
        """
        mb = self._mailbox
        mb.folder.set(folder)

        # Read UIDVALIDITY for this folder; if it changed we must re-scan.
        status = mb.folder.status(folder, [
            MailBoxFolderStatusOptions.UIDVALIDITY,
            MailBoxFolderStatusOptions.UIDNEXT,
        ])
        uidvalidity = int(status.get("UIDVALIDITY", 0) or 0)

        channel_id = self._channel_id(folder)
        prev_state = self._channel_state(channel_id)
        prev_uv = int(prev_state.get("uidvalidity") or 0)
        prev_uid = int(prev_state.get("last_uid") or 0)

        no_state = prev_uv != uidvalidity or prev_uid <= 0
        if force or no_state:
            # Bound first-run / forced rescan by date (IMAP `SINCE`) so we don't
            # haul in the whole archive. `default_limit` is the safety cap when
            # `since` is not provided (`memorandum --hours 0` etc.).
            if since is not None:
                criteria = AND(date_gte=since.date())
                limit = None
            else:
                criteria = "ALL"
                limit = self._default_limit
        else:
            criteria = f"UID {prev_uid + 1}:*"
            limit = None

        new_messages: list[dict] = []
        max_uid = prev_uid
        for mm in mb.fetch(criteria, limit=limit, mark_seen=False, headers_only=False):
            try:
                uid_int = int(mm.uid) if mm.uid else 0
            except (TypeError, ValueError):
                uid_int = 0
            # IMAP UID ranges like "N:*" are inclusive of N; skip duplicates explicitly.
            if uid_int and prev_uv == uidvalidity and not force and uid_int <= prev_uid:
                continue

            normalized = self._normalize(mm, folder)
            if normalized is None:
                continue
            new_messages.append(normalized)
            if uid_int > max_uid:
                max_uid = uid_int

        if self._db is not None:
            self._upsert_channel(channel_id, folder, uidvalidity, max_uid)

        return new_messages

    # ── Channel state helpers ────────────────────────────────────────────────

    def _channel_state(self, channel_id: str) -> dict:
        if self._db is None:
            return {}
        row = self._db.get_channel_row(self.source_name, channel_id)
        extra = (row or {}).get("extra") or {}
        return extra if isinstance(extra, dict) else {}

    def _upsert_channel(self, channel_id: str, folder: str, uidvalidity: int, last_uid: int) -> None:
        from pipeline.ingest import parse_channel_issue_ids  # lazy: avoid cycle

        issue_ids = parse_channel_issue_ids(
            folder, self._youtrack_cfg.get("project_prefixes")
        )
        extra: dict = {"uidvalidity": uidvalidity, "last_uid": last_uid}
        if issue_ids:
            extra["issue_ids"] = issue_ids
        try:
            self._db.upsert_channel({
                "id": channel_id,
                "source": self.source_name,
                "name": folder,
                "display_name": self._display_name(folder),
                "channel_type": "email",
                "extra": extra,
                "last_update_at": last_uid,
            })
        except Exception as e:
            logger.warning(f"[{self.source_name}/{folder}] upsert_channel failed: {e}")

    # ── Normalization ────────────────────────────────────────────────────────

    def _normalize(self, mm: MailMessage, folder: str) -> Optional[dict]:
        """Turn a MailMessage into the canonical msg dict the ingest pipeline expects."""
        message_id = (mm.headers.get("message-id") or (None,))[0] if mm.headers else None
        if not message_id:
            # Fall back to UID-based id when the sender omitted Message-ID.
            uid = mm.uid or "noid"
            message_id = f"<uid-{folder}-{uid}@{self.source_name}>"

        msg_id = f"{self.source_name}:{message_id}"

        from_pair = mm.from_values
        sender_addr = (from_pair.email if from_pair else mm.from_ or "").lower()
        from_name = (from_pair.name if from_pair else "").strip()
        sender_display = from_name or sender_addr
        # Cache the display name so get_sender_info can fill senders.full_name.
        if sender_addr and from_name:
            self._sender_name_cache[sender_addr] = from_name

        to_pairs = _ensure_iter_addresses(mm.to_values)
        cc_pairs = _ensure_iter_addresses(mm.cc_values)
        recipient_emails = [a for _, a in (to_pairs + cc_pairs)]

        in_reply_to_raw = (mm.headers.get("in-reply-to") or (None,))[0] if mm.headers else None
        refs_raw = (mm.headers.get("references") or (None,))[0] if mm.headers else None
        references = _parse_references(refs_raw)
        # Root of thread: first reference, else self (an orphan starts its own thread).
        thread_root = references[0] if references else message_id

        body = mm.text or _strip_html(mm.html or "")
        subject = (mm.subject or "").strip()
        # Order: Subject, then attachment markers (so they survive the 200-char
        # preview truncation in format_message), then text-attachment previews,
        # then the body itself. The full text — markers and all — is still
        # stored in the DB and indexed in the vector store regardless.
        text_parts = [f"Subject: {subject}"] if subject else []
        text_preview_parts: list[str] = []
        for att in mm.attachments or ():
            marker, preview = self._attachment_marker(att)
            text_parts.append(marker)
            if preview:
                text_preview_parts.append(preview)
        text_parts.extend(text_preview_parts)
        if body:
            text_parts.append(body.strip())
        text = "\n\n".join(text_parts).strip()

        timestamp = mm.date_str
        try:
            ts = mm.date.astimezone(timezone.utc).isoformat() if mm.date else timestamp
        except Exception:
            ts = timestamp

        return {
            "id": msg_id,
            "source": self.source_name,
            "channel_id": self._channel_id(folder),
            "sender": sender_display,
            "sender_id": sender_addr,
            "sender_email": sender_addr,
            "timestamp": ts,
            "text": text,
            "thread_id": thread_root,
            "reply_to_id": in_reply_to_raw,
            "tags": [],
            "raw": {
                "from": sender_addr,
                "from_name": (from_pair.name if from_pair else ""),
                "to": [{"name": n, "email": e} for n, e in to_pairs],
                "cc": [{"name": n, "email": e} for n, e in cc_pairs],
                "subject": subject,
                "message_id": message_id,
                "in_reply_to": in_reply_to_raw,
                "references": references,
                "folder": folder,
                "uid": mm.uid,
            },
            "_recipient_emails": recipient_emails,
        }

    def _attachment_marker(self, att) -> tuple[str, str]:
        """Cache attachment and return ``(marker, preview)``.

        ``marker`` is the inline `[attachment: name, file_id=...]` line, kept
        short and hoisted near the top of the message so a truncated display
        still shows the file_id. ``preview`` is the first 5KB of a text-typed
        attachment (empty string for binaries) — appended later in the text.
        """
        filename = getattr(att, "filename", None) or "attachment"
        ext = Path(filename).suffix.lower()
        # Stable file_id: based on payload content (mirrors how Pachca/Mattermost cache).
        import hashlib
        try:
            payload: bytes = att.payload or b""
        except Exception:
            payload = b""
        file_id = hashlib.sha1(payload).hexdigest()[:24] if payload else uuid.uuid4().hex[:24]
        cache_path = Path(self._file_cache_dir) / f"{file_id}{ext}"
        try:
            if payload and not cache_path.exists():
                cache_path.write_bytes(payload)
        except OSError as e:
            logger.debug(f"[{self.source_name}] cache write failed for {filename}: {e}")

        marker = f"[attachment: {filename}, file_id={file_id}]"
        preview = ""
        if ext in self._text_extensions and payload:
            preview = payload[:MAX_TEXT_PREVIEW_SIZE].decode("utf-8", errors="ignore")
        return marker, preview

    # ── send_message (DRAFT via IMAP APPEND) ─────────────────────────────────

    def send_message(
        self,
        channel: Optional[str],
        text: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """Build a reply and APPEND it to the Drafts folder. Returns the new Message-ID.

        This does NOT send via SMTP — the user reviews the draft in their mail client
        and clicks Send. `reply_to` is the original Message-ID being replied to;
        recipients and threading headers are derived from it.
        """
        if not text or not text.strip():
            raise ValueError("Refusing to draft an empty email body.")
        if not reply_to:
            raise ValueError(
                "Email send requires reply_to: the Message-ID of the email being replied to "
                "(new-thread sending is out of scope for v1)."
            )
        if self._db is None:
            raise RuntimeError("EmailConnector.send_message needs a db (parent lookup).")
        if self._mailbox is None:
            raise RuntimeError("EmailConnector not connected — call .connect() first")

        parent = self._lookup_parent(reply_to)
        if parent is None:
            raise ValueError(
                f"Parent message '{reply_to}' not found in the local DB; cannot derive "
                f"recipients. Ingest the thread first or supply the exact Message-ID."
            )

        raw = parent.get("raw") or {}
        if isinstance(raw, str):
            import json as _json
            try:
                raw = _json.loads(raw)
            except (ValueError, TypeError):
                raw = {}

        msg = self._build_reply(raw, reply_to, text)
        new_message_id = msg["Message-ID"]
        flags = [MailMessageFlags.DRAFT, MailMessageFlags.SEEN]

        self._mailbox.append(
            msg.as_bytes(),
            folder=self._drafts_folder,
            flag_set=flags,
        )
        logger.info(
            f"[{self.source_name}] Drafted reply to {reply_to} "
            f"in folder '{self._drafts_folder}' (new id {new_message_id})"
        )
        return new_message_id

    def _lookup_parent(self, message_id: str) -> Optional[dict]:
        """Find the parent message in the local DB by its Message-ID."""
        candidates = [
            f"{self.source_name}:{message_id}",
            message_id,
        ]
        for full_id in candidates:
            rows = self._db.get_by_ids([full_id])
            if rows:
                return rows[0]
        return None

    def _build_reply(self, parent_raw: dict, parent_message_id: str, body: str) -> EmailMessage:
        """Build an RFC 5322 reply with reply-all recipients minus self."""
        me = self.from_address

        original_from = (parent_raw.get("from") or "").lower()
        original_to = [
            (r.get("email") or "").lower()
            for r in (parent_raw.get("to") or [])
            if r.get("email")
        ]
        original_cc = [
            (r.get("email") or "").lower()
            for r in (parent_raw.get("cc") or [])
            if r.get("email")
        ]
        original_subject = parent_raw.get("subject") or ""
        original_refs = list(parent_raw.get("references") or [])

        # Reply-all: To = original sender; Cc = (original To + Cc) - self - sender.
        to_addr = original_from
        cc_set: list[str] = []
        seen = {me, original_from}
        for addr in original_to + original_cc:
            if addr and addr not in seen:
                cc_set.append(addr)
                seen.add(addr)

        msg = EmailMessage()
        msg["From"] = me
        if to_addr:
            msg["To"] = to_addr
        if cc_set:
            msg["Cc"] = ", ".join(cc_set)
        subject = original_subject if original_subject.lower().startswith("re:") \
            else f"Re: {original_subject}".strip()
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["In-Reply-To"] = parent_message_id
        refs = original_refs + [parent_message_id]
        msg["References"] = " ".join(refs)
        new_id = email.utils.make_msgid(domain=(me.split("@", 1)[1] if "@" in me else None))
        msg["Message-ID"] = new_id
        msg.set_content(body, subtype="plain", charset="utf-8")
        return msg
