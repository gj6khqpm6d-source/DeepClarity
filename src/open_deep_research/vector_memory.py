"""Runtime vector memory for the research agent.

When the agent reads web content (search results / fetched pages), embed it into a
per-thread in-memory index so later rounds can semantically re-consult what was
already read ("recall") instead of only having lossy summaries in context.

This is an ENHANCEMENT layer: every entry point degrades to a no-op if the
embedding backend is unavailable, so the graph behaves exactly as before.
"""

import asyncio
import logging
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)

_embedding_model = None
_embedding_attempted = False
_stores: dict[str, "ThreadMemory"] = {}

_CHUNK_SIZE = 1800
_CHUNK_OVERLAP = 200
_DEFAULT_TOP_K = 5
_MODEL_NAME = "BAAI/bge-small-zh-v1.5"


def _get_embedding_model():
    """Lazily load the embedding model once; cache success/failure."""
    global _embedding_model, _embedding_attempted
    if _embedding_attempted:
        return _embedding_model
    _embedding_attempted = True
    try:
        from fastembed import TextEmbedding
        _embedding_model = TextEmbedding(_MODEL_NAME)
        logger.info("Vector memory: loaded embedding model %s", _MODEL_NAME)
    except Exception as e:
        logger.warning("Vector memory disabled (embedding model failed to load): %s", e)
    return _embedding_model


def _chunk_text(text: str) -> list:
    """Split long text into overlapping chunks; keep short text whole."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= _CHUNK_SIZE:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start + _CHUNK_SIZE])
        start += _CHUNK_SIZE - _CHUNK_OVERLAP
    return chunks


class ThreadMemory:
    """In-memory vector index for one research thread."""

    def __init__(self):
        self._chunks = []
        self._embeddings = None
        self._lock = asyncio.Lock()

    async def add(self, text, url="", title=""):
        """Embed content into this thread's memory (best-effort)."""
        model = _get_embedding_model()
        if model is None:
            return
        chunks = _chunk_text(text)
        if not chunks:
            return
        try:
            vectors = list(model.embed(chunks))
        except Exception as e:
            logger.warning("Vector memory: embedding failed, skipping: %s", e)
            return
        async with self._lock:
            for chunk, vector in zip(chunks, vectors):
                vec = np.asarray(vector, dtype="float32").reshape(1, -1)
                self._chunks.append({"text": chunk, "url": url, "title": title})
                self._embeddings = (
                    vec if self._embeddings is None
                    else np.vstack([self._embeddings, vec])
                )

    async def search(self, query, top_k=_DEFAULT_TOP_K):
        """Return top-k chunks semantically similar to query, best-first."""
        model = _get_embedding_model()
        async with self._lock:
            if model is None or self._embeddings is None or not self._chunks:
                return []
            try:
                q = np.asarray(list(model.embed([query]))[0], dtype="float32").reshape(1, -1)
            except Exception:
                return []
            norms = np.linalg.norm(self._embeddings, axis=1, keepdims=True)
            norms[norms == 0] = 1e-9
            qn = np.linalg.norm(q)
            if qn <= 0:
                return []
            sims = (self._embeddings @ q.T).flatten() / (norms.flatten() * qn)
            results = []
            for i in np.argsort(-sims)[:top_k]:
                if sims[i] <= 0:
                    continue
                entry = dict(self._chunks[int(i)])
                entry["score"] = float(sims[i])
                results.append(entry)
            return results


def _store(thread_id):
    if not thread_id:
        return None
    store = _stores.get(thread_id)
    if store is None:
        store = ThreadMemory()
        _stores[thread_id] = store
    return store


async def remember(config, text, url="", title=""):
    """Best-effort: embed content into the current thread's memory."""
    try:
        thread_id = (config or {}).get("configurable", {}).get("thread_id")
        store = _store(thread_id)
        if store is not None and text and text.strip():
            await store.add(text, url=url, title=title)
    except Exception as e:
        logger.debug("Vector memory: remember skipped (%s)", e)


async def recall(query, top_k=_DEFAULT_TOP_K, config=None):
    """Return top-k remembered chunks for query in the current thread."""
    try:
        thread_id = (config or {}).get("configurable", {}).get("thread_id")
        store = _store(thread_id)
        if store is None:
            return []
        return await store.search(query, top_k=top_k)
    except Exception as e:
        logger.debug("Vector memory: recall failed (%s)", e)
        return []


def clear_memory(thread_id):
    """Drop a thread's memory; called when a research cycle ends."""
    if thread_id:
        _stores.pop(thread_id, None)


_RECALL_DESCRIPTION = (
    "Search the research memory: retrieve the most relevant passages previously "
    "read or fetched during this research, with source URLs and similarity scores. "
    "Use this to re-consult exact wording or details from content you already "
    "gathered, instead of re-searching the web."
)


async def _recall_tool_impl(query: str, top_k: int, config) -> str:
    """Recall the most relevant previously-read passages for `query`."""
    results = await recall(query, top_k=top_k, config=config)
    if not results:
        return ("No matching content found in the research memory yet. "
                "Search the web for new information instead.")
    lines = [f"Recall results for: {query}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"\n--- MEMORY {i} (score {r['score']:.2f}) ---")
        lines.append(f"SOURCE: {r['url']}")
        lines.append(f"TITLE: {r['title']}")
        lines.append(r["text"][:1200])
        lines.append("-" * 60)
    return "\n".join(lines)


from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool


@tool(description=_RECALL_DESCRIPTION)
async def recall_from_read_content(
    query: str,
    top_k: int = _DEFAULT_TOP_K,
    config: RunnableConfig = None,
) -> str:
    """Recall the most relevant previously-read passages for `query`."""
    return await _recall_tool_impl(query, top_k, config)
