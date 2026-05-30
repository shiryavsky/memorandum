"""Shared fixtures and mocks for the test suite."""
import sys
from unittest.mock import MagicMock
import pytest

# Mock VectorStore before any test module imports it.
# ChromaDB + FlagEmbedding/BGE-M3 are too heavy for unit tests.
sys.modules["storage.vector_store"] = MagicMock()

from storage.db import Database  # noqa: E402 — must come after sys.modules patch


@pytest.fixture
def tmp_db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    yield db
    db.close()


@pytest.fixture
def minimal_config(tmp_path):
    return {
        "sqlite_path": str(tmp_path / "db.sqlite"),
        "chroma_path": str(tmp_path / "chroma"),
        "sources": {
            "test_mm": {
                "type": "mattermost",
                "url": "https://mm.example.com",
                "token": "test_token",
                "enabled": True,
            }
        },
    }


@pytest.fixture
def sample_message():
    return {
        "id": "test_mm:post123",
        "source": "test_mm",
        "channel_id": "ch001",
        "sender": "alice",
        "sender_id": "user001",
        "timestamp": "2024-01-15T10:30:00+00:00",
        "text": "Hello world",
        "thread_id": None,
        "reply_to_id": None,
        "tags": [],
        "raw": {"post_id": "post123", "team_name": "myteam"},
    }
