"""Per-session semantic recall.

Before a `delegate_to_session` runs against an existing chain, we build a
small in-memory embedding index of THAT session's prior transcript, so the
delegated turn can semantically search its own history via the
`recall_history` MCP tool — catching relevant earlier context that plain
substring grep (or a truncated/compacted window) would miss.

Per-session by design: indexing ALL sessions is too big; a single session's
transcript is bounded and cheap. Embeddings use the backend's existing
`project_match.embedding` (local model2vec, pure-numpy cosine, no new dep,
no daemon). The index is cached keyed by `(sid, message_count)` and rebuilt
only when the session has grown.
"""

import logging
from typing import Optional

import numpy as np

import session_manager
from project_match.embedding import embed

logger = logging.getLogger(__name__)

# Long messages are split into windows of this many characters so a single
# wall-of-text message doesn't become one undiscriminating chunk.
_CHUNK_CHARS = 1000
_DEFAULT_K = 5

# sid -> {"count": int, "chunks": list[dict], "vectors": np.ndarray}
_cache: dict[str, dict] = {}


def _msg_text(m: dict) -> str:
    content = (m or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts).strip()
    return ""


def _chunk(messages: list) -> list[dict]:
    """One chunk per message (long messages windowed). Each chunk keeps its
    role + ordinal message index so recall results are locatable."""
    chunks: list[dict] = []
    for idx, m in enumerate(messages or []):
        if not isinstance(m, dict):
            continue
        role = m.get("role") or ""
        if role not in ("user", "assistant"):
            continue
        text = _msg_text(m)
        if not text:
            continue
        for off in range(0, len(text), _CHUNK_CHARS):
            chunks.append({
                "role": role,
                "idx": idx,
                "text": text[off:off + _CHUNK_CHARS],
            })
    return chunks


def build_index(sid: str) -> int:
    """Build (or reuse) the embedding index for session `sid`. Returns the
    number of chunks indexed. Cached by `(sid, message_count)` — a no-op
    rebuild when the session hasn't grown since the last build."""
    sess = session_manager.manager.get(sid)
    if not sess:
        return 0
    messages = sess.get("messages") or []
    count = len(messages)
    cached = _cache.get(sid)
    if cached and cached["count"] == count:
        return len(cached["chunks"])

    chunks = _chunk(messages)
    if not chunks:
        _cache[sid] = {"count": count, "chunks": [], "vectors": np.zeros((0, 0))}
        return 0
    vectors = embed([c["text"] for c in chunks])
    _cache[sid] = {"count": count, "chunks": chunks, "vectors": vectors}
    logger.info("session_recall.build_index sid=%s msgs=%d chunks=%d",
                sid[:8], count, len(chunks))
    return len(chunks)


def recall(sid: str, query: str, *, k: int = _DEFAULT_K) -> list[dict]:
    """Top-k semantically-similar transcript chunks for `query`. Empty list
    if no index was built for `sid` (recall is opt-in per delegation)."""
    query = (query or "").strip()
    if not query:
        return []
    entry = _cache.get(sid)
    if not entry or not entry["chunks"]:
        return []
    k = max(1, min(int(k or _DEFAULT_K), 20))
    qv = embed([query])[0]
    sims = entry["vectors"] @ qv  # rows L2-normalized -> dot == cosine
    order = np.argsort(sims)[::-1][:k]
    out: list[dict] = []
    for i in order:
        c = entry["chunks"][int(i)]
        out.append({
            "role": c["role"],
            "message_index": c["idx"],
            "score": round((float(sims[int(i)]) + 1.0) / 2.0, 4),
            "text": c["text"],
        })
    return out


def drop(sid: str) -> None:
    _cache.pop(sid, None)
