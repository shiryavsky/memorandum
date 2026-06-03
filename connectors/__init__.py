"""Source connectors for message collection."""

from ._common import ConnectorProtocol
from .email_connector import EmailConnector
from .mattermost_connector import MattermostConnector
from .pachca_connector import PachcaConnector
from .telegram_connector import TelegramConnector

__all__ = [
    "ConnectorProtocol",
    "EmailConnector",
    "MattermostConnector",
    "PachcaConnector",
    "TelegramConnector",
]
