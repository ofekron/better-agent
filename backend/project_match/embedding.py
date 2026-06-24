"""Static multilingual sentence embeddings (model2vec).

Pure-numpy inference: no torch, ~0.4ms/query, ~1.2GB resident. Covers English
and Hebrew. Chosen over a transformer (sentence-transformers) which added a
~1.3GB resident / 400GB-virtual torch footprint for the same accuracy.
"""
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

MODEL_NAME = "minishlab/potion-multilingual-128M"

TOP_K = 5


@lru_cache(maxsize=1)
def _model():
    from model2vec import StaticModel

    return StaticModel.from_pretrained(MODEL_NAME)


def embed(texts: list[str]) -> np.ndarray:
    """L2-normalized static embeddings, shape (len(texts), dim)."""
    v = np.asarray(_model().encode(list(texts)), dtype=np.float32)
    return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)


@dataclass
class EmbeddingTopic:
    """A project, represented by the embeddings of its individual prompts."""

    vectors: np.ndarray  # (n_prompts, dim), each row L2-normalized

    @classmethod
    def from_prompts(cls, prompts: list[str]) -> "EmbeddingTopic":
        return cls(embed(prompts))


def embedding_similarity(topic: EmbeddingTopic, text_vector: np.ndarray, k: int = TOP_K) -> float:
    """Mean of the top-k cosine similarities between the text and the project's
    prompts, mapped from [-1, 1] to [0, 1]. k-NN over real prompts discriminates
    far better than a single averaged centroid.
    """
    sims = topic.vectors @ text_vector  # rows are L2-normalized -> dot == cosine
    top = sims if sims.shape[0] <= k else np.partition(sims, -k)[-k:]
    return (float(top.mean()) + 1.0) / 2.0
