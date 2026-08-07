"""Unit owner for project_match.embedding.

Pure-numpy static-embedding inference: L2-normalization, top-k cosine similarity,
and the [-1, 1] -> [0, 1] score map. The heavyweight collaborator is model2vec's
StaticModel (a ~1.2GB lazy load); we inject a fake ``model2vec`` module so
``_model()``'s real body (lazy import + from_pretrained) executes against
deterministic vectors without the load.
"""
import sys
import types

import numpy as np
import pytest

import project_match.embedding as emb
from project_match.embedding import (
    MODEL_NAME,
    TOP_K,
    EmbeddingTopic,
    embed,
    embedding_similarity,
)

# Canonical orthogonal basis vectors for deterministic similarity math.
_E0 = [1.0, 0.0]
_E1 = [0.0, 1.0]


class _FakeStaticModel:
    """model2vec.StaticModel stand-in. ``encode_output`` is set per test."""

    encode_output = [_E0]
    from_pretrained_calls = 0

    @classmethod
    def from_pretrained(cls, name):
        cls.from_pretrained_calls += 1
        return cls()

    def encode(self, texts):
        # embed() passes list(texts); each test configures exactly one row per
        # input text so the returned matrix aligns with the asserted math.
        return np.asarray(self.encode_output, dtype=np.float32)

    @staticmethod
    def make_from_pretrained_spy(sink):
        def _from_pretrained(cls, name):
            sink.append(name)
            return cls()

        return classmethod(_from_pretrained)


@pytest.fixture(autouse=True)
def fake_model2vec(monkeypatch):
    """Inject a fake model2vec module and clear the lru_cache around each test."""
    fake_mod = types.ModuleType("model2vec")
    fake_mod.StaticModel = _FakeStaticModel
    monkeypatch.setitem(sys.modules, "model2vec", fake_mod)
    _FakeStaticModel.from_pretrained_calls = 0
    emb._model.cache_clear()
    yield
    emb._model.cache_clear()


def _set_output(rows):
    _FakeStaticModel.encode_output = rows


# --- constants / contract ----------------------------------------------------

def test_constants():
    assert MODEL_NAME == "minishlab/potion-multilingual-128M"
    assert TOP_K == 5


# --- _model ------------------------------------------------------------------

def test_model_loads_from_model2vec_with_canonical_name(monkeypatch):
    sink = []
    monkeypatch.setattr(
        _FakeStaticModel,
        "from_pretrained",
        _FakeStaticModel.make_from_pretrained_spy(sink),
    )
    emb._model.cache_clear()
    model = emb._model()
    assert model is not None
    assert sink == [MODEL_NAME]


def test_model_from_pretrained_invoked_once_per_cache_fill():
    _set_output([_E0])
    emb._model()
    assert _FakeStaticModel.from_pretrained_calls == 1


def test_model_is_cached_across_calls():
    _set_output([_E0])
    emb._model()
    emb._model()
    assert _FakeStaticModel.from_pretrained_calls == 1  # lru_cache(maxsize=1)


# --- embed -------------------------------------------------------------------

def test_embed_l2_normalizes_each_row():
    _set_output([[3.0, 4.0], [0.0, 5.0]])
    v = embed(["a", "b"])
    assert v.shape == (2, 2)
    assert v.dtype == np.float32
    norms = np.linalg.norm(v, axis=1)
    np.testing.assert_allclose(norms, [1.0, 1.0], atol=1e-6)
    np.testing.assert_allclose(v[0], [0.6, 0.8], atol=1e-6)
    np.testing.assert_allclose(v[1], [0.0, 1.0], atol=1e-6)


def test_embed_epsilon_prevents_nan_on_zero_vector():
    _set_output([[0.0, 0.0]])
    v = embed(["zero"])
    assert v.shape == (1, 2)
    assert np.all(np.isfinite(v))
    np.testing.assert_allclose(v, [[0.0, 0.0]], atol=1e-6)


def test_embed_passes_list_of_texts_to_model(monkeypatch):
    captured = {}
    orig = _FakeStaticModel.encode

    def spy(self, texts):
        captured["texts"] = texts
        captured["is_list"] = isinstance(texts, list)
        return orig(self, texts)

    _set_output([_E0])
    monkeypatch.setattr(_FakeStaticModel, "encode", spy)
    embed(("x", "y"))
    assert captured["is_list"] is True
    assert captured["texts"] == ["x", "y"]


def test_embed_returns_float32_and_preserves_count():
    _set_output([_E0, _E1, _E0])
    v = embed(["p1", "p2", "p3"])
    assert v.dtype == np.float32
    assert v.shape[0] == 3


# --- EmbeddingTopic ----------------------------------------------------------

def test_topic_from_prompts_matches_embed():
    _set_output([_E0, _E1])
    prompts = ["hello", "world"]
    topic = EmbeddingTopic.from_prompts(prompts)
    np.testing.assert_allclose(topic.vectors, embed(prompts), atol=1e-7)


def test_topic_is_dataclass_with_vectors_field():
    _set_output([_E0])
    topic = EmbeddingTopic.from_prompts(["only"])
    assert topic.vectors.shape == (1, 2)
    # dataclass: direct field access.
    assert topic.vectors is topic.vectors


# --- embedding_similarity ----------------------------------------------------

def test_similarity_uses_all_sims_when_fewer_prompts_than_k():
    # n=2 <= default k=5 -> True branch (no np.partition).
    _set_output([_E0, _E1])
    topic = EmbeddingTopic.from_prompts(["a", "b"])
    text = np.asarray(_E0, dtype=np.float32)  # matches prompt 0
    score = embedding_similarity(topic, text)
    # sims = [1.0, 0.0], mean 0.5 -> (0.5 + 1) / 2 = 0.75.
    assert score == pytest.approx(0.75)


def test_similarity_uses_top_k_when_more_prompts_than_k():
    # n=4 > k=2 -> False branch (np.partition top-k).
    _set_output([_E0, _E1, _E0, _E1])
    topic = EmbeddingTopic.from_prompts(["a", "b", "c", "d"])
    text = np.asarray(_E0, dtype=np.float32)
    # sims = [1, 0, 1, 0]; top-2 = [1, 1]; mean 1 -> 1.0.
    score = embedding_similarity(topic, text, k=2)
    assert score == pytest.approx(1.0)


def test_similarity_more_prompts_than_default_top_k():
    # n=6 > default TOP_K=5 -> False branch with default k.
    _set_output([_E0] * 6)
    topic = EmbeddingTopic.from_prompts([str(i) for i in range(6)])
    text = np.asarray(_E0, dtype=np.float32)
    score = embedding_similarity(topic, text)
    # all sims = 1 -> 1.0
    assert score == pytest.approx(1.0)


def test_similarity_maps_zero_sim_to_midpoint():
    _set_output([_E1])
    topic = EmbeddingTopic.from_prompts(["anti"])
    text = np.asarray(_E0, dtype=np.float32)  # orthogonal -> sim 0
    score = embedding_similarity(topic, text)
    # sim 0 -> (0 + 1) / 2 = 0.5
    assert score == pytest.approx(0.5)


def test_similarity_maps_negative_sim_to_zero():
    # Anti-parallel vector -> sim -1 -> (-1 + 1) / 2 = 0.0 (low end of the map).
    _set_output([[-1.0, 0.0]])
    topic = EmbeddingTopic.from_prompts(["opposite"])
    text = np.asarray(_E0, dtype=np.float32)
    score = embedding_similarity(topic, text)
    assert score == pytest.approx(0.0)


def test_similarity_dot_product_uses_text_vector_as_is():
    # Function does not re-normalize text_vector; a scaled vector scales the dot.
    _set_output([_E0])
    topic = EmbeddingTopic.from_prompts(["a"])
    scaled = np.asarray([2.0, 0.0], dtype=np.float32)
    score = embedding_similarity(topic, scaled)
    # dot = 2.0 -> (2 + 1) / 2 = 1.5 (clamped to [0,1] domain by mapping only).
    assert score == pytest.approx(1.5)


def test_similarity_n_equals_k_boundary():
    # n == k -> True branch (<=), uses all sims.
    _set_output([_E0, _E1])
    topic = EmbeddingTopic.from_prompts(["a", "b"])
    text = np.asarray(_E0, dtype=np.float32)
    score = embedding_similarity(topic, text, k=2)
    assert score == pytest.approx(0.75)


def test_similarity_with_mixed_sims_partitions_correctly():
    # 5 prompts, k=3; sims descending; top-3 should be the three largest.
    rows = [_E0, _E0, _E1, _E1, _E0]  # sims vs _E0 = [1,1,0,0,1]
    _set_output(rows)
    topic = EmbeddingTopic.from_prompts(["a", "b", "c", "d", "e"])
    text = np.asarray(_E0, dtype=np.float32)
    score = embedding_similarity(topic, text, k=3)
    # top-3 of [1,1,0,0,1] = [1,1,1]; mean 1 -> 1.0
    assert score == pytest.approx(1.0)
