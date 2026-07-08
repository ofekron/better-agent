"""Detects BA-internal (injected) worker prompts in native transcripts.

A native transcript's `user_prompt` elements are all role=user, but BA injects
many of them for internal workers (machine-completion, search, file-editor,
adversarial review, testape, etc.). These workers often spawn providers with
no durable run/session record, so they can't be classified via the runs dir.
Their prompt text carries reliable BA-defined markers (`<machine-completion-prep>`,
`<search-worker-provision>`, …), so detecting those tags at ingestion is the
authoritative signal that a session is internal — not direct human usage.

Kept dependency-free (only `re`) so the native transcript index worker can
import it without pulling in session_manager or other heavy modules.
"""

from __future__ import annotations

import re
from typing import Optional

_INTERNAL_IMPORT_PROMPT_SIGNATURES = (
    "Better Agent run.sh startup checker",
    "startup checker for Z.AI",
    "direct Claude Code CLI process configured for the Z.AI Claude-compatible provider",
    "machine completion worker for the requirement-analysis pipeline",
    "You are an adversarial reviewer for Better Agent",
    "You are a HOSTILE adversarial code reviewer.",
    "You are adversarial reviewer for a Better Agent RCA.",
    "You are worker:testape",
    "Better Agent requires a parent-session reply after subagent work.",
)

_INTERNAL_IMPORT_PROMPT_PREFIXES = (
    "<self>",
    "<worker-prep>",
    "<machine-completion-prep>",
    "<search-worker-provision>",
    "<get-requirements-processor-prep>",
    "<file-editor-provision>",
    "<project-structure-maintainer-provision>",
    "<verdict-prompt>",
    "<command-name>",
    "<system_bootstrap>",
)

_INTERNAL_IMPORT_PROMPT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"^use these selected capabilities for this run only\.",
        r"^the following injected context is from better agent, not from the user\.",
        r"^---\s*name:\s*test-ui-expert\b",
        r"^=== your workspace ===",
        r"^you are a technical analysis expert and a super ninja trader\b",
        r"^(adversarial(ly)? (re-)?review|read-only adversarial|final adversarial review|second adversarial review)",
        r"^you are a hostile adversarial code reviewer\b",
        r"^use hostile adversarial review stance",
        r"^#\s*testape/[a-z0-9_/-]+\b",
        r"^▶\s*👤\s*user\b",
        r"^in /users/[^,\n]+,\s*adversarially review",
        r"^(read-only:|read-only adversarial validation|in /users/[^,\n]+,\s*read-only:|in /users/[^,\n]+,\s*audit\b)",
        r"^please review the following git diff representing",
        r"^investigate this testape product bug\b.*\breturn commit-ready facts",
        r"you are the dedicated testape ui-testing expert",
        r"^using the testape\b",
        r"^(navigate to|open) https?://.*--- you are the dedicated testape ui-testing expert",
        r"^(a device worker|a sign-in form has two fields|audit this testape learned state graph)",
        r"^(convert these verified discoveries|analyze these detector/run measurements|preserve observed analytics confirmations)",
        r"^runtime ui test only\. do not inspect files\.",
        r"^reply with exactly:\s*testape_ok$",
    )
)

_INJECTED_PROMPT_SUFFIX_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"\n\s*# global preferences\s*\n\s*## ",
        r"\n\s*# agents\.md instructions for /users/",
        r"\n\s*use these selected capabilities for this run only\. they are active context",
        r"\n\s*the following injected context is from better agent, not from the user\.",
    )
)


def normalize_import_prompt(prompt: str) -> str:
    text = (prompt or "").strip()
    cut_at: Optional[int] = None
    for pattern in _INJECTED_PROMPT_SUFFIX_PATTERNS:
        match = pattern.search(text)
        if match and match.start() > 0:
            cut_at = match.start() if cut_at is None else min(cut_at, match.start())
    if cut_at is None:
        return text
    return text[:cut_at].rstrip()


def is_internal_import_prompt(prompt: str) -> bool:
    text = normalize_import_prompt(prompt)
    return (
        text.startswith(_INTERNAL_IMPORT_PROMPT_PREFIXES)
        or any(sig in text for sig in _INTERNAL_IMPORT_PROMPT_SIGNATURES)
        or any(pattern.search(text) for pattern in _INTERNAL_IMPORT_PROMPT_PATTERNS)
    )
