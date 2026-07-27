"""Canonical list of provider KINDs a run can target.

Capability contexts carry one output per provider kind and
`capability_contexts.provider_capability_contexts` selects the entry matching
the runner's kind — so a kind missing from this list receives NO content at
all for that context. Every producer of per-kind outputs (`assistant_ui`'s
role prompt, `harness_profile_resolver`'s instruction blocks) reads the list
from here so the coverage cannot drift between them.

The live registry is the source; the fallback tuple keeps coverage complete
when the registry is unavailable (some test contexts) and anchors the sorted
result so capability_contexts hashes stay byte-stable across calls.
"""

from __future__ import annotations

FALLBACK_PROVIDER_KINDS = (
    "agy", "amp", "claude", "claude-remote", "codex", "copilot", "cursor",
    "fugu", "kimi", "openai", "opencode", "pi", "qwen",
)


def all_provider_kinds() -> list[str]:
    kinds: list[str] = []
    try:
        from provider import known_providers

        for prov in known_providers():
            kind = getattr(prov, "KIND", "")
            if isinstance(kind, str) and kind:
                kinds.append(kind)
    except Exception:  # noqa: BLE001 — registry unavailable in some test contexts
        kinds = []
    return sorted({*kinds, *FALLBACK_PROVIDER_KINDS})
