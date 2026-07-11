from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home


_IMPORT_HOME = _test_home.TestHome.acquire("ba-test-requirement-semantic-import-")

import numpy as np  # noqa: E402

from stores.requirement_semantic_index import (  # noqa: E402
    RequirementSemanticIndex,
    SemanticIndexError,
)
from stores.requirement_store import RequirementStore, SENSITIVITIES  # noqa: E402


ALL_SENSITIVITIES = frozenset(SENSITIVITIES)
DIM = 32


class CountingEmbedder:
    """Deterministic toy embedder: hashed bag-of-words, unit-normalized."""

    def __init__(self) -> None:
        self.embedded_texts: list[str] = []

    def __call__(self, texts: list[str]) -> np.ndarray:
        self.embedded_texts.extend(texts)
        vectors = np.zeros((len(texts), DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            for token in text.lower().split():
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                vectors[row, digest[0] % DIM] += 1.0
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vectors / norms


def _register(store: RequirementStore, requirement_id: str, text: str, **overrides):
    fields = {
        "requirement_id": requirement_id,
        "text": text,
        "kind": "communication",
        "authority": "user_stated",
        "sensitivity": "normal",
        "source_session_id": "session-1",
        "source_message_id": f"message-{requirement_id}",
        "span_start": 0,
        "span_end": len(text),
    }
    fields.update(overrides)
    return store.register(**fields)


def _fixture(embedder=None):
    store = RequirementStore()
    embedder = embedder or CountingEmbedder()
    index = RequirementSemanticIndex(store, embedder=embedder, embedder_id="toy-v1")
    _register(store, "req-tests", "every bug fix must include a failing regression test")
    _register(store, "req-brevity", "keep responses short and executive summary style")
    _register(
        store,
        "req-secret",
        "the deploy token rotation runbook is confidential",
        sensitivity="secret",
    )
    return store, embedder, index


def test_semantic_search_ranks_by_similarity_with_citations() -> None:
    store, embedder, index = _fixture()
    hits = index.search(
        "regression test for a bug fix", authorized_sensitivities=ALL_SENSITIVITIES
    )
    assert hits and hits[0]["requirement_id"] == "req-tests"
    assert hits[0]["source"]["message_id"] == "message-req-tests"
    assert all(set(h["source"]) == {"session_id", "message_id", "span_start", "span_end", "sha256"} for h in hits)


def test_semantic_search_enforces_authorization_via_truth() -> None:
    store, embedder, index = _fixture()
    hits = index.search(
        "confidential deploy token runbook",
        authorized_sensitivities=frozenset({"normal"}),
    )
    assert all(h["requirement_id"] != "req-secret" for h in hits)
    hits = index.search(
        "confidential deploy token runbook",
        authorized_sensitivities=frozenset({"secret"}),
    )
    assert hits and hits[0]["requirement_id"] == "req-secret"
    for invalid in ((), ("bogus",), "normal"):
        try:
            index.search("anything", authorized_sensitivities=invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid authorization {invalid!r} was accepted")


def test_incremental_reuse_and_watermark_staleness() -> None:
    store, embedder, index = _fixture()
    index.search("test", authorized_sensitivities=ALL_SENSITIVITIES)
    baseline = len(embedder.embedded_texts)
    assert baseline == 3 + 1  # three corpus texts + one query

    _register(store, "req-new", "always run the linter before committing changes")
    hits = index.search("linter commit", authorized_sensitivities=ALL_SENSITIVITIES)
    assert any(h["requirement_id"] == "req-new" for h in hits)
    corpus_embeds = [t for t in embedder.embedded_texts[baseline:] if t != "linter commit"]
    assert corpus_embeds == ["always run the linter before committing changes"]


def test_embedder_identity_change_forces_full_rebuild() -> None:
    store, embedder, index = _fixture()
    index.search("test", authorized_sensitivities=ALL_SENSITIVITIES)
    replacement = CountingEmbedder()
    reindex = RequirementSemanticIndex(store, embedder=replacement, embedder_id="toy-v2")
    reindex.search("test", authorized_sensitivities=ALL_SENSITIVITIES)
    assert len([t for t in replacement.embedded_texts if t != "test"]) == 3


def test_purge_destroys_vector_file_at_delete_time() -> None:
    store, embedder, index = _fixture()
    index.search("test", authorized_sensitivities=ALL_SENSITIVITIES)
    assert (store.index_dir / "vectors.npz").exists()
    store.delete("req-secret", expected_revision=1)
    assert not store.index_dir.exists()
    hits = index.search(
        "confidential deploy token runbook", authorized_sensitivities=ALL_SENSITIVITIES
    )
    assert all(h["requirement_id"] != "req-secret" for h in hits)


def test_corrupt_vector_file_is_rebuilt_not_trusted() -> None:
    store, embedder, index = _fixture()
    index.search("test", authorized_sensitivities=ALL_SENSITIVITIES)
    vector_file = store.index_dir / "vectors.npz"
    vector_file.write_bytes(b"not an npz archive")
    hits = index.search(
        "regression test for a bug fix", authorized_sensitivities=ALL_SENSITIVITIES
    )
    assert hits and hits[0]["requirement_id"] == "req-tests"

    # Loadable npz with mangled metadata shapes must also rebuild, not stick.
    watermark = store.truth_watermark()
    np.savez(
        vector_file,
        format_version=np.asarray([1, 1]),
        watermark=np.int64(watermark),
        embedder_id=np.str_("toy-v1"),
        requirement_ids=np.asarray(["req-tests"], dtype=str),
        text_sha256=np.asarray(["0" * 64], dtype=str),
        vectors=np.zeros((1, DIM), dtype=np.float32),
    )
    hits = index.search(
        "regression test for a bug fix", authorized_sensitivities=ALL_SENSITIVITIES
    )
    assert hits and hits[0]["requirement_id"] == "req-tests"


def test_bad_embedder_output_fails_closed() -> None:
    store = RequirementStore()
    _register(store, "req-1", "some requirement text")
    for label, bad in (
        ("nan", lambda texts: np.full((len(texts), DIM), np.nan, dtype=np.float32)),
        ("shape", lambda texts: np.zeros((len(texts) + 1, DIM), dtype=np.float32)),
        ("zero", lambda texts: np.zeros((len(texts), DIM), dtype=np.float32)),
    ):
        index = RequirementSemanticIndex(store, embedder=bad, embedder_id=f"bad-{label}")
        try:
            index.search("anything", authorized_sensitivities=ALL_SENSITIVITIES)
        except SemanticIndexError:
            pass
        else:
            raise AssertionError(f"{label} embedder output was accepted")


def test_deterministic_rebuild() -> None:
    store, embedder, index = _fixture()
    index.search("test", authorized_sensitivities=ALL_SENSITIVITIES)
    with np.load(store.index_dir / "vectors.npz", allow_pickle=False) as archive:
        first = {key: np.copy(archive[key]) for key in archive.files}
    (store.index_dir / "vectors.npz").unlink()
    index.search("test", authorized_sensitivities=ALL_SENSITIVITIES)
    with np.load(store.index_dir / "vectors.npz", allow_pickle=False) as archive:
        second = {key: np.copy(archive[key]) for key in archive.files}
    assert first.keys() == second.keys()
    for key in first:
        assert np.array_equal(first[key], second[key]), key


def test_hybrid_rerank_fuses_exact_and_semantic() -> None:
    store, embedder, index = _fixture()
    _register(store, "req-verify", "verify changes end to end before reporting done")
    hybrid = index.search_hybrid(
        "regression test", authorized_sensitivities=ALL_SENSITIVITIES, limit=3
    )
    ids = [h["requirement_id"] for h in hybrid]
    assert ids and ids[0] == "req-tests"
    assert len(ids) == len(set(ids))
    restricted = index.search_hybrid(
        "confidential deploy token runbook",
        authorized_sensitivities=frozenset({"normal"}),
    )
    assert all(h["requirement_id"] != "req-secret" for h in restricted)


def main() -> None:
    tests = [
        test_semantic_search_ranks_by_similarity_with_citations,
        test_semantic_search_enforces_authorization_via_truth,
        test_incremental_reuse_and_watermark_staleness,
        test_embedder_identity_change_forces_full_rebuild,
        test_purge_destroys_vector_file_at_delete_time,
        test_corrupt_vector_file_is_rebuilt_not_trusted,
        test_bad_embedder_output_fails_closed,
        test_deterministic_rebuild,
        test_hybrid_rerank_fuses_exact_and_semantic,
    ]
    _IMPORT_HOME.release()
    for test in tests:
        home = _test_home.TestHome.acquire("ba-test-requirement-semantic-")
        try:
            test()
            print(f"PASS {test.__name__}")
        finally:
            home.release()


if __name__ == "__main__":
    main()
