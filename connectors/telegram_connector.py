"""Telegram connector using Bot API (HTTP REST)."""
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable

from ._common import DEFAULT_TEXT_EXTENSIONS, MAX_TEXT_PREVIEW_SIZE

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"

# Chat types where t.me/c/ permalinks are valid
_LINKABLE_CHAT_TYPES = {"channel", "supergroup"}


class TelegramConnector:
    """Connector for Telegram using the Bot API.

    Requires a bot token from @BotFather. The bot must have access to the chats
    you want to monitor (admin, privacy mode disabled, or secretary mode).

    Uses getUpdates with a global offset for incremental sync. The offset is
    persisted in the channels table under id="__offset__" so restarts resume
    without duplicates.

    Collects: groups, supergroups, channels (channel_post), business messages.
    Skips: direct messages sent to the bot itself (private chat, message type).
    """

    def __init__(
        self,
        bot_token: str,
        source_name: str = "telegram",
        chat_ids: list = None,
        db_callback: Callable = None,
        db=None,
        attachments_path: str = "data/attachments",
        text_extensions: set = None,
        youtrack_cfg: dict = None,
    ):
        self.source_name = source_name
        self._token = bot_token
        self._chat_ids = set(str(c) for c in (chat_ids or []))
        self._db_callback = db_callback
        self._db = db
        self._attachments_path = attachments_path
        self._text_extensions = text_extensions or DEFAULT_TEXT_EXTENSIONS
        self._bot_info: dict = {}
        self._sender_cache: dict[str, dict] = {}
        self._chat_desc_cache: dict[str, Optional[str]] = {}  # chat_id → description (or None)
        self._youtrack_cfg = youtrack_cfg or {}

        Path(attachments_path).mkdir(parents=True, exist_ok=True)

    # ── API helpers ───────────────────────────────────────────────────────────

    def _api(self, method: str, params: dict = None, timeout: int = 30, http_method: str = "GET") -> object:
        url = f"{_API_BASE}/bot{self._token}/{method}"
        if http_method == "POST":
            resp = requests.post(url, data=params, timeout=timeout)
        else:
            resp = requests.get(url, params=params, timeout=timeout)
        # Telegram puts the failure reason in the JSON body's `description`, even on 4xx.
        # Read it before raising so the reason isn't lost (e.g. "chat not found" on 400).
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not resp.ok:
            desc = data.get("description")
            if desc:
                # Keep response attached so callers can still branch on status_code (e.g. 409).
                raise requests.HTTPError(
                    f"Telegram API error [{method}] {resp.status_code}: {desc}", response=resp
                )
            resp.raise_for_status()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error [{method}]: {data.get('description', 'unknown')}")
        return data["result"]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Verify token via getMe."""
        self._bot_info = self._api("getMe")
        logger.info(f"[{self.source_name}] Connected as: @{self._bot_info.get('username')}")

    def disconnect(self) -> None:
        self._bot_info = {}

    # ── Offset persistence ────────────────────────────────────────────────────

    def _load_offset(self) -> Optional[int]:
        if self._db_callback:
            return self._db_callback(self.source_name, "__offset__")
        return None

    def _save_offset(self, offset: int) -> None:
        if self._db:
            self._db.upsert_channel({
                "id": "__offset__",
                "source": self.source_name,
                "name": "__offset__",
                "display_name": f"{self.source_name} update offset",
                "last_update_at": offset,
            })

    # ── Chat helpers ──────────────────────────────────────────────────────────

    def _url_chat_id(self, chat_id: int) -> str:
        """Strip the -100 prefix used by supergroups/channels for t.me/c/ links."""
        s = str(chat_id)
        if s.startswith("-100"):
            return s[4:]
        return s.lstrip("-")

    def _fetch_chat_description(self, chat: dict) -> Optional[str]:
        """Return the chat's description via getChat, cached per chat for the connector's lifetime.

        Skips private chats (no description, only a `bio` we don't use). Failures are logged
        and cached as None so we don't retry every poll.
        """
        chat_id = str(chat.get("id", ""))
        if not chat_id or chat.get("type") == "private":
            return None
        if chat_id in self._chat_desc_cache:
            return self._chat_desc_cache[chat_id]
        try:
            info = self._api("getChat", {"chat_id": chat_id})
            desc = (info.get("description") or "").strip() or None
        except Exception as e:
            logger.warning(f"[{self.source_name}/{chat_id}] getChat failed: {e}")
            desc = None
        self._chat_desc_cache[chat_id] = desc
        return desc

    def _upsert_chat(self, chat: dict, current_time_ms: int,
                     business_connection_id: str = None) -> None:
        if not self._db:
            return
        chat_id = chat["id"]
        title = (chat.get("title") or chat.get("username")
                 or chat.get("first_name") or str(chat_id))
        # Business (private) chats can only be replied to via their business_connection_id,
        # so persist it on the channel for send_message to look up later.
        from pipeline.ingest import parse_channel_issue_ids  # lazy
        issue_ids = parse_channel_issue_ids(title, self._youtrack_cfg.get("project_prefixes"))
        extra: dict = {}
        if business_connection_id:
            extra["business_connection_id"] = business_connection_id
        if issue_ids:
            extra["issue_ids"] = issue_ids
        extra = extra or None
        try:
            self._db.upsert_channel({
                "id": str(chat_id),
                "source": self.source_name,
                "name": chat.get("username") or str(chat_id),
                "display_name": title,
                "description": self._fetch_chat_description(chat),
                "channel_type": chat.get("type"),
                "extra": extra,
                "last_update_at": current_time_ms,
            })
        except Exception as e:
            logger.warning(f"[{self.source_name}/{title}] Failed to upsert chat: {e}")

    # ── File handling ─────────────────────────────────────────────────────────

    def _download_file(self, file_id: str, name: str = "") -> Optional[str]:
        """Download a file, cache it, and return text content for text files."""
        try:
            file_info = self._api("getFile", {"file_id": file_id})
            file_path = file_info.get("file_path", "")
            url = f"{_API_BASE}/file/bot{self._token}/{file_path}"
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            content = resp.content

            ext = Path(name).suffix.lower() if name else ""
            cache_path = Path(self._attachments_path) / f"{file_id}{ext}"
            with open(cache_path, "wb") as f:
                f.write(content)

            if ext in self._text_extensions:
                return content.decode("utf-8", errors="ignore")
        except Exception as e:
            logger.warning(f"[{self.source_name}] Failed to download file {file_id}: {e}")
        return None

    def _process_media(self, msg: dict) -> tuple[str, list]:
        """Return (text_indicators, file_metadata_list) for any attached media."""
        text_parts = []
        file_metadata = []

        doc = msg.get("document")
        if doc:
            file_id = doc.get("file_id", "")
            name = doc.get("file_name") or file_id
            mime = doc.get("mime_type", "")
            ext = Path(name).suffix.lower()
            file_metadata.append({"file_id": file_id, "name": name, "mime_type": mime})
            if ext in self._text_extensions:
                preview = self._download_file(file_id, name)
                if preview:
                    text_parts.append(f"[document: {name}, file_id={file_id}]\n{preview[:MAX_TEXT_PREVIEW_SIZE]}")
                else:
                    text_parts.append(f"[document: {name}, file_id={file_id}]")
            else:
                text_parts.append(f"[document: {name}, file_id={file_id}]")

        photos = msg.get("photo")
        if photos:
            photo = photos[-1]  # largest size
            file_id = photo.get("file_id", "")
            file_metadata.append({
                "file_id": file_id,
                "name": "photo.jpg",
                "mime_type": "image/jpeg",
            })
            text_parts.append(f"[photo, file_id={file_id}]")

        audio = msg.get("audio")
        if audio:
            file_id = audio.get("file_id", "")
            name = audio.get("file_name") or "audio"
            file_metadata.append({
                "file_id": file_id,
                "name": name,
                "mime_type": audio.get("mime_type", "audio/*"),
            })
            text_parts.append(f"[audio: {name}, file_id={file_id}]")

        voice = msg.get("voice")
        if voice:
            file_id = voice.get("file_id", "")
            file_metadata.append({
                "file_id": file_id,
                "name": "voice.ogg",
                "mime_type": "audio/ogg",
            })
            text_parts.append(f"[voice message, file_id={file_id}]")

        video = msg.get("video")
        if video:
            file_id = video.get("file_id", "")
            name = video.get("file_name") or "video"
            file_metadata.append({
                "file_id": file_id,
                "name": name,
                "mime_type": video.get("mime_type", "video/*"),
            })
            text_parts.append(f"[video: {name}, file_id={file_id}]")

        return "\n".join(text_parts), file_metadata

    # ── Message normalization ─────────────────────────────────────────────────

    def _normalize(self, msg: dict, chat: dict) -> dict:
        chat_id = chat["id"]
        title = (chat.get("title") or chat.get("username")
                 or chat.get("first_name") or str(chat_id))

        sender = msg.get("from") or {}
        sender_id = str(sender.get("id", ""))
        first_name = sender.get("first_name", "")
        last_name = sender.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip() or None
        username = sender.get("username")
        sender_name = username or full_name or sender_id or title

        if sender_id and sender_id not in self._sender_cache:
            self._sender_cache[sender_id] = {
                "sender_id": sender_id,
                "source": self.source_name,
                "username": username,
                "full_name": full_name,
                "email": None,
                "phone": None,
                "avatar_url": None,
                "extra": {},
            }

        text = msg.get("text") or msg.get("caption") or ""
        media_preview, file_metadata = self._process_media(msg)
        if media_preview:
            text = f"{text}\n\n{media_preview}" if text else media_preview

        timestamp = datetime.fromtimestamp(msg["date"], tz=timezone.utc).isoformat()
        message_id = msg["message_id"]
        reply_to = msg.get("reply_to_message")
        chat_type = chat.get("type", "")

        from pipeline.ingest import extract_urls  # lazy
        return {
            "id": f"{self.source_name}:{chat_id}:{message_id}",
            "source": self.source_name,
            "channel_id": str(chat_id),
            "sender": sender_name,
            "sender_id": sender_id,
            "timestamp": timestamp,
            "text": text,
            "thread_id": str(msg["message_thread_id"]) if msg.get("message_thread_id") else None,
            "reply_to_id": str(reply_to["message_id"]) if reply_to else None,
            "tags": [],
            "raw": {
                "chat_id": self._url_chat_id(chat_id) if chat_type in _LINKABLE_CHAT_TYPES else "",
                "message_id": message_id,
                "chat_type": chat_type,
                "files": file_metadata,
                "urls": extract_urls(text, self._youtrack_cfg),
            },
        }

    # ── Sender info ───────────────────────────────────────────────────────────

    def get_sender_info(self, user_id: str) -> dict:
        return self._sender_cache.get(user_id) or {
            "sender_id": user_id,
            "source": self.source_name,
            "username": None,
            "full_name": None,
            "email": None,
            "phone": None,
            "avatar_url": None,
            "extra": {},
        }

    def send_message(self, chat_id, text: str, reply_to: int = None,
                     business_connection_id: str = None) -> int:
        """Send a message via Bot API sendMessage (POST). Returns the new message id.

        reply_to threads the message under an existing one; business_connection_id is
        required to send from a business/secretary chat (from BusinessConnection updates).
        """
        params: dict = {"chat_id": chat_id, "text": text}
        if reply_to:
            params["reply_to_message_id"] = reply_to
        if business_connection_id:
            params["business_connection_id"] = business_connection_id
        try:
            result = self._api("sendMessage", params, http_method="POST")
        except requests.HTTPError as e:
            if "BUSINESS_PEER_INVALID" in str(e):
                raise RuntimeError(
                    "Telegram refused this business reply (BUSINESS_PEER_INVALID). The "
                    "business_connection_id is accepted, but Telegram won't let the bot message "
                    f"chat {chat_id} on behalf of the business account. Usual causes: the bot's "
                    "reply rights were not granted (or were revoked) in the owner's Telegram "
                    "Business → Chatbots settings, the connection is disabled, or that chat is no "
                    "longer an active recipient. Re-grant the bot's reply permission and retry."
                ) from e
            raise
        return result.get("message_id")

    def fetch_new(self, channel: str, limit: int = 50) -> list[dict]:
        """Fetch new updates for one chat since the saved offset (oldest→newest).

        A single read-only getUpdates call starting at the saved offset. It does NOT
        advance the offset or write any state, so the scheduler still picks these updates
        up and stores them (no consume-once data loss). `channel` is the chat id to filter
        to. Raises RuntimeError if Telegram reports a concurrent getUpdates (409).
        """
        offset = self._load_offset()
        params: dict = {
            "timeout": 0,
            "limit": 100,
            "allowed_updates": ["message", "channel_post", "business_message"],
        }
        if offset is not None:
            params["offset"] = offset

        try:
            updates = self._api("getUpdates", params, timeout=15)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 409:
                raise RuntimeError(
                    "Telegram is busy with another getUpdates request (likely an ingest run); "
                    "try again in a moment."
                )
            raise

        messages = []
        for update in updates:
            is_business = "business_message" in update
            msg = (update.get("message") or update.get("channel_post")
                   or update.get("business_message"))
            if not msg:
                continue

            chat = msg.get("chat", {})
            if str(chat.get("id", "")) != str(channel):
                continue
            if not is_business and chat.get("type") == "private":
                continue
            if not (msg.get("text") or msg.get("caption")
                    or msg.get("document") or msg.get("photo")
                    or msg.get("audio") or msg.get("voice")
                    or msg.get("video")):
                continue

            normalized = self._normalize(msg, chat)
            # Live messages aren't read back through the DB join, so attach the chat name here.
            normalized["channel"] = (chat.get("title") or chat.get("username")
                                     or chat.get("first_name") or str(chat.get("id", "")))
            messages.append(normalized)

        messages.sort(key=lambda m: m["timestamp"])
        return messages[-limit:]

    # ── Main fetch ────────────────────────────────────────────────────────────

    def fetch_messages(
        self,
        since: datetime = None,
        force: bool = False,
        default_since_ms: int = None,
    ) -> dict:
        """Poll getUpdates with offset tracking and return normalized messages.

        Collects message, channel_post, and business_message update types.
        Skips direct messages to the bot itself (private chat via message type).

        When force=True and no offset is saved, messages older than `since`
        are skipped at the timestamp level (Bot API delivers them anyway).
        """
        messages = []
        chats_seen: set = set()
        current_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        offset = None if force else self._load_offset()
        since_ts = int(since.timestamp()) if since and force else 0

        logger.info(f"[{self.source_name}] Fetching updates (offset={offset}, force={force})")

        while True:
            params: dict = {
                "timeout": 0,
                "limit": 100,
                "allowed_updates": ["message", "channel_post", "business_message"],
            }
            if offset is not None:
                params["offset"] = offset

            try:
                updates = self._api("getUpdates", params, timeout=15)
            except Exception as e:
                logger.error(f"[{self.source_name}] getUpdates failed: {e}")
                break

            if not updates:
                break

            for update in updates:
                offset = update["update_id"] + 1

                is_business = "business_message" in update
                msg = (update.get("message") or update.get("channel_post")
                       or update.get("business_message"))
                if not msg:
                    continue

                chat = msg.get("chat", {})
                chat_id = str(chat.get("id", ""))

                # Skip DMs sent directly to the bot (not business messages)
                if not is_business and chat.get("type") == "private":
                    continue

                if self._chat_ids and chat_id not in self._chat_ids:
                    continue

                # On first force-run (no saved offset), drop messages older than since
                if since_ts and msg.get("date", 0) < since_ts:
                    continue

                if not (msg.get("text") or msg.get("caption")
                        or msg.get("document") or msg.get("photo")
                        or msg.get("audio") or msg.get("voice")
                        or msg.get("video")):
                    continue

                messages.append(self._normalize(msg, chat))

                if chat_id not in chats_seen:
                    chats_seen.add(chat_id)
                    bcid = msg.get("business_connection_id") if is_business else None
                    self._upsert_chat(chat, current_time_ms, business_connection_id=bcid)

            self._save_offset(offset)

            if len(updates) < 100:
                break

        logger.info(
            f"[{self.source_name}] Total: {len(messages)} messages "
            f"from {len(chats_seen)} chats"
        )
        return {
            "messages": messages,
            "messages_count": len(messages),
            "channels_scanned": len(chats_seen),
            "channels_skipped": 0,
        }
