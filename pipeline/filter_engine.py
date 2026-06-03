"""Filter engine for message filtering based on YAML rules."""
import re


class FilterEngine:
    """Per-source message filter engine using YAML rules.

    Rules are applied in order:
    1. Skip specific senders (by ID or name)
    2. Skip specific channels (by name or ID)
    3. Apply only_channels allowlist (if non-empty)
    4. Skip messages matching skip_patterns regex
    """

    def __init__(self, config: dict):
        """Initialize filter engine with a source's filters dict."""
        self.skip_senders = set(config.get("skip_senders", []))
        self.skip_channels = set(config.get("skip_channels", []))
        self.only_channels = set(config.get("only_channels", []))
        self.skip_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in config.get("skip_patterns", [])
        ]
        # Email-specific filters — silently inert for chat sources whose msgs
        # carry neither a folder nor a subject.
        self.skip_folders = set(config.get("skip_folders", []))
        self.skip_subjects = [
            re.compile(p, re.IGNORECASE)
            for p in config.get("skip_subjects", [])
        ]

    def should_keep(self, msg: dict) -> bool:
        """Return True if the message should be kept."""
        # 1. Skip by sender — match the sender_id, the display name, AND (for
        # email) the From address surfaced as raw.from / sender_email.
        sender_id = msg.get("sender_id", "")
        sender_name = msg.get("sender", "")
        sender_email = msg.get("sender_email") or ""
        raw = msg.get("raw") or {}
        if isinstance(raw, dict):
            sender_email = sender_email or (raw.get("from") or "")
        if (sender_id in self.skip_senders
                or sender_name in self.skip_senders
                or (sender_email and sender_email in self.skip_senders)):
            return False

        # 2. Skip by channel id. Connectors emit only `channel_id` on the
        # message dict; the human-friendly display_name lives on the channels
        # row, not the message, so name-based skip/only matching happens at
        # the connector level (skip_channels passed to the constructor) — not
        # here. Filter rules that list display names still work because each
        # connector translates names to ids when applying its own skip list.
        channel_id = msg.get("channel_id", "")
        if channel_id in self.skip_channels:
            return False

        # 2b. Skip by email folder (raw.folder or channel_id "folder:...").
        if self.skip_folders:
            folder = ""
            if isinstance(raw, dict):
                folder = raw.get("folder") or ""
            if not folder and channel_id.startswith("folder:"):
                folder = channel_id[len("folder:"):]
            if folder and folder in self.skip_folders:
                return False

        # 3. Apply only_channels allowlist (if non-empty)
        if self.only_channels and channel_id not in self.only_channels:
            return False

        # 4. Skip by regex patterns
        text = msg.get("text", "")
        for pattern in self.skip_patterns:
            if pattern.search(text):
                return False

        # 5. Email subject regex (raw.subject)
        if self.skip_subjects and isinstance(raw, dict):
            subject = raw.get("subject") or ""
            if subject:
                for pattern in self.skip_subjects:
                    if pattern.search(subject):
                        return False

        return True

    def filter_messages(self, messages: list[dict]) -> list[dict]:
        """Filter a list of messages.

        Args:
            messages: List of message dictionaries

        Returns:
            Filtered list of messages
        """
        return [msg for msg in messages if self.should_keep(msg)]
