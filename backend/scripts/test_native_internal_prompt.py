"""Unit coverage for native_internal_prompt.py.

The module is a dependency-free classifier: it decides whether a native
transcript ``user_prompt`` element is a BA-injected internal worker prompt
(prefix/signature/regex markers) or genuine human input. Each test asserts a
distinguishing classification or normalization outcome, not just execution.
"""

from __future__ import annotations

import pytest

import native_internal_prompt as nip
from native_internal_prompt import (
    is_internal_import_prompt,
    normalize_import_prompt,
)

# ---------------------------------------------------------------------------
# normalize_import_prompt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   ", "\n\t  "])
def test_normalize_falsy_or_whitespace_collapses_to_empty(raw):
    assert normalize_import_prompt(raw) == ""


def test_normalize_plain_text_without_suffix_is_unchanged():
    text = "How do I deploy this service to production?"
    assert normalize_import_prompt(text) == text


def test_normalize_strips_at_single_injected_suffix():
    raw = "<worker-prep>run the thing\n\n# global preferences\n## Keep it short"
    assert normalize_import_prompt(raw) == "<worker-prep>run the thing"


def test_normalize_picks_the_earliest_of_multiple_suffixes():
    # Two distinct suffix markers; the earlier one in the text must win.
    raw = (
        "real prefix\n"
        "the following injected context is from better agent, not from the user.\n"
        "noise\n"
        "# global preferences\n## Keep it short"
    )
    assert normalize_import_prompt(raw) == "real prefix"


def test_normalize_does_not_cut_suffix_only_input_to_empty():
    # The guard is `match.start() > 0`: a suffix with nothing before it must not
    # be cut to an empty string. (.strip() drops leading whitespace first, so the
    # \n-anchored suffix pattern never matches a suffix-only input either way.)
    raw = "# global preferences\n## Keep it short"
    assert normalize_import_prompt(raw) == raw


# ---------------------------------------------------------------------------
# is_internal_import_prompt — prefix, signature, regex branches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", nip._INTERNAL_IMPORT_PROMPT_PREFIXES)
def test_classifies_every_known_prefix_as_internal(prefix):
    assert is_internal_import_prompt(prefix + " do the work") is True


def test_prefix_must_be_at_start_embedded_prefix_is_external():
    # startswith semantics: a prefix buried mid-text is NOT internal.
    assert is_internal_import_prompt("explain <worker-prep> to me") is False


@pytest.mark.parametrize("signature", nip._INTERNAL_IMPORT_PROMPT_SIGNATURES)
def test_classifies_every_known_signature_as_internal(signature):
    assert is_internal_import_prompt("intro " + signature + " trailing") is True


@pytest.mark.parametrize(
    "prompt",
    [
        "adversarially review the diff below",
        "read-only adversarial validation of the change",
        "in /users/ofekron/better-claude, audit the auth surface",
        "please review the following git diff representing the change",
        "using the testape browser, navigate to the login page",
        "reply with exactly: testape_ok",
    ],
)
def test_classifies_regex_pattern_prompts_as_internal(prompt):
    assert is_internal_import_prompt(prompt) is True


def test_regex_match_is_case_insensitive():
    # Patterns compile with IGNORECASE.
    assert is_internal_import_prompt("ADVERSARIAL REVIEW the diff") is True


def test_plain_human_prompt_is_external():
    assert is_internal_import_prompt("Can you add a unit test for the parser?") is False


def test_none_prompt_is_external():
    assert is_internal_import_prompt(None) is False


# ---------------------------------------------------------------------------
# normalization + classification interaction
# ---------------------------------------------------------------------------


def test_internal_prefix_survives_suffix_stripping():
    # A genuine internal worker prompt that carries a trailing injected block
    # stays internal after the trailer is stripped.
    raw = "<machine-completion-prep>analyze\n\n# global preferences\n## Keep it short"
    assert is_internal_import_prompt(raw) is True


def test_external_prefix_with_injected_suffix_is_external():
    # A human question with a leaked injected trailer gets stripped to the human
    # question, which must classify as external (not internal-by-contamination).
    raw = "what does this error mean?\n\n# global preferences\n## Keep it short"
    assert is_internal_import_prompt(raw) is False
