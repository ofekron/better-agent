#!/usr/bin/env python3
"""Locks the 4th requirements evidence channel: search_requirement_units_vector.

The vector (ONNX MiniLM cosine) channel closes the BM25-blind slice —
requirements semantically related to the request but sharing no tokens with it,
which rg / FTS / transcript-SQL cannot surface. A toy embedder is injected so
the test is hermetic (no onnxruntime, no network); it clusters a synonym set
onto one basis vector, proving the index + cosine ranking + cwd filter +
source-change rebuild logic that the real MiniLM embedder drives in production.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # .../backend
REPO = ROOT.parent
PKG_ROOT = REPO / "better-agent-private" / "extensions" / "requirements"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PKG_ROOT))
sys.path.insert(0, str(REPO / "sdk"))

import _test_home  # noqa: E402

_test_home.isolate("ba-test-vec-")

import numpy as np  # noqa: E402
import requirement_context  # noqa: E402
from requirement_analysis.prephase import units_path  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("PASS" if cond else "FAIL"), label)
    if not cond:
        FAILURES.append(label)


DIM = 64
# Synonyms the toy embedder collapses onto one semantic direction. "rate
# limiting" and "throttle requests" share no tokens but map to the same vector.
THROTTLE = {"rate", "limiting", "limit", "limits", "throttle", "requests", "request"}


def toy_embed(texts: list[str]) -> np.ndarray:
    out = np.zeros((len(texts), DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        toks = set(re.findall(r"[a-z]+", (t or "").lower()))
        if toks & THROTTLE:
            out[i, 0] = 1.0
        else:
            slot = int(hashlib.md5((t or "").strip().lower().encode()).hexdigest(), 16) % (DIM - 1) + 1
            out[i, slot] = 1.0
    return out


def write_units(records: list[dict]) -> None:
    path = units_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def texts_of(result: dict) -> list[str]:
    return [m.get("text") for m in result.get("matches", [])]


BLIND_QUERY = "throttle requests to protect backend"
A_TEXT = "enforce rate caps on incoming traffic"


def test_blind_slice_vector_recovers_fts_miss() -> None:
    a = {"text": A_TEXT, "kind": "requirement", "source_key": "A", "cwd": "/proj", "ts": "1"}
    b = {"text": "remember the user's preferred commit message style", "kind": "preference",
         "source_key": "B", "cwd": "/proj", "ts": "2"}
    write_units([a, b])

    fts = requirement_context.search_requirement_units_fts(query=BLIND_QUERY, all_projects=True)
    check(A_TEXT not in texts_of(fts),
          "FTS misses the lexically-blind semantic match (the gap the vector channel closes)")

    vec = requirement_context.search_requirement_units_vector(
        query=BLIND_QUERY, all_projects=True, embedder=toy_embed)
    check(bool(vec.get("success")) and vec.get("count", 0) >= 1, "vector search returns matches")
    matches = texts_of(vec)
    check(A_TEXT in matches, "vector recovers the blind semantic match")
    check(bool(matches) and matches[0] == A_TEXT, "blind match ranks first by cosine")
    check("remember the user's preferred commit message style" not in matches,
          "unrelated record excluded (orthogonal vector)")


def test_index_rebuild_on_source_change() -> None:
    a = {"text": A_TEXT, "kind": "requirement", "source_key": "A", "cwd": "/proj", "ts": "1"}
    write_units([a])
    requirement_context.search_requirement_units_vector(
        query=BLIND_QUERY, all_projects=True, embedder=toy_embed)

    c = {"text": "throttle burst requests per second", "kind": "requirement",
         "source_key": "C", "cwd": "/proj", "ts": "3"}
    write_units([a, c])  # size/mtime change → cached index must rebuild
    vec = requirement_context.search_requirement_units_vector(
        query=BLIND_QUERY, all_projects=True, embedder=toy_embed)
    check("throttle burst requests per second" in texts_of(vec),
          "vector index rebuilt after requirement_units.jsonl change")


def test_cwd_filter() -> None:
    a = {"text": A_TEXT, "kind": "requirement", "source_key": "A", "cwd": "/proj-one", "ts": "1"}
    d = {"text": "throttle requests per second", "kind": "requirement",
         "source_key": "D", "cwd": "/proj-two", "ts": "2"}
    write_units([a, d])
    vec = requirement_context.search_requirement_units_vector(
        query=BLIND_QUERY, cwd="/proj-one", all_projects=False, embedder=toy_embed)
    matches = texts_of(vec)
    check(A_TEXT in matches, "cwd filter keeps own-project match")
    check("throttle requests per second" not in matches,
          "cwd filter excludes other-project match")


def main() -> int:
    test_blind_slice_vector_recovers_fts_miss()
    test_index_rebuild_on_source_change()
    test_cwd_filter()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
