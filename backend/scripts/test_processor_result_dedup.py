from __future__ import annotations

import json

import requirement_context


def _row(
    text: str,
    evidence: dict[str, object],
    *,
    polarity: str = "positive",
) -> dict[str, object]:
    return {
        "text": text,
        "kind": "explicit",
        "origin": "user_prompt",
        "polarity": polarity,
        "strength": "high",
        "source": "evidence",
        "evidence": evidence,
        "cwd": "/repo",
    }


def test_same_unit_source_key_is_deterministically_first_wins() -> None:
    first = _row("Keep the user's wording", {"unit_source_key": "unit-1"})
    duplicate = _row("Polished duplicate wording", {"unit_source_key": "unit-1"})
    text = json.dumps({"requirements": [first, duplicate]})

    result = json.loads(requirement_context.dedupe_processor_output_text(text))

    assert result["requirements"] == [first]


def test_same_transcript_row_keeps_distinct_requirements() -> None:
    evidence = {
        "path": "/transcripts/session.jsonl",
        "element_index": 4,
        "sid": "session-1",
        "ts_utc": "2026-08-05T00:00:00Z",
    }
    first = _row("Keep files short", evidence)
    formatting_variant = _row("  keep   FILES short ", evidence)
    distinct = _row("Use root-relative imports", evidence)
    text = json.dumps({"requirements": [first, formatting_variant, distinct]})

    result = json.loads(requirement_context.dedupe_processor_output_text(text))

    assert result["requirements"] == [first, distinct]


def test_different_evidence_rows_are_not_semantically_guessed() -> None:
    first = _row("Keep files short", {"unit_source_key": "unit-1"})
    second = _row("Keep files short", {"unit_source_key": "unit-2"})
    text = json.dumps({"requirements": [first, second]}, indent=2)

    assert requirement_context.dedupe_processor_output_text(text) == text


def test_opposite_lifecycle_from_same_transcript_row_is_preserved() -> None:
    evidence = {
        "path": "/transcripts/session.jsonl",
        "element_index": 8,
        "sid": "session-1",
        "ts_utc": "2026-08-05T00:01:00Z",
    }
    positive = _row("Use vector search", evidence)
    negative = _row("Use vector search", evidence, polarity="negative")
    text = json.dumps({"requirements": [positive, negative]})

    assert requirement_context.dedupe_processor_output_text(text) == text


def test_opposite_lifecycle_from_same_unit_is_preserved() -> None:
    evidence = {"unit_source_key": "unit-1"}
    positive = _row("Use vector search", evidence)
    negative = _row("Use vector search", evidence, polarity="negative")
    text = json.dumps({"requirements": [positive, negative]})

    assert requirement_context.dedupe_processor_output_text(text) == text


def test_invalid_or_already_unique_output_is_byte_preserved() -> None:
    unique = '{\n  "requirements": [{"text": "one", "evidence": {"unit_source_key": "u"}}]\n}'
    assert requirement_context.dedupe_processor_output_text(unique) == unique
    assert requirement_context.dedupe_processor_output_text("not json") == "not json"


def test_schema_invalid_output_is_byte_preserved() -> None:
    malformed = json.dumps({
        "requirements": [
            {
                "text": "one",
                "kind": ["explicit"],
                "origin": "user_prompt",
                "polarity": "positive",
                "evidence": {"unit_source_key": "unit-1"},
            },
            {
                "text": "two",
                "kind": ["explicit"],
                "origin": "user_prompt",
                "polarity": "positive",
                "evidence": {"unit_source_key": "unit-1"},
            },
        ]
    })

    assert requirement_context.dedupe_processor_output_text(malformed) == malformed


def test_unsupported_evidence_identity_is_byte_preserved() -> None:
    duplicate = _row("Keep files short", {})
    malformed = json.dumps({"requirements": [duplicate, duplicate]}, indent=2)

    assert requirement_context.dedupe_processor_output_text(malformed) == malformed
