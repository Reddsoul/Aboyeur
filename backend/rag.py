"""ChromaDB semantic search over the recipe corpus.

Single source of truth for the Chroma collection — imported by both the backend
(search) and the ingest pipeline (upsert). Same embedding function on both sides.
"""
import os
from typing import Optional

# chromadb is imported lazily: the live app (aboyeur.agent over FTS5) never
# touches it, and the package pulls in onnxruntime + a model download. It's
# no longer a declared dependency — every entry point below raises a clear
# ImportError if something still reaches for it.
CHROMA_HOST = os.getenv("CHROMA_HOST", "chromadb")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))
CHROMA_COLLECTION = os.getenv("CHROMA_COLLECTION", "recipes")

_EMBED_FN = None
_client = None
_collection = None


def _embed_fn():
    """Local ONNX MiniLM (offline, no API). Built on first use."""
    global _EMBED_FN
    if _EMBED_FN is None:
        from chromadb.utils import embedding_functions
        _EMBED_FN = embedding_functions.DefaultEmbeddingFunction()
    return _EMBED_FN


def get_client():
    global _client
    if _client is None:
        import chromadb
        _client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    return _client


def wait_ready(timeout: float = 90.0, interval: float = 2.0) -> bool:
    """Block until ChromaDB answers a heartbeat (or timeout). Returns True if ready."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            get_client().heartbeat()
            return True
        except Exception:
            time.sleep(interval)
    return False


def get_collection():
    """get-or-create the recipes collection (cosine space)."""
    global _collection
    if _collection is None:
        _collection = get_client().get_or_create_collection(
            name=CHROMA_COLLECTION,
            embedding_function=_embed_fn(),
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def _flatten_meta(meta: dict) -> dict:
    """Chroma metadata values must be scalars. Lists -> comma strings."""
    out = {}
    for k, v in meta.items():
        if isinstance(v, (list, tuple)):
            out[k] = ", ".join(str(x) for x in v)
        elif isinstance(v, dict):
            continue
        elif v is None:
            out[k] = ""
        else:
            out[k] = v
    return out


def upsert_recipes(ids: list[str], documents: list[str], metadatas: list[dict]):
    """Upsert a batch of recipe chunks. ids must be globally unique ({book}::{slug})."""
    if not ids:
        return
    col = get_collection()
    col.upsert(
        ids=ids,
        documents=documents,
        metadatas=[_flatten_meta(m) for m in metadatas],
    )


def search(query: str, n: int = 10, filters: Optional[dict] = None) -> list[dict]:
    """Semantic search. `filters` is a Chroma `where` clause (metadata equality).

    Returns: [{id, document, metadata, distance, score}], best first.
    """
    col = get_collection()
    kwargs = {"query_texts": [query], "n_results": max(1, n)}
    if filters:
        kwargs["where"] = filters
    res = col.query(**kwargs)
    out = []
    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    for i, _id in enumerate(ids):
        dist = dists[i] if i < len(dists) else 1.0
        out.append({
            "id": _id,
            "document": docs[i] if i < len(docs) else "",
            "metadata": metas[i] if i < len(metas) else {},
            "distance": dist,
            "score": round(1.0 - float(dist), 4),  # cosine distance -> similarity
        })
    return out


def get_by_id(recipe_id: str) -> Optional[dict]:
    col = get_collection()
    res = col.get(ids=[recipe_id])
    ids = res.get("ids") or []
    if not ids:
        return None
    return {
        "id": ids[0],
        "document": (res.get("documents") or [""])[0],
        "metadata": (res.get("metadatas") or [{}])[0],
    }


def delete_book(book_id: str):
    """Remove all chunks belonging to a book."""
    get_collection().delete(where={"book_id": book_id})


def delete_ids(ids: list[str]):
    if ids:
        get_collection().delete(ids=ids)


def count() -> int:
    try:
        return get_collection().count()
    except Exception:
        return 0


def reset_collection():
    """Drop and recreate the collection (used by reindex --full-rebuild)."""
    global _collection
    try:
        get_client().delete_collection(CHROMA_COLLECTION)
    except Exception:
        pass
    _collection = None
    return get_collection()
