"""Shared constants for all source connectors.

Connector modules used to redefine these at the top of each file (with
mattermost/telegram drifting on `DEFAULT_TEXT_EXTENSIONS` and all four
agreeing on `MAX_TEXT_PREVIEW_SIZE`). One source of truth keeps inline
text previews and "is this attachment textual?" decisions consistent.
"""


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
