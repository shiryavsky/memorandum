"""Tests for storage/vector_store.py — config-driven embedding load + tuning.

The conftest.py replaces `storage.vector_store` with a MagicMock so other tests
never load BGE-M3. This file restores the real module locally and stubs out
FlagEmbedding + chromadb so the model still never loads.
"""
import sys
from unittest.mock import MagicMock, patch
import numpy as np

# Drop conftest's whole-module mock and stub FlagEmbedding before re-import.
sys.modules.pop("storage.vector_store", None)
sys.modules["FlagEmbedding"] = MagicMock()

import storage.vector_store as vs_module  # noqa: E402
from storage.vector_store import (  # noqa: E402
    VectorStore,
    DEFAULT_EMBEDDING,
    _resolve_embedding,
)


# ── _resolve_embedding ────────────────────────────────────────────────────────

def test_resolve_embedding_none_returns_defaults_copy():
    out = _resolve_embedding(None)
    assert out == DEFAULT_EMBEDDING
    assert out is not DEFAULT_EMBEDDING  # must be a copy
    out["model"] = "tampered"
    assert DEFAULT_EMBEDDING["model"] == "BAAI/bge-m3"


def test_resolve_embedding_overrides_subset_of_keys():
    out = _resolve_embedding({"model": "BAAI/bge-small-en-v1.5", "device": "cuda"})
    assert out["model"] == "BAAI/bge-small-en-v1.5"
    assert out["device"] == "cuda"
    # Untouched fields fall back to defaults.
    assert out["use_fp16"] is True
    assert out["max_length"] == 512
    assert out["batch_size"] == 1
    assert out["collection_name"] == "messages"


def test_resolve_embedding_ignores_none_values_in_user_dict():
    out = _resolve_embedding({"device": None, "model": "x"})
    assert out["device"] == "cpu"  # None did not override the default
    assert out["model"] == "x"


# ── __init__: model + collection wiring ──────────────────────────────────────

def _patch_init(tmp_path):
    """Return (PersistentClient mock, BGEM3 mock, collection mock) and patches.

    Use as:
        with _make_store_ctx(tmp_path) as (pc, model_cls, col):
            store = VectorStore(...)
    """
    return tmp_path


def _make(tmp_path, embedding=None):
    fake_collection = MagicMock()
    fake_collection.count.return_value = 0
    fake_client = MagicMock()
    fake_client.get_or_create_collection.return_value = fake_collection
    fake_model = MagicMock()
    fake_model.encode.return_value = {
        "dense_vecs": np.array([np.zeros(8, dtype=np.float32)])
    }
    fake_model_cls = MagicMock(return_value=fake_model)
    with patch.object(vs_module, "chromadb") as mod_chromadb, \
         patch("FlagEmbedding.BGEM3FlagModel", fake_model_cls):
        mod_chromadb.PersistentClient.return_value = fake_client
        store = VectorStore(str(tmp_path / "chroma"), embedding=embedding)
    return store, fake_client, fake_collection, fake_model, fake_model_cls


def test_init_defaults_load_bge_m3_with_fp16_cpu(tmp_path):
    _, _, _, _, model_cls = _make(tmp_path)
    model_cls.assert_called_once_with("BAAI/bge-m3", use_fp16=True, device="cpu")


def test_init_custom_model_and_device(tmp_path):
    _, _, _, _, model_cls = _make(tmp_path, embedding={
        "model": "BAAI/bge-small-en-v1.5", "device": "cuda", "use_fp16": False,
    })
    model_cls.assert_called_once_with(
        "BAAI/bge-small-en-v1.5", use_fp16=False, device="cuda"
    )


def test_init_uses_configured_collection_name(tmp_path):
    store, client, _, _, _ = _make(tmp_path, embedding={"collection_name": "alt"})
    assert store.collection_name == "alt"
    client.get_or_create_collection.assert_called_once_with(
        name="alt", embedding_function=None
    )


def test_init_default_collection_name_is_messages(tmp_path):
    store, client, _, _, _ = _make(tmp_path)
    assert store.collection_name == "messages"
    client.get_or_create_collection.assert_called_once_with(
        name="messages", embedding_function=None
    )


def test_init_stores_max_length_and_batch_size(tmp_path):
    store, _, _, _, _ = _make(tmp_path, embedding={"max_length": 256, "batch_size": 8})
    assert store.max_length == 256
    assert store.batch_size == 8


# ── insert / semantic_search use configured max_length + batch_size ──────────

def test_insert_calls_encode_with_configured_tuning(tmp_path):
    store, _, _, model, _ = _make(tmp_path, embedding={"max_length": 256, "batch_size": 4})
    store.insert({"id": "m1", "text": "hello world",
                  "source": "s", "channel": "c", "sender": "a", "timestamp": "t"})
    model.encode.assert_called_once_with(["hello world"], max_length=256, batch_size=4)


def test_insert_skips_empty_text(tmp_path):
    store, _, col, model, _ = _make(tmp_path)
    assert store.insert({"id": "m1", "text": "   "}) is False
    model.encode.assert_not_called()
    col.upsert.assert_not_called()


def test_insert_upserts_with_vector_metadata_and_id(tmp_path):
    store, _, col, _, _ = _make(tmp_path)
    store.insert({"id": "m1", "text": "hi", "source": "s",
                  "channel": "c", "sender": "a", "timestamp": "t"})
    call = col.upsert.call_args[1]
    assert call["ids"] == ["m1"]
    assert call["documents"] == ["hi"]
    assert call["metadatas"][0]["source"] == "s"
    assert call["metadatas"][0]["sender"] == "a"
    assert len(call["embeddings"][0]) == 8  # the fake model's dim


def test_semantic_search_calls_encode_with_configured_tuning(tmp_path):
    store, _, col, model, _ = _make(tmp_path, embedding={"max_length": 128, "batch_size": 2})
    col.query.return_value = {"ids": [[]], "documents": [[]],
                              "metadatas": [[]], "distances": [[]]}
    store.semantic_search("query text")
    model.encode.assert_called_once_with(["query text"], max_length=128, batch_size=2)


def test_semantic_search_returns_empty_on_collection_error(tmp_path):
    store, _, col, _, _ = _make(tmp_path)
    col.query.side_effect = RuntimeError("boom")
    assert store.semantic_search("q") == []


def test_semantic_search_builds_where_clause(tmp_path):
    store, _, col, _, _ = _make(tmp_path)
    col.query.return_value = {"ids": [[]], "documents": [[]],
                              "metadatas": [[]], "distances": [[]]}
    store.semantic_search("q", source="mm", channel="dev", sender="alice",
                          since="2026-01-01T00:00:00")
    where = col.query.call_args[1]["where"]
    assert where == {
        "source": "mm", "channel": "dev", "sender": "alice",
        "timestamp": {"$gte": "2026-01-01T00:00:00"},
    }


# ── dimensionality guard ──────────────────────────────────────────────────────

def test_check_dim_silent_when_collection_empty(tmp_path):
    store, _, col, _, _ = _make(tmp_path)
    col.count.return_value = 0
    store.insert({"id": "m1", "text": "hi"})  # would raise if guard tripped


def test_check_dim_silent_when_dims_match(tmp_path):
    store, _, col, _, _ = _make(tmp_path)
    col.count.return_value = 1
    col.peek.return_value = {"embeddings": [[0.0] * 8]}  # matches fake model dim 8
    store.insert({"id": "m1", "text": "hi"})  # no exception


def test_check_dim_raises_when_existing_dim_differs(tmp_path):
    store, _, col, _, _ = _make(tmp_path)
    col.count.return_value = 1
    col.peek.return_value = {"embeddings": [[0.0] * 1024]}  # old collection 1024-dim
    import pytest
    with pytest.raises(RuntimeError, match="dimension mismatch"):
        store.insert({"id": "m1", "text": "hi"})


def test_check_dim_runs_once(tmp_path):
    store, _, col, _, _ = _make(tmp_path)
    col.count.return_value = 1
    col.peek.return_value = {"embeddings": [[0.0] * 8]}
    store.insert({"id": "m1", "text": "hi"})
    store.insert({"id": "m2", "text": "hello"})
    # peek only called the first time
    assert col.peek.call_count == 1


def test_check_dim_handles_numpy_array_peek_result(tmp_path):
    """Regression: chroma returns the embeddings column as a numpy array.
    `arr or []` and `if not arr` trip its ambiguous-truth-value guard. The
    check must use explicit length / None tests instead."""
    store, _, col, _, _ = _make(tmp_path)
    col.count.return_value = 1
    # Real chroma payload shape — a 2-D numpy array of stored vectors.
    col.peek.return_value = {"embeddings": np.zeros((1, 8), dtype=np.float32)}
    store.insert({"id": "m1", "text": "hi"})  # must not raise


def test_check_dim_numpy_array_mismatch_still_raises(tmp_path):
    """The numpy-friendly path must still surface a real dim mismatch."""
    store, _, col, _, _ = _make(tmp_path)
    col.count.return_value = 1
    col.peek.return_value = {"embeddings": np.zeros((1, 1024), dtype=np.float32)}
    import pytest
    with pytest.raises(RuntimeError, match="dimension mismatch"):
        store.insert({"id": "m1", "text": "hi"})


# ── delete_many (bulk variant) ──────────────────────────────────────

def test_delete_many_empty_returns_zero(tmp_path):
    store, _, col, _, _ = _make(tmp_path)
    assert store.delete_many([]) == 0
    col.delete.assert_not_called()


def test_delete_many_passes_ids_to_collection(tmp_path):
    store, _, col, _, _ = _make(tmp_path)
    n = store.delete_many(["a", "b", "c"])
    assert n == 3
    col.delete.assert_called_once_with(ids=["a", "b", "c"])


def test_delete_many_chunks_by_chunk_size(tmp_path):
    store, _, col, _, _ = _make(tmp_path)
    ids = [f"m{i}" for i in range(12)]
    n = store.delete_many(ids, chunk_size=5)
    assert n == 12
    # 12 / 5 = 3 batches (5, 5, 2)
    assert col.delete.call_count == 3
    batches = [c.kwargs["ids"] for c in col.delete.call_args_list]
    assert batches == [ids[0:5], ids[5:10], ids[10:12]]


def test_delete_many_per_chunk_failure_does_not_raise(tmp_path):
    store, _, col, _, _ = _make(tmp_path)
    col.delete.side_effect = [None, RuntimeError("upstream 500"), None]
    n = store.delete_many(["a", "b", "c"], chunk_size=1)
    # All three were attempted; the failing one is logged + counted, not raised.
    assert n == 3
    assert col.delete.call_count == 3
