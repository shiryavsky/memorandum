"""Shared constants and the connector protocol.

Connector modules used to redefine these constants at the top of each file
(with mattermost/telegram drifting on ``DEFAULT_TEXT_EXTENSIONS`` and all
four agreeing on ``MAX_TEXT_PREVIEW_SIZE``). One source of truth keeps
inline text previews and "is this attachment textual?" decisions
consistent.

``ConnectorProtocol`` is a structural typing aid — it doesn't enforce
inheritance, but documents the contract every connector implements so
``connectors.factory.build_connector`` has a real return type and
new-connector authors can see the surface they need to cover. The four
shipped connectors don't subclass anything; they satisfy the protocol
by shape.
"""
from typing import Optional, Protocol, runtime_checkable


# Inline first-N-bytes preview of a text attachment when the connector
# decides to include attachment content in the message body. 5KB is enough
# for any reasonable log snippet without bloating the embedded text.
MAX_TEXT_PREVIEW_SIZE = 5 * 1024


# File extensions whose content is safe to read as UTF-8 and inline into
# the message text. Anything else stays a `[file_id=…]` reference and is
# served on-demand via `get_attached_file`.
DEFAULT_TEXT_EXTENSIONS = frozenset({
    ".txt", ".log",
    ".md", ".markdown",
    ".json", ".xml", ".yaml", ".yml",
    ".csv", ".lst",
})


@runtime_checkable
class ConnectorProtocol(Protocol):
    """The shape every source connector implements.

    Structural, not nominal — connectors don't subclass this. The protocol
    exists so ``factory.build_connector``'s return type is meaningful and
    so a new-connector author has one place to read what they need to
    provide. ``runtime_checkable`` lets ``isinstance(x, ConnectorProtocol)``
    do the duck-typing check if a test ever wants it.

    Notes on the contract:
    - ``fetch_new`` may raise ``NotImplementedError`` when live polling
      isn't a fit for the source — Email does this because IMAP polling
      is too heavy for an interactive tool call. The MCP server's
      ``get_new_messages`` handler catches that and punts with a clear
      message. Returning an empty list is *not* the same thing; it
      means "no new messages right now."
    - ``message_url`` returns ``None`` when no permalink exists
      (Telegram private chats, email drafts, channels the token can't
      resolve). Callers treat ``None`` as "send succeeded but no link to
      include."
    """

    source_name: str

    def connect(self) -> None:
        """Open the underlying transport (HTTP session, IMAP login, …)."""
        ...

    def disconnect(self) -> None:
        """Close transport state. Idempotent."""
        ...

    def fetch_messages(self, since, force: bool = False,
                       default_since_ms: Optional[int] = None) -> dict:
        """Batch-fetch new messages across all channels of this source.

        Returns ``{messages, messages_count, channels_scanned,
        channels_skipped, kept, dropped, fetch_ms}`` — same shape every
        connector emits and ``pipeline.ingest._fetch_one`` consumes.
        """
        ...

    def fetch_new(self, channel: str, limit: int = 50) -> list[dict]:
        """Live-read the gap on one channel for ``get_new_messages``.

        May raise ``NotImplementedError`` (Email does) when live polling
        is not a supported mode for this source.
        """
        ...

    def send_message(self, channel, text: str, reply_to=None):
        """Send a message; return the new message id.

        ``reply_to`` is the parent message id (Mattermost root post id,
        Telegram message id, Pachca parent message id, email Message-ID).
        Each connector accepts the value it needs and coerces internally.
        Platform-specific extras (Telegram business_connection_id, email
        recipient derivation) are resolved inside the connector.
        """
        ...

    def message_url(self, channel, message_id) -> Optional[str]:
        """Build a permalink for a just-sent message, or None if not applicable."""
        ...

    def get_sender_info(self, sender_id: str) -> dict:
        """Hydrate sender metadata for caching in ``senders``. Best-effort."""
        ...
