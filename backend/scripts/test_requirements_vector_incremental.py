#!/usr/bin/env python3
"""Locks incremental vector-index embedding.

The vector index used to re-embed the WHOLE corpus (~57s of CPU for ~1400
records, tens of minutes under concurrent processor forks) whenever
requirement_units.jsonl changed — which it does every time on-demand
extraction appends a unit mid-run. That full re-embed dominated processor
wall-clock and pushed runs past the 900s dispatch budget into ReadTimeout.

requirement_units.jsonl is append-only and MiniLM is deterministic per text,
so the unchanged prefix's vectors are reusable: embed only the appended tail
and vstack. These tests prove the incremental contract with a counting
embedder (hermetic — no onnxruntime, no network).
"""
from __future__ import annotations

import hashlib
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

_test_home.isolate("ba-test-vec-incr-")

import numpy as np  # noqa: E402
import requirement_context  # noqa: E402
from requirement_analysis.prephase import units_path  # noqa: E402

FAILURES: list[str] = []

DIM = 32
THROTTLE = {"rate", "limiting", "limit", "limits", "throttle", "requests", "request"}


class CountingEmbedder:
    """Toy embedder that records how many texts it was asked to embed in total,
    across all calls. Mirrors the toy clustering in test_unit_vector_search."""

    def __init__(self) -> None:
        self.embedded_total = 0

    def __call__(self, texts: list[str]) -> np.ndarray:
        self.embedded_total += len(texts)
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            toks = set(re.findall(r"[a-z]+", (t or "").lower()))
            if toks & THROTTLE:
                out[i, 0] = 1.0
            else:
                slot = int(hashlib.md5((t or "").strip().lower().encode()).hexdigest(), 16) % (DIM - 1) + 1
                out[i, slot] = 1.0
        return out


def search(emb: CountingEmbedder, query: str) -> dict:
    """Wrap the vector search and account for the per-call query embedding so
    tests can reason purely about record embeddings."""
    res = requirement_context.search_requirement_units_vector(
        query=query, all_projects=True, embedder=emb)
    emb.embedded_total -= 1  # the query vector, not a record
    return res


def records_embedded(emb: CountingEmbedder) -> int:
    return emb.embedded_total


def check(cond: bool, label: str) -> None:
    print(("PASS" if cond else "FAIL"), label)
    if not cond:
        FAILURES.append(label)


def write_units(records: list[dict]) -> None:
    path = units_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(__import__("json").dumps(record, ensure_ascii=False) + "\n")


def texts_of(result: dict) -> list[str]:
    return [m.get("text") for m in result.get("matches", [])]


def rec(key: str, text: str, ts: str, cwd: str = "/proj") -> dict:
    return {"text": text, "kind": "requirement", "source_key": key, "cwd": cwd, "ts": ts}


BASE_RECORDS = [
    rec("A", "remember preferred commit style", "1"),
    rec("B", "enforce rate caps on traffic", "2"),
    rec("C", "keep logs for thirty days", "3"),
]
QUERY = "throttle requests"


def test_cold_start_embeds_all() -> None:
    write_units(BASE_RECORDS)
    emb = CountingEmbedder()
    search(emb, QUERY)
    check(records_embedded(emb) == len(BASE_RECORDS),
          f"cold start embeds all {len(BASE_RECORDS)} records once (got {records_embedded(emb)})")


def test_append_embeds_only_tail() -> None:
    write_units(BASE_RECORDS)
    emb = CountingEmbedder()
    search(emb, QUERY)
    after_cold = records_embedded(emb)

    appended = [
        rec("D", "throttle burst requests per second", "4"),
        rec("E", "rotate credentials monthly", "5"),
    ]
    write_units(BASE_RECORDS + appended)  # append-only growth
    result = search(emb, QUERY)

    delta = records_embedded(emb) - after_cold
    check(delta == len(appended),
          f"appending {len(appended)} records embeds only the tail (delta={delta}), not the full {len(BASE_RECORDS) + len(appended)}")
    check("throttle burst requests per second" in texts_of(result),
          "appended record is searchable after incremental build")


def test_prefix_mismatch_falls_back_to_full() -> None:
    write_units(BASE_RECORDS)
    emb = CountingEmbedder()
    search(emb, QUERY)

    # Rewrite the file with a DIFFERENT first source_key. The corpus is
    # append-only with source_key dedup in production, so a key change is the
    # real mismatch signal: the cached index's prefix no longer aligns, so the
    # incremental path must bail out and re-embed everything.
    mutated = [rec("A2", "completely different requirement text", "1")] + BASE_RECORDS[1:]
    write_units(mutated)
    before = records_embedded(emb)
    search(emb, QUERY)
    check(records_embedded(emb) - before == len(mutated),
          f"prefix mismatch triggers full re-embed of {len(mutated)} (delta={records_embedded(emb) - before})")


def test_same_key_different_text_re_embeds() -> None:
    """Regression for the staleness bug: source_key is structural
    (source_prompt_key:unit:index), and _replace_units_for_prompt_keys can
    re-append a re-extracted unit under the SAME source_key with DIFFERENT
    text. Key-only prefix matching would reuse the stale vector; the per-record
    text-hash guard must force a full re-embed and serve the new text."""
    write_units(BASE_RECORDS)
    emb = CountingEmbedder()
    search(emb, QUERY)
    before = records_embedded(emb)

    # Same source_keys, same positions, but B's text changes to a throttle
    # match — key-only matching would keep B's old (non-throttle) vector and
    # the new text would NOT surface for a throttle query.
    rewrote = [
        BASE_RECORDS[0],
        rec("B", "throttle requests per second", "2"),  # same key "B", new text
        BASE_RECORDS[2],
    ]
    write_units(rewrote)
    result = search(emb, QUERY)
    check(records_embedded(emb) - before == len(rewrote),
          f"same-key/different-text forces full re-embed of {len(rewrote)} (delta={records_embedded(emb) - before}), no stale reuse")
    check("throttle requests per second" in texts_of(result),
          "re-extracted text under the same source_key is reflected (not stale)")


def test_unchanged_source_embeds_nothing() -> None:
    write_units(BASE_RECORDS)
    emb = CountingEmbedder()
    search(emb, QUERY)
    after_first = records_embedded(emb)
    # Second call with an identical source file: state matches, index reused.
    search(emb, QUERY)
    check(records_embedded(emb) == after_first,
          f"unchanged source embeds nothing on reuse (delta={records_embedded(emb) - after_first})")


def main() -> int:
    test_cold_start_embeds_all()
    test_append_embeds_only_tail()
    test_prefix_mismatch_falls_back_to_full()
    test_same_key_different_text_re_embeds()
    test_unchanged_source_embeds_nothing()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
