"""Mattermost connector using REST API."""
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable


# Configure logging
logger = logging.getLogger(__name__)

# File extensions that should have content extracted (defaults, can be overridden via config)
DEFAULT_TEXT_EXTENSIONS = {'.txt', '.log', '.md', '.markdown', '.json', '.xml', '.yaml', '.yml', '.csv', '.lst'}
MAX_TEXT_PREVIEW_SIZE = 5 * 1024  # 5KB for message text preview only


def _combine_purpose_header(purpose: str, header: str) -> Optional[str]:
    """Join Mattermost channel `purpose` and `header` into a single description.

    Both are user-set free text; purpose is "what this channel is for", header is the pinned
    banner. Either may be empty. Returns None when both are blank.
    """
    parts = [p.strip() for p in (purpose, header) if p and p.strip()]
    return " — ".join(parts) if parts else None


class MattermostConnector:
    """Connector for Mattermost using the REST API.

    Uses Personal Access Token for authentication.
    Retrieves messages from all accessible teams and channels.
    Supports sender caching for enriched user information.
    Supports per-channel state tracking for incremental sync.
    Supports file attachment processing.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        source_name: str = "mattermost",
        skip_channels: list = None,
        only_channels: list = None,
        db_callback: Callable = None,
        db=None,
        attachments_path: str = "data/attachments",
        text_extensions: set = None,
        youtrack_cfg: dict = None,
    ):
        """Initialize Mattermost connector.

        Args:
            base_url: Base URL of Mattermost server (e.g., https://mattermost.example.com)
            token: Personal Access Token from Account Settings → Security
            source_name: Logical source name stored in messages.source (e.g., "company_mattermost")
            skip_channels: List of channel names to skip
            only_channels: If set, only fetch from these channels (empty = all)
            db_callback: Callback to get saved state: fn(source, channel_id) -> int|None
            db: Database instance for updating state
            attachments_path: Directory where message attachments are stored on disk
            text_extensions: Set of file extensions to treat as text (e.g., {".txt", ".md"})
        """
        self.source_name = source_name
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        self._connected = False
        self._user_cache = {}
        self._skip_channels = set(skip_channels or [])
        self._only_channels = set(only_channels or [])
        self._db_callback = db_callback
        self._db = db
        self._attachments_path = attachments_path
        self._text_extensions = text_extensions or DEFAULT_TEXT_EXTENSIONS
        self._youtrack_cfg = youtrack_cfg or {}

        Path(attachments_path).mkdir(parents=True, exist_ok=True)

    def _get(self, path: str, params: dict = None) -> dict | list:
        """Make GET request to Mattermost API."""
        url = f"{self.base_url}/api/v4{path}"
        response = requests.get(url, headers=self.headers, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, data: dict = None, json: dict = None) -> dict | list:
        """Make POST request to Mattermost API."""
        url = f"{self.base_url}/api/v4{path}"
        response = requests.post(url, headers=self.headers, data=data, json=json, timeout=30)
        response.raise_for_status()
        return response.json()

    def _get_dm_channel_name(self, channel: dict) -> str:
        """Get friendly name for direct message channels.

        DM channels have type "D" and name format: userId1__userId2__userId3...
        Fetches usernames and returns "username1, username2, ..."

        Args:
            channel: Channel dictionary with 'name', 'type', 'display_name'

        Returns:
            Friendly channel name or original name if not a DM channel
        """
        # Only process DM channels
        if channel.get("type") != "D":
            return channel.get("display_name") or channel.get("name", "unknown")

        # If display_name exists, use it
        if channel.get("display_name"):
            return channel["display_name"]

        # Parse member IDs from channel name (format: userId1__userId2)
        member_ids = channel.get("name", "").split("__")
        if not member_ids or len(member_ids) < 2:
            return channel.get("name", "direct_message")

        # Fetch user info for all members (batch request)
        try:
            users = self._post("/users/ids", json=member_ids)
        except Exception as e:
            logger.warning(f"Failed to batch fetch users for DM channel: {e}")
            return channel.get("name", "direct_message")

        # Build friendly name from usernames
        usernames = []
        for user in users:
            username = user.get("username") or user.get("id", "?")
            usernames.append(username)

        return ", ".join(usernames)

    def _download_file(self, file_id: str) -> tuple[bytes, str]:
        """Download a file from Mattermost.

        Args:
            file_id: File ID in Mattermost

        Returns:
            Tuple of (raw content, extension from Content-Type)
        """
        url = f"{self.base_url}/api/v4/files/{file_id}"
        response = requests.get(url, headers=self.headers, timeout=60)
        response.raise_for_status()

        # Get extension from Content-Type header
        content_type = response.headers.get('Content-Type', '')
        extension = ''
        if content_type:
            import mimetypes
            # Parse MIME type (strip parameters like charset)
            mime_type = content_type.split(';')[0].strip()
            ext = mimetypes.guess_extension(mime_type)
            if ext:
                extension = ext

        return response.content, extension

    def _process_attachments(self, post: dict) -> tuple[str, list]:
        """Process file attachments from a post.

        Args:
            post: Post dictionary with file_ids and metadata

        Returns:
            Tuple of (modified_text, file_metadata_list)
        """
        file_ids = post.get("file_ids", [])
        files_metadata = post.get("metadata", {}).get("files", [])

        # Always start with the original message text (even if no files attached)
        modified_text = post.get("message", "")

        if not file_ids:
            # Return message text with empty file list if no attachments
            return modified_text, []

        # Create a map of file_id -> file metadata
        file_map = {}
        for f in files_metadata:
            file_map[f.get("id")] = f

        modified_text_parts = []
        file_metadata = []
        text_content_parts = []

        for file_id in file_ids:
            file_info = file_map.get(file_id, {})

            name = file_info.get("name", "unknown")
            size = file_info.get("size", 0)
            extension = file_info.get("extension", "").lower()
            full_extension = f".{extension}" if extension else ""

            # Add to metadata list
            metadata_entry = {
                "id": file_id,
                "name": name,
                "size": size,
                "extension": extension
            }
            file_metadata.append(metadata_entry)

            # Append reference to text
            modified_text_parts.append(f"attached_files: [{name}]({file_id})")

            # For text files, download and cache content
            if full_extension in self._text_extensions:
                cache_path = Path(self._attachments_path) / (file_id + full_extension)

                # Download full file if not cached
                if not cache_path.exists():
                    try:
                        content, file_ext = self._download_file(file_id)
                        # Save full file to cache with extension
                        with open(cache_path, 'wb') as f:
                            f.write(content)
                        logger.debug(f"Cached file {file_id}: {name} ({len(content)} bytes, ext={file_ext})")
                    except Exception as e:
                        logger.warning(f"Failed to download file {file_id}: {e}")
                        continue

                # Read and append first 5KB for message text
                try:
                    with open(cache_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read(MAX_TEXT_PREVIEW_SIZE)
                        text_content_parts.append(f"attached_files_content ({name}):\n{content}")
                except Exception as e:
                    logger.warning(f"Failed to read cached file {file_id}: {e}")

        # Build final text
        modified_text = post.get("message", "")

        if modified_text_parts:
            modified_text = f"{modified_text}\n" + "\n".join(modified_text_parts)

        if text_content_parts:
            modified_text = f"{modified_text}\n\n" + "\n\n".join(text_content_parts)

        return modified_text, file_metadata

    def connect(self) -> None:
        """Verify connection by fetching current user info."""
        try:
            me = self._get("/users/me")
            self._user_cache["me"] = me
            self._connected = True
            logger.info(f"[{self.source_name}] Connected as: {me.get('username', me.get('id'))}")
        except requests.RequestException as e:
            self._connected = False
            raise ConnectionError(f"Failed to connect to Mattermost: {e}")

    def disconnect(self) -> None:
        """Close connection."""
        self._connected = False
        self._user_cache.clear()

    def get_user_info(self, user_id: str) -> dict:
        """Get user info from cache or API."""
        if user_id in self._user_cache:
            return self._user_cache[user_id]

        try:
            user = self._get(f"/users/{user_id}")
            self._user_cache[user_id] = user
            return user
        except requests.RequestException as e:
            logger.warning(f"Failed to fetch user {user_id}: {e}")
            return {"id": user_id, "username": user_id, "first_name": "Unknown"}

    def get_sender_info(self, user_id: str) -> dict:
        """Get sender info for database caching."""
        user = self.get_user_info(user_id)
        return {
            "sender_id": user_id,
            "source": self.source_name,
            "username": user.get("username"),
            "full_name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or None,
            "email": user.get("email"),
            "phone": user.get("notify_props", {}).get("phone", ""),
            "avatar_url": user.get("avatar_url") or f"{self.base_url}/api/v4/users/{user_id}/image",
            "extra": {
                "position": user.get("position"),
                "locale": user.get("locale"),
            }
        }

    def get_attached_file_content(self, file_id: str, extension: str = None) -> Optional[str]:
        """Get content of an attached file from cache.

        Args:
            file_id: File ID in Mattermost
            extension: File extension (e.g., ".txt", ".png")

        Returns:
            File content as string, or None if not cached
        """
        if extension:
            cache_path = Path(self._attachments_path) / (file_id + extension)
        else:
            # Try to find file by checking common extensions
            for ext in self._text_extensions:
                cache_path = Path(self._attachments_path) / (file_id + ext)
                if cache_path.exists():
                    break
            else:
                cache_path = Path(self._attachments_path) / file_id

        if not cache_path.exists():
            # Download and save file
            try:
                content, file_ext = self._download_file(file_id)
                # Use extension from Content-Type header
                cache_path = Path(self._attachments_path) / (file_id + file_ext) if file_ext else cache_path
                with open(cache_path, 'wb') as f:
                    f.write(content)
                # Return as text if possible
                try:
                    return content.decode('utf-8', errors='ignore')
                except Exception:
                    return None
            except Exception as e:
                logger.warning(f"Failed to download file {file_id}: {e}")
                return None

        try:
            with open(cache_path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Failed to read cached file {file_id}: {e}")
            return None

    def _normalize_post(self, post: dict, channel: dict) -> dict:
        """Convert a raw Mattermost post into a normalized message dict."""
        sender_id = post["user_id"]
        sender = self.get_user_info(sender_id)
        sender_name = sender.get("username", sender_id)

        text, file_metadata = self._process_attachments(post)

        try:
            timestamp = datetime.fromtimestamp(
                post["create_at"] / 1000, tz=timezone.utc
            ).isoformat()
        except (KeyError, ValueError):
            timestamp = datetime.now(timezone.utc).isoformat()

        from pipeline.ingest import extract_urls  # lazy import to avoid circular
        return {
            "id": f"{self.source_name}:{post['id']}",
            "source": self.source_name,
            "channel_id": channel["id"],
            "sender": sender_name,
            "sender_id": sender_id,
            "timestamp": timestamp,
            "text": text,
            "thread_id": post.get("root_id") or None,
            "reply_to_id": post.get("parent_id") or None,
            "tags": [],
            "raw": {
                "team_id": channel.get("team_id", ""),
                "team_name": channel.get("team_name", ""),
                "channel_type": channel.get("type"),
                "post_id": post["id"],
                "update_at": post.get("update_at"),
                "files": file_metadata,
                "original_message": post.get("message"),
                "urls": extract_urls(text, self._youtrack_cfg),
            },
        }

    def _resolve_channel(self, channel: str) -> Optional[dict]:
        """Find a channel by id, name, or display_name across all teams.

        Returns the channel dict (with team_id/team_name set) or None if not found.
        """
        teams = self._get("/users/me/teams")
        for team in teams:
            channels = self._get(f"/users/me/teams/{team['id']}/channels")
            for ch in channels:
                if channel in (ch.get("id"), ch.get("name"), ch.get("display_name")):
                    ch["team_id"] = team["id"]
                    ch["team_name"] = team.get("name", "")
                    return ch
        return None

    def fetch_new(self, channel: str, limit: int = 50) -> list[dict]:
        """Fetch messages newer than the saved sync state for one channel (oldest→newest).

        Resolves the channel, reads its saved last_update_at via the read-only db_callback,
        and returns posts since then. Falls back to the newest `limit` posts when the channel
        has no saved state. Writes nothing (db must be None). Raises ValueError if the channel
        cannot be resolved.
        """
        resolved = self._resolve_channel(channel)
        if resolved is None:
            raise ValueError(f"Channel '{channel}' not found in source '{self.source_name}'")

        since_ms = self._db_callback(self.source_name, resolved["id"]) if self._db_callback else None
        current_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        if since_ms:
            messages, _ = self._fetch_channel_posts(resolved, since_ms, current_time_ms)
        else:
            data = self._get(
                f"/channels/{resolved['id']}/posts",
                params={"per_page": limit, "page": 0},
            )
            posts = data.get("posts", {})
            messages = [
                self._normalize_post(p, resolved)
                for pid in data.get("order", [])[:limit]
                if (p := posts.get(pid)) and p.get("message")
            ]

        # Live messages aren't read back through the DB join, so attach the channel name here.
        display = self._get_dm_channel_name(resolved)
        for m in messages:
            m["channel"] = display

        messages.sort(key=lambda m: m["timestamp"])
        return messages[-limit:]

    def _fetch_channel_posts(self, channel: dict, since_ms: int, current_time_ms: int) -> tuple[list[dict], int]:
        """Fetch all posts from a channel using iterative since-pagination.

        Returns:
            Tuple of (messages list, oldest_update_at)
        """
        messages = []
        batch_num = 0
        oldest_in_batch = current_time_ms
        seen_ids = set()

        while True:
            batch_num += 1
            params = {"since": since_ms}

            try:
                posts_data = self._get(f"/channels/{channel['id']}/posts", params=params)
            except requests.RequestException as e:
                logger.warning(f"[{self.source_name}/{channel.get('name')}] Failed to fetch posts: {e}")
                break

            posts = posts_data.get("posts", {})
            if not posts:
                logger.debug(f"[{channel.get('name')}] Batch {batch_num}: 0 messages - done")
                break

            oldest_update = oldest_in_batch
            batch_count = 0
            for post_id, post in posts.items():
                if not post.get("message"):
                    continue

                if post_id in seen_ids:
                    continue
                seen_ids.add(post_id)

                update_at = post.get("update_at", 0)
                if update_at > 0 and update_at < oldest_update:
                    oldest_update = update_at

                messages.append(self._normalize_post(post, channel))
                batch_count += 1

            dup_count = len([p for p in posts.values() if p.get("message")]) - batch_count
            if dup_count > 0:
                logger.debug(f"[{channel.get('name')}] Batch {batch_num}: "
                             f"{batch_count} messages ({dup_count} duplicates skipped), "
                             f"oldest: {oldest_update}")
            else:
                logger.debug(f"[{channel.get('name')}] Batch {batch_num}: "
                             f"{batch_count} messages, oldest: {oldest_update}")

            if oldest_update >= current_time_ms:
                break

            if oldest_update >= since_ms and oldest_update >= oldest_in_batch:
                break

            since_ms = oldest_update - 1
            oldest_in_batch = oldest_update

        return messages, oldest_in_batch

    def fetch_messages(
        self,
        since: datetime = None,
        force: bool = False,
        default_since_ms: int = None
    ) -> dict:
        """Fetch messages from all accessible channels.

        Args:
            since: Ignored if force=False and db_callback is set
            force: If True, ignore saved state and use default_since_ms
            default_since_ms: Default timestamp in ms for force mode or new channels

        Returns:
            Dict with messages, messages_count, channels_scanned, channels_skipped
        """
        messages = []
        channels_scanned = 0
        channels_skipped = 0
        current_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        logger.info(f"[{self.source_name}] Fetching messages (force={force})")

        teams = self._get("/users/me/teams")
        logger.info(f"[{self.source_name}] Found {len(teams)} teams")

        for team in teams:
            channels = self._get(f"/users/me/teams/{team['id']}/channels")
            logger.info(f"[{self.source_name}] Team '{team.get('name', 'unknown')}': {len(channels)} channels")

            for channel in channels:
                channel["team_id"] = team["id"]
                channel["team_name"] = team.get("name", "")

                # Get friendly name for DM channels
                channel_name = self._get_dm_channel_name(channel)
                channel_id = channel.get("id", "")

                # Check channel filters
                if self._skip_channels and (channel_name in self._skip_channels or channel_id in self._skip_channels):
                    logger.debug(f"Skipping channel (skip_list): {channel_name}")
                    channels_skipped += 1
                    continue

                if self._only_channels and channel_name not in self._only_channels and channel_id not in self._only_channels:
                    logger.debug(f"Skipping channel (only_list): {channel_name}")
                    channels_skipped += 1
                    continue

                # Determine start timestamp for this channel
                since_ms = None
                if not force and self._db_callback:
                    saved_state = self._db_callback(self.source_name, channel_id)
                    if saved_state:
                        since_ms = saved_state
                        logger.debug(f"[{channel_name}] Using saved state: {saved_state}")
                    else:
                        since_ms = default_since_ms
                        logger.debug(f"[{channel_name}] No saved state, using default: {default_since_ms}")
                else:
                    since_ms = default_since_ms
                    if force:
                        logger.debug(f"[{channel_name}] Force mode, using default: {default_since_ms}")

                if since_ms is None:
                    since_ms = default_since_ms

                channel_messages, oldest_update = self._fetch_channel_posts(
                    channel, since_ms, current_time_ms
                )

                if channel_messages:
                    logger.info(f"[{self.source_name}/{channel_name}] {len(channel_messages)} messages")
                    messages.extend(channel_messages)
                    channels_scanned += 1
                else:
                    channels_skipped += 1

                # Always upsert channel metadata and sync state
                if self._db:
                    from pipeline.ingest import parse_channel_issue_ids  # lazy
                    issue_ids = parse_channel_issue_ids(
                        channel_name, self._youtrack_cfg.get("project_prefixes"))
                    try:
                        self._db.upsert_channel({
                            "id": channel_id,
                            "source": self.source_name,
                            "name": channel.get("name"),
                            "display_name": channel_name,
                            "description": _combine_purpose_header(
                                channel.get("purpose"), channel.get("header")),
                            "team_id": channel.get("team_id", ""),
                            "team_name": channel.get("team_name", ""),
                            "channel_type": channel.get("type"),
                            "extra": {"issue_ids": issue_ids} if issue_ids else None,
                            "last_update_at": current_time_ms,
                        })
                        logger.debug(f"[{channel_name}] Upserted channel state: {current_time_ms}")
                    except Exception as e:
                        logger.warning(f"[{self.source_name}/{channel_name}] Failed to upsert channel: {e}")

        logger.info(f"[{self.source_name}] Total: {len(messages)} messages fetched")
        return {
            "messages": messages,
            "messages_count": len(messages),
            "channels_scanned": channels_scanned,
            "channels_skipped": channels_skipped
        }

    def send_message(self, channel_id: str, text: str, root_id: str = None) -> str:
        """Post a message to a channel via POST /api/v4/posts. Returns the new post id.

        Pass root_id to reply inside a thread. Surfaces a clear error on 429 (Mattermost
        allows ~5 posts / 30s) instead of failing silently.
        """
        payload = {"channel_id": channel_id, "message": text}
        if root_id:
            payload["root_id"] = root_id
        try:
            post = self._post("/posts", json=payload)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                raise RuntimeError(
                    "Mattermost rate limit hit (~5 posts / 30s); wait a moment and retry."
                )
            raise
        return post.get("id", "")

    def get_sender_fetch_callback(self):
        """Return a callback function for caching sender info."""
        return self.get_sender_info
