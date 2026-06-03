"""Each shipped connector must satisfy `ConnectorProtocol` by shape.

The protocol is `@runtime_checkable`, so `isinstance(cls(...), Protocol)`
walks the method set. We instantiate each connector with the minimal
arguments needed and assert the duck-typing check passes — catches a
missing or renamed method before it lands.
"""
import pytest

from connectors import (
    ConnectorProtocol,
    EmailConnector,
    MattermostConnector,
    PachcaConnector,
    TelegramConnector,
)


def _instance(cls, **kwargs):
    """Build a connector with whatever each one needs to instantiate cheaply."""
    if cls is MattermostConnector:
        return cls(base_url="https://x", token="t", **kwargs)
    if cls is TelegramConnector:
        return cls(bot_token="123:abc", **kwargs)
    if cls is PachcaConnector:
        return cls(access_token="t", **kwargs)
    if cls is EmailConnector:
        return cls(host="h", username="u", password="p", **kwargs)
    raise AssertionError(f"unknown connector class: {cls}")


@pytest.mark.parametrize("cls", [
    MattermostConnector,
    TelegramConnector,
    PachcaConnector,
    EmailConnector,
])
def test_connector_satisfies_protocol(cls):
    inst = _instance(cls)
    assert isinstance(inst, ConnectorProtocol)
