#!/usr/bin/env python3
"""100% unit coverage for runtime_skill_templates.

Covers template-variable normalization (None / valid / dedup / non-list /
non-str-item / unknown-variable branches), the skill-text machine_id
specialization (no-op short-circuits + replacement + invalid machine_id),
and the skill-file read/rewrite/no-op-write paths. Pure stdlib logic; the
test-home guard is engaged only to match repo convention.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import _test_home  # noqa: E402

TEST_HOME = _test_home.TestHome.acquire("ba-runtime-skill-templates-")
import atexit  # noqa: E402

atexit.register(TEST_HOME.release)

from runtime_skill_templates import (  # noqa: E402
    MACHINE_ID_TEMPLATE_TOKEN,
    MACHINE_ID_TEMPLATE_VARIABLE,
    SUPPORTED_TEMPLATE_VARIABLES,
    RuntimeSkillSource,
    normalize_template_variables,
    specialize_skill_file,
    specialize_skill_text,
)


def test_constants_and_supported_variables():
    assert MACHINE_ID_TEMPLATE_VARIABLE == "machine_id"
    assert MACHINE_ID_TEMPLATE_TOKEN == "{{better_agent.machine_id}}"
    assert SUPPORTED_TEMPLATE_VARIABLES == frozenset({"machine_id"})


def test_runtime_skill_source_defaults(tmp_path):
    source = RuntimeSkillSource(root=tmp_path)
    assert source.root == tmp_path
    assert source.template_variables == ()


def test_normalize_none_returns_empty():
    assert normalize_template_variables(None) == ()


def test_normalize_empty_list_returns_empty():
    assert normalize_template_variables([]) == ()


def test_normalize_valid_list_and_tuple():
    assert normalize_template_variables(["machine_id"]) == ("machine_id",)
    assert normalize_template_variables(("machine_id",)) == ("machine_id",)


def test_normalize_dedupes_preserving_order():
    assert normalize_template_variables(["machine_id", "machine_id"]) == (
        "machine_id",
    )


def test_normalize_rejects_non_sequence():
    for value in ("machine_id", 5, object()):
        with pytest.raises(ValueError, match="must be a list of strings"):
            normalize_template_variables(value)


def test_normalize_rejects_non_string_item():
    with pytest.raises(ValueError, match="must be a list of strings"):
        normalize_template_variables([1])


def test_normalize_rejects_unknown_variable_names_sorted():
    with pytest.raises(
        ValueError, match=r"unsupported runtime skill template variable: alpha"
    ):
        normalize_template_variables(["zeta", "alpha"])


def test_specialize_text_noop_when_variable_not_requested():
    text = f"hello {MACHINE_ID_TEMPLATE_TOKEN} world"
    assert (
        specialize_skill_text(text, template_variables=[], machine_id="node-1")
        == text
    )


def test_specialize_text_noop_when_token_absent():
    assert (
        specialize_skill_text(
            "no token here", template_variables=["machine_id"], machine_id="node-1"
        )
        == "no token here"
    )


def test_specialize_text_replaces_token_with_valid_machine_id():
    text = f"id={MACHINE_ID_TEMPLATE_TOKEN}"
    assert (
        specialize_skill_text(
            text, template_variables=["machine_id"], machine_id="node-1"
        )
        == "id=node-1"
    )


def test_specialize_text_replaces_all_token_occurrences():
    text = f"{MACHINE_ID_TEMPLATE_TOKEN}-{MACHINE_ID_TEMPLATE_TOKEN}"
    assert (
        specialize_skill_text(
            text, template_variables=["machine_id"], machine_id="node-1"
        )
        == "node-1-node-1"
    )


@pytest.mark.parametrize("machine_id", [None, "", "bad node!", "1 leading"])
def test_specialize_text_rejects_invalid_machine_id(machine_id):
    with pytest.raises(ValueError, match="machine node id is invalid"):
        specialize_skill_text(
            MACHINE_ID_TEMPLATE_TOKEN,
            template_variables=["machine_id"],
            machine_id=machine_id,
        )


def test_specialize_file_rewrites_when_token_present(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text(f"use {MACHINE_ID_TEMPLATE_TOKEN} now", encoding="utf-8")
    specialize_skill_file(
        path, template_variables=["machine_id"], machine_id="node-1"
    )
    assert path.read_text(encoding="utf-8") == "use node-1 now"


def test_specialize_file_skips_write_when_token_absent(tmp_path):
    path = tmp_path / "SKILL.md"
    original = "static skill text"
    path.write_text(original, encoding="utf-8")
    stamp = path.stat().st_mtime_ns

    specialize_skill_file(
        path, template_variables=["machine_id"], machine_id="node-1"
    )

    assert path.read_text(encoding="utf-8") == original
    assert path.stat().st_mtime_ns == stamp
