"""ChromaDB vector store for semantic search."""
import chromadb
from pathlib import Path
from typing import Optional


# Defaults match the original hardcoded values so omitting `embedding:` in
# config preserves today's behavior exactly.
DEFAULT_EMBEDDING = {
    "model": "BAAI/bge-m3",
    "device": "cpu",
    "use_fp16": True,
    "max_length": 512,
    "batch_size": 1,
    "collection_name": "messages",
}


def _resolve_embedding(embedding: Optional[dict]) -> dict:
    """Merge user-provided embedding config over the defaults."""
    out = dict(DEFAULT_EMBEDDING)
    if embedding:
        for k, v in embedding.items():
            if v is not None:
                out[k] = v
    return out


class VectorStore:
    """ChromaDB vector store for semantic message search.

    Embedding model and load flags are config-driven (`embedding:` block); the
    defaults reproduce the original BGE-M3 / fp16 / cpu setup.
    """

    def __init__(self, path: str, embedding: Optional[dict] = None):
        """Initialize ChromaDB client, collection, and the embedding model.

        Args:
            path: Directory path for ChromaDB persistence.
            embedding: Optional dict — keys: model, device, use_fp16, max_length,
                batch_size, collection_name. Missing keys fall back to DEFAULT_EMBEDDING.
        """
        from FlagEmbedding import BGEM3FlagModel

        cfg = _resolve_embedding(embedding)
        Path(path).mkdir(parents=True, exist_ok=True)
        self.path = path
        self.embedding = cfg
        self.collection_name = cfg["collection_name"]
        self.max_length = int(cfg["max_length"])
        self.batch_size = int(cfg["batch_size"])

        self.client = chromadb.PersistentClient(path=path)
        self.model = BGEM3FlagModel(
            cfg["model"],
            use_fp16=bool(cfg["use_fp16"]),
            device=cfg["device"],
        )
        self.collection = self.client.get_or_create_collection(
            name=self.collection_name,
            embedding_function=None,  # we encode manually
        )
        self._dim_checked = False

    def _check_dim(self, model_dim: int) -> None:
        """One-shot guard: if the existing collection's vectors don't match the
        configured model's output dimension, raise a clear error pointing at the
        swap recipe. Cheap because it only peeks at one row, and only once."""
        if self._dim_checked:
            return
        self._dim_checked = True
        try:
            if self.collection.count() == 0:
                return
            sample = self.collection.peek(1)
        except Exception:
            return
        # Chroma returns the embeddings column as a numpy array — `arr or []`
        # and `if not arr` trigger numpy's ambiguous-truth-value error, so
        # use explicit length / None checks instead.
        embs = (sample or {}).get("embeddings")
        if embs is None or len(embs) == 0:
            return
        first = embs[0]
        if first is None or len(first) == 0:
            return
        existing_dim = len(first)
        if existing_dim != model_dim:
            raise RuntimeError(
                f"Embedding dimension mismatch: collection '{self.collection_name}' "
                f"at '{self.path}' holds {existing_dim}-dim vectors, but the "
                f"configured model '{self.embedding['model']}' produces "
                f"{model_dim}-dim. Either set embedding.collection_name to a fresh "
                f"name in config, or delete the chroma path and re-ingest. "
                f"See README → 'Swapping the embedding model'."
            )

    def insert(self, msg: dict) -> bool:
        """Insert a message into the vector store.

        Returns True if inserted, False if skipped (empty text).
        """
        text = msg.get("text", "").strip()
        if not text:
            return False

        metadata = {
            "source": msg.get("source", ""),
            "channel": msg.get("channel") or msg.get("channel_id") or "",
            "sender": msg.get("sender") or "",
            "timestamp": msg.get("timestamp") or "",
        }

        embeddings = self.model.encode(
            [text],
            max_length=self.max_length,
            batch_size=self.batch_size,
        )["dense_vecs"]
        vector = embeddings[0].tolist()
        self._check_dim(len(vector))

        self.collection.upsert(
            ids=[msg["id"]],
            documents=[text],
            metadatas=[metadata],
            embeddings=[vector],
        )
        return True

    def semantic_search(
        self,
        query: str,
        n_results: int = 10,
        source: Optional[str] = None,
        channel: Optional[str] = None,
        sender: Optional[str] = None,
        since: Optional[str] = None,
    ) -> list[dict]:
        """Perform semantic search on message embeddings."""
        where_clause = {}
        if source:
            where_clause["source"] = source
        if channel:
            where_clause["channel"] = channel
        if sender:
            where_clause["sender"] = sender
        if since:
            where_clause["timestamp"] = {"$gte": since}

        query_embeddings = self.model.encode(
            [query],
            max_length=self.max_length,
            batch_size=self.batch_size,
        )["dense_vecs"]

        try:
            results = self.collection.query(
                query_embeddings=[query_embeddings[0].tolist()],
                n_results=n_results,
                where=where_clause if where_clause else None,
            )
        except Exception:
            return []

        if not results or not results.get("ids"):
            return []

        hits = []
        for i, doc_id in enumerate(results["ids"][0]):
            doc = results["documents"][0][i] if results["documents"] else ""
            metadata = results["metadatas"][0][i] if results["metadatas"] else {}
            distance = results["distances"][0][i] if results["distances"] else 0.0
            hits.append({
                "id": doc_id,
                "text": doc,
                "metadata": metadata,
                "distance": distance,
                "similarity": 1.0 - distance,
            })
        return hits

    def count(self) -> int:
        """Total embedded messages in this collection."""
        return self.collection.count()

    def delete(self, msg_id: str) -> bool:
        try:
            self.collection.delete(ids=[msg_id])
            return True
        except Exception:
            return False

    def delete_many(self, ids: list[str], chunk_size: int = 5000) -> int:
        """Bulk-delete by id list. Chunked so a horizon-crossing prune doesn't
        push a 100k-id payload through chroma in one call. Per-chunk failures
        are logged via the chroma client's own error path but never raised —
        the next prune will retry. Returns how many ids were attempted."""
        if not ids:
            return 0
        attempted = 0
        for i in range(0, len(ids), max(1, chunk_size)):
            batch = ids[i:i + chunk_size]
            try:
                self.collection.delete(ids=batch)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    f"vector_store.delete_many: chunk of {len(batch)} ids failed: {e}"
                )
            attempted += len(batch)
        return attempted

    def clear(self) -> None:
        self.collection.delete(where={})
