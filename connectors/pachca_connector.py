"""Pachca connector using REST API."""
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger(__name__)

_API_BASE = "https://api.pachca.com/api/shared/v1"
MAX_TEXT_PREVIEW_SIZE = 5 * 1024  # first 5KB inlined for text attachments


class PachcaConnector:
    """Connector for Pachca using the REST API.

    Auth: personal access token (Automations → API in settings).
    Incremental sync: per-chat newest message ID stored in channels.last_update_at.
    """

    def __init__(
        self,
        access_token: str,
        source_name: str = "pachca",
        only_channels: list = None,
        skip_channels: list = None,
        db_callback: Callable = None,
        db=None,
        default_limit: int = 200,
        text_extensions: set = None,
        file_cache_dir: str = "data/file_cache",
        youtrack_cfg: dict = None,
    ):
        self.source_name = source_name
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self._only_channels = set(only_channels or [])
        self._skip_channels = set(skip_channels or [])
        self._db_callback = db_callback
        self._db = db
        self._default_limit = default_limit
        self._text_extensions = text_extensions or set()
        self._file_cache_dir = file_cache_dir
        self._youtrack_cfg = youtrack_cfg or {}
        self._user_cache: dict = {}
        self._me_id: Optional[int] = None

        Path(file_cache_dir).mkdir(parents=True, exist_ok=True)

    # ── API helper ────────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{_API_BASE}{path}"
        resp = requests.get(url, headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, json: dict = None) -> dict:
        url = f"{_API_BASE}{path}"
        resp = requests.post(url, headers=self._headers, json=json, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Verify token via GET /profile (current user)."""
        data = self._get("/profile")
        me = data.get("data", data)
        self._me_id = me.get("id")
        name = self._full_name(me) or me.get("nickname") or str(me.get("id", "unknown"))
        logger.info(f"[{self.source_name}] Connected as: {name}")

    def disconnect(self) -> None:
        self._user_cache.clear()

    # ── User lookup ───────────────────────────────────────────────────────────

    @staticmethod
    def _full_name(user: dict) -> Optional[str]:
        """Build a full name from first_name + last_name (User has no `name` field)."""
        full = f"{user.get('first_name', '')} {user.get('last_name') or ''}".strip()
        return full or None

    def _get_user(self, user_id: int) -> dict:
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        try:
            data = self._get(f"/users/{user_id}")
            user = data.get("data", {})
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 404:
                # System/deleted/inaccessible user (e.g. service messages). Expected, not noisy.
                logger.debug(f"[{self.source_name}] User {user_id} not found (system/inaccessible)")
            else:
                logger.warning(f"[{self.source_name}] Failed to fetch user {user_id}: {e}")
            user = {}
        except requests.RequestException as e:
            logger.warning(f"[{self.source_name}] Failed to fetch user {user_id}: {e}")
            user = {}
        # Cache negative results too, so we don't re-fetch (and re-log) the same user.
        self._user_cache[user_id] = user
        return user

    def get_sender_info(self, user_id: str) -> dict:
        user = self._get_user(int(user_id)) if user_id and user_id.isdigit() else {}
        full_name = self._full_name(user)
        return {
            "sender_id": user_id,
            "source": self.source_name,
            "username": user.get("nickname") or user_id,
            "full_name": full_name,
            "email": user.get("email"),
            "phone": user.get("phone_number"),
            "avatar_url": user.get("image_url"),
            "extra": {},
        }

    # ── Chat listing ──────────────────────────────────────────────────────────

    @staticmethod
    def _chat_type(chat: dict) -> str:
        """Derive a channel_type label from the Chat booleans (channel/personal)."""
        if chat.get("channel"):
            return "channel"
        if chat.get("personal"):
            return "personal"
        return "group"

    def _chat_display_name(self, chat: dict) -> str:
        """Friendly chat name. Personal chats have no `name`, so use the partner's name."""
        name = chat.get("name")
        if name:
            return name
        if chat.get("personal"):
            members = [m for m in (chat.get("member_ids") or []) if m != self._me_id]
            partner_id = members[0] if members else None
            if partner_id:
                user = self._get_user(partner_id)
                partner = self._full_name(user) or user.get("nickname")
                if partner:
                    return partner
        return str(chat.get("id"))

    def _list_chats(self) -> list[dict]:
        chats = []
        cursor = None
        while True:
            params = {"availability": "is_member", "limit": 50}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/chats", params=params)
            chats.extend(data.get("data", []))
            paginate = data.get("meta", {}).get("paginate", {})
            if not paginate.get("has_next"):
                break
            cursor = paginate.get("next_page")
        return chats

    def _normalize_message(self, raw_msg: dict, chat_id: int) -> dict:
        """Convert a raw Pachca message into a normalized message dict."""
        msg_id = raw_msg.get("id", 0)
        user_id = raw_msg.get("user_id")
        # Message carries the sender's display name; fall back to a user lookup.
        sender_name = raw_msg.get("display_name")
        if not sender_name:
            user = self._get_user(user_id) if user_id else {}
            sender_name = self._full_name(user) or user.get("nickname") or str(user_id or "")

        try:
            timestamp = datetime.fromisoformat(
                raw_msg["created_at"].replace("Z", "+00:00")
            ).isoformat()
        except (KeyError, ValueError):
            timestamp = datetime.now(timezone.utc).isoformat()

        thread = raw_msg.get("thread") or {}
        thread_id = thread.get("id")
        parent_id = raw_msg.get("parent_message_id")

        text, file_metadata = self._process_files(raw_msg)

        from pipeline.ingest import extract_urls  # lazy
        return {
            "id": f"{self.source_name}:{msg_id}",
            "source": self.source_name,
            "channel_id": str(chat_id),
            "sender": sender_name,
            "sender_id": str(user_id or ""),
            "timestamp": timestamp,
            "text": text,
            "thread_id": str(thread_id) if thread_id else None,
            "reply_to_id": str(parent_id) if parent_id else None,
            "tags": [],
            "raw": {
                "message_id": msg_id,
                "chat_id": chat_id,
                "entity_type": raw_msg.get("entity_type"),
                "entity_id": raw_msg.get("entity_id"),
                "files": file_metadata,
                "urls": extract_urls(text, self._youtrack_cfg),
            },
        }

    def _download_file(self, url: str, file_id, name: str) -> Optional[Path]:
        """Download an attachment to the file cache as `{file_id}{ext}` (idempotent).

        Pachca download URLs are signed and short-lived, so we fetch at ingest while the URL
        is still valid. Returns the cached path, or None on failure. The S3 URL is
        pre-signed — no Authorization header is sent.
        """
        if not url or file_id is None:
            return None
        ext = Path(name).suffix.lower()
        cache_path = Path(self._file_cache_dir) / f"{file_id}{ext}"
        if cache_path.exists():
            return cache_path
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            cache_path.write_bytes(resp.content)
            return cache_path
        except Exception as e:
            logger.warning(f"[{self.source_name}] Failed to download file {file_id} ({name}): {e}")
            return None

    def _process_files(self, raw_msg: dict) -> tuple[str, list]:
        """Embed attachment references in the message text and cache the files.

        Each file becomes `[<file_type>: <name>, file_id=<id>]` (no long signed URL), and is
        downloaded into the file cache so `get_attached_file` can serve it by id later. Text
        files also get their first 5KB inlined. Returns (text, file_metadata).
        """
        text = raw_msg.get("content", "") or ""
        files = raw_msg.get("files") or []
        if not files:
            return text, []

        refs = []
        previews = []
        file_metadata = []
        for f in files:
            file_id = f.get("id")
            name = f.get("name", "file")
            ftype = f.get("file_type", "file")
            cache_path = self._download_file(f.get("url", ""), file_id, name)

            refs.append(f"[{ftype}: {name}, file_id={file_id}]")
            file_metadata.append({"id": file_id, "name": name, "file_type": ftype})

            ext = Path(name).suffix.lower()
            if cache_path and ext in self._text_extensions:
                try:
                    content = cache_path.read_text(encoding="utf-8", errors="ignore")
                    previews.append(f"attached_files_content ({name}):\n{content[:MAX_TEXT_PREVIEW_SIZE]}")
                except Exception:
                    pass

        combined = f"{text}\n" + "\n".join(refs) if text else "\n".join(refs)
        if previews:
            combined = f"{combined}\n\n" + "\n\n".join(previews)
        return combined, file_metadata

    def _resolve_chat(self, chat: str) -> Optional[dict]:
        """Find a chat by id or name. Returns the chat dict or None if not found."""
        for c in self._list_chats():
            if chat in (str(c.get("id")), c.get("name")):
                return c
        return None

    def send_message(self, chat_id, text: str, parent_message_id: int = None) -> int:
        """Post a message via POST /messages. Returns the new message id.

        parent_message_id replies inside a thread. The chat is addressed as a discussion
        entity (entity_type="discussion", entity_id=chat_id).
        """
        message = {"entity_type": "discussion", "entity_id": chat_id, "content": text}
        if parent_message_id:
            message["parent_message_id"] = parent_message_id
        data = self._post("/messages", json={"message": message})
        return data.get("data", {}).get("id")

    def fetch_new(self, channel: str, limit: int = 50) -> list[dict]:
        """Fetch messages newer than the saved sync state for one chat (oldest→newest).

        Resolves the chat, reads its saved newest message id via the read-only db_callback,
        and returns messages since then. Falls back to the newest `limit` messages when the
        chat has no saved state. Writes nothing (db must be None). Raises ValueError if the
        chat cannot be resolved.
        """
        chat = self._resolve_chat(channel)
        if chat is None:
            raise ValueError(f"Chat '{channel}' not found in source '{self.source_name}'")

        chat_id = chat.get("id")
        last_seen_id = self._db_callback(self.source_name, str(chat_id)) if self._db_callback else None

        if last_seen_id:
            messages, _ = self._fetch_chat_messages(chat_id, last_seen_id, force=False)
        else:
            data = self._get("/messages", params={"chat_id": chat_id, "limit": limit})
            batch = data.get("data", [])
            messages = [self._normalize_message(m, chat_id) for m in batch[:limit]]

        # Live messages aren't read back through the DB join, so attach the chat name here.
        display = self._chat_display_name(chat)
        for m in messages:
            m["channel"] = display

        messages.sort(key=lambda m: m["timestamp"])
        return messages[-limit:]

    # ── Per-chat message fetch ────────────────────────────────────────────────

    def _fetch_chat_messages(
        self, chat_id: int, last_seen_id: Optional[int], force: bool
    ) -> tuple[list[dict], Optional[int]]:
        """Fetch messages for one chat, newest-first.

        Stops when msg["id"] <= last_seen_id (incremental sync) or after
        default_limit messages on first run. Returns (messages, newest_id).
        """
        messages = []
        newest_id: Optional[int] = None
        fetched = 0
        cursor = None

        while True:
            params = {"chat_id": chat_id, "limit": 50}
            if cursor:
                params["cursor"] = cursor

            try:
                data = self._get("/messages", params=params)
            except requests.RequestException as e:
                logger.warning(f"[{self.source_name}/{chat_id}] Failed to fetch messages: {e}")
                break

            batch = data.get("data", [])
            if not batch:
                break

            done = False
            for raw_msg in batch:
                msg_id = raw_msg.get("id", 0)

                if newest_id is None:
                    newest_id = msg_id

                # Incremental stop: already seen this and newer ones
                if not force and last_seen_id is not None and msg_id <= last_seen_id:
                    done = True
                    break

                messages.append(self._normalize_message(raw_msg, chat_id))
                fetched += 1

                # First-run cap: stop after default_limit messages
                if not force and last_seen_id is None and fetched >= self._default_limit:
                    done = True
                    break

            if done:
                break

            paginate = data.get("meta", {}).get("paginate", {})
            if not paginate.get("has_next"):
                break
            cursor = paginate.get("next_page")

        return messages, newest_id

    # ── Main fetch ────────────────────────────────────────────────────────────

    def fetch_messages(
        self,
        since: datetime = None,
        force: bool = False,
        default_since_ms: int = None,
    ) -> dict:
        """Fetch messages from all accessible chats with per-chat incremental sync."""
        messages = []
        channels_scanned = 0
        channels_skipped = 0

        chats = self._list_chats()
        logger.info(f"[{self.source_name}] Found {len(chats)} chats")

        for chat in chats:
            chat_id = chat.get("id")
            chat_name = self._chat_display_name(chat)

            if self._skip_channels and chat_name in self._skip_channels:
                channels_skipped += 1
                logger.debug(f"[{self.source_name}] Skipping chat (skip_list): {chat_name}")
                continue

            if self._only_channels and chat_name not in self._only_channels:
                channels_skipped += 1
                logger.debug(f"[{self.source_name}] Skipping chat (only_list): {chat_name}")
                continue

            last_seen_id = None
            if not force and self._db_callback:
                last_seen_id = self._db_callback(self.source_name, str(chat_id))

            chat_messages, newest_id = self._fetch_chat_messages(chat_id, last_seen_id, force)

            if self._db and newest_id is not None:
                from pipeline.ingest import parse_channel_issue_ids  # lazy
                issue_ids = parse_channel_issue_ids(
                    chat_name, self._youtrack_cfg.get("project_prefixes"))
                try:
                    self._db.upsert_channel({
                        "id": str(chat_id),
                        "source": self.source_name,
                        "name": chat.get("name") or str(chat_id),  # personal chats have no name
                        "display_name": chat_name,
                        "channel_type": self._chat_type(chat),
                        "extra": {"issue_ids": issue_ids} if issue_ids else None,
                        "last_update_at": newest_id,
                    })
                except Exception as e:
                    logger.warning(f"[{self.source_name}/{chat_name}] Failed to upsert channel: {e}")

            if chat_messages:
                logger.info(f"[{self.source_name}/{chat_name}] {len(chat_messages)} messages")
                messages.extend(chat_messages)
                channels_scanned += 1
            else:
                channels_skipped += 1

        logger.info(
            f"[{self.source_name}] Total: {len(messages)} messages "
            f"from {channels_scanned} chats"
        )
        return {
            "messages": messages,
            "messages_count": len(messages),
            "channels_scanned": channels_scanned,
            "channels_skipped": channels_skipped,
        }
