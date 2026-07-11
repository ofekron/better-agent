from __future__ import annotations

import hashlib
import os
import tempfile
import time
import zipfile
from typing import Any, Callable, Iterable

import numpy as np

import perf
from stores.requirement_store import (
    MAX_QUERY_CHARS,
    RequirementStore,
    _validate_authorization,
)
from stores.sqlite_truth_base import required_identifier


INDEX_FORMAT_VERSION = 1
RRF_RANK_CONSTANT = 60
_FILE_NAME = "vectors.npz"

Embedder = Callable[[list[str]], Any]


class SemanticIndexError(RuntimeError):
    pass


def _validate_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query must be a non-empty string of at most {MAX_QUERY_CHARS} characters")
    return query


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalized_vectors(raw: Any, expected_rows: int, *, label: str) -> np.ndarray:
    vectors = np.asarray(raw, dtype=np.float32)
    if vectors.ndim != 2 or vectors.shape[0] != expected_rows or vectors.shape[1] < 1:
        raise SemanticIndexError(f"{label} produced shape {vectors.shape}, expected ({expected_rows}, dim)")
    if not np.isfinite(vectors).all():
        raise SemanticIndexError(f"{label} produced non-finite values")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if (norms == 0).any():
        raise SemanticIndexError(f"{label} produced a zero-norm vector")
    return vectors / norms


class RequirementSemanticIndex:
    """Disposable vector index over a RequirementStore.

    Never authoritative: every similarity hit is re-qualified against the
    truth rows via qualify_citations. The npz file lives inside the store's
    index_dir, so a purge destroys it wholesale; freshness is keyed on
    (format version, truth watermark, embedder_id, dim), and any mismatch or
    unreadable file causes a locked destroy-and-rebuild. Only requirement ids,
    text hashes, and vectors are persisted — never text.
    """

    def __init__(self, store: RequirementStore, *, embedder: Embedder, embedder_id: str) -> None:
        self._store = store
        self._embedder = embedder
        self._embedder_id = required_identifier("embedder_id", embedder_id)
        self._file = store.index_dir / _FILE_NAME

    def search(
        self,
        query: str,
        *,
        authorized_sensitivities: Iterable[str],
        include_superseded: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        query = _validate_query(query)
        started = time.perf_counter()
        index = self._ensure()
        try:
            if index["requirement_ids"].size == 0:
                return []
            query_vector = _normalized_vectors(
                self._embedder([query]), 1, label="query embedding"
            )[0]
            if query_vector.shape[0] != index["vectors"].shape[1]:
                raise SemanticIndexError(
                    "query embedding dimension does not match the index"
                )
            similarities = index["vectors"] @ query_vector
            order = np.lexsort((index["requirement_ids"], -similarities))
            candidates = [str(index["requirement_ids"][i]) for i in order]
            return self._store.qualify_citations(
                candidates,
                authorized_sensitivities=authorized_sensitivities,
                include_superseded=include_superseded,
                limit=limit,
            )
        finally:
            perf.record(
                "requirement_semantic_index.search",
                (time.perf_counter() - started) * 1000.0,
            )

    def search_hybrid(
        self,
        query: str,
        *,
        authorized_sensitivities: Iterable[str],
        include_superseded: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Rerank the union of exact (FTS) and semantic hits with reciprocal
        rank fusion. Both hit lists are already truth-verified and authorized;
        fusion only reorders and dedupes."""
        _validate_authorization(authorized_sensitivities)
        exact = self._store.retrieve(
            query,
            authorized_sensitivities=authorized_sensitivities,
            include_superseded=include_superseded,
            limit=limit,
        )
        semantic = self.search(
            query,
            authorized_sensitivities=authorized_sensitivities,
            include_superseded=include_superseded,
            limit=limit,
        )
        scores: dict[str, float] = {}
        citations: dict[str, dict[str, Any]] = {}
        for hits in (exact, semantic):
            for rank, citation in enumerate(hits):
                requirement_id = citation["requirement_id"]
                scores[requirement_id] = scores.get(requirement_id, 0.0) + 1.0 / (
                    RRF_RANK_CONSTANT + rank + 1
                )
                citations.setdefault(requirement_id, citation)
        fused = sorted(scores, key=lambda rid: (-scores[rid], rid))
        return [citations[requirement_id] for requirement_id in fused[:limit]]

    # -- index lifecycle -----------------------------------------------------

    def _ensure(self) -> dict[str, np.ndarray]:
        watermark = self._store.truth_watermark()
        index = self._load(watermark)
        if index is not None:
            return index
        with self._store.index_rebuild_lock():
            watermark = self._store.truth_watermark()
            index = self._load(watermark)
            if index is not None:
                return index
            return self._rebuild(watermark)

    def _load(self, watermark: int) -> dict[str, np.ndarray] | None:
        if not self._file.exists():
            return None
        try:
            with np.load(self._file, allow_pickle=False) as archive:
                if (
                    int(archive["format_version"]) != INDEX_FORMAT_VERSION
                    or int(archive["watermark"]) != watermark
                    or str(archive["embedder_id"]) != self._embedder_id
                ):
                    return None
                index = {
                    "requirement_ids": archive["requirement_ids"].astype(str),
                    "text_sha256": archive["text_sha256"].astype(str),
                    "vectors": np.asarray(archive["vectors"], dtype=np.float32),
                }
        except (OSError, TypeError, ValueError, KeyError, zipfile.BadZipFile):
            return None
        ids, shas, vectors = index["requirement_ids"], index["text_sha256"], index["vectors"]
        if (
            vectors.ndim != 2
            or ids.shape[0] != vectors.shape[0]
            or shas.shape[0] != vectors.shape[0]
            or not np.isfinite(vectors).all()
        ):
            return None
        return index

    def _rebuild(self, watermark: int) -> dict[str, np.ndarray]:
        started = time.perf_counter()
        rows = self._store.indexable_rows()
        prior = self._reusable_vectors()
        ids: list[str] = []
        shas: list[str] = []
        vector_rows: list[np.ndarray | None] = []
        missing_texts: list[str] = []
        missing_slots: list[int] = []
        for row in rows:
            sha = _text_sha256(row["text"])
            ids.append(row["requirement_id"])
            shas.append(sha)
            reused = prior.get(sha)
            vector_rows.append(reused)
            if reused is None:
                missing_slots.append(len(vector_rows) - 1)
                missing_texts.append(row["text"])
        if missing_texts:
            embedded = _normalized_vectors(
                self._embedder(missing_texts), len(missing_texts), label="embedder"
            )
            for slot, vector in zip(missing_slots, embedded):
                vector_rows[slot] = vector
        if vector_rows:
            dims = {vector.shape[0] for vector in vector_rows}
            if len(dims) != 1:
                raise SemanticIndexError("mixed embedding dimensions in rebuild")
            vectors = np.stack(vector_rows).astype(np.float32)
        else:
            vectors = np.zeros((0, 1), dtype=np.float32)
        index = {
            "requirement_ids": np.asarray(ids, dtype=str),
            "text_sha256": np.asarray(shas, dtype=str),
            "vectors": vectors,
        }
        self._store.index_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temp_path = tempfile.mkstemp(
            prefix=".vectors.", suffix=".npz", dir=str(self._store.index_dir)
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                np.savez(
                    handle,
                    format_version=np.int64(INDEX_FORMAT_VERSION),
                    watermark=np.int64(watermark),
                    embedder_id=np.str_(self._embedder_id),
                    **index,
                )
            os.replace(temp_path, self._file)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise
        finally:
            perf.record(
                "requirement_semantic_index.rebuild",
                (time.perf_counter() - started) * 1000.0,
            )
        return index

    def _reusable_vectors(self) -> dict[str, np.ndarray]:
        """Vectors from the outgoing index keyed by text sha — embeddings are
        deterministic per (embedder_id, text), so unchanged texts skip the
        embedder on rebuild."""
        if not self._file.exists():
            return {}
        try:
            with np.load(self._file, allow_pickle=False) as archive:
                if str(archive["embedder_id"]) != self._embedder_id:
                    return {}
                if int(archive["format_version"]) != INDEX_FORMAT_VERSION:
                    return {}
                shas = archive["text_sha256"].astype(str)
                vectors = np.asarray(archive["vectors"], dtype=np.float32)
        except (OSError, TypeError, ValueError, KeyError, zipfile.BadZipFile):
            return {}
        if vectors.ndim != 2 or shas.shape[0] != vectors.shape[0] or not np.isfinite(vectors).all():
            return {}
        return {str(sha): vectors[i] for i, sha in enumerate(shas)}
