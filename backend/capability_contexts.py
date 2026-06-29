from __future__ import annotations

import re
from typing import Any, Optional

from prompt_templates import render_prompt

MAX_CAPABILITY_CONTEXTS = 20
MAX_CAPABILITY_OUTPUTS = 20
MAX_CAPABILITY_CONTENT_CHARS = 200_000
MAX_CAPABILITY_FIELD_CHARS = 1_000
_SOURCE_HEADING_TOKEN_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


def _clean_text(value: Any, field: str, *, required: bool = True, max_chars: int = MAX_CAPABILITY_FIELD_CHARS) -> str:
    if not isinstance(value, str):
        if required:
            raise ValueError(f"{field} must be a string")
        return ""
    text = value.strip()
    if required and not text:
        raise ValueError(f"{field} must be non-empty")
    if len(text) > max_chars:
        raise ValueError(f"{field} is too large")
    return text


def normalize_capability_contexts(value: Any) -> list[dict]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("capability_contexts must be a list")
    if len(value) > MAX_CAPABILITY_CONTEXTS:
        raise ValueError("capability_contexts has too many entries")

    normalized: list[dict] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"capability_contexts[{index}] must be an object")
        outputs = item.get("outputs")
        if not isinstance(outputs, list):
            raise ValueError(f"capability_contexts[{index}].outputs must be a list")
        if len(outputs) > MAX_CAPABILITY_OUTPUTS:
            raise ValueError(f"capability_contexts[{index}].outputs has too many entries")

        clean_outputs: list[dict] = []
        for output_index, output in enumerate(outputs):
            if not isinstance(output, dict):
                raise ValueError(f"capability_contexts[{index}].outputs[{output_index}] must be an object")
            content = _clean_text(
                output.get("content"),
                f"capability_contexts[{index}].outputs[{output_index}].content",
                max_chars=MAX_CAPABILITY_CONTENT_CHARS,
            )
            clean_outputs.append({
                "provider_kind": _clean_text(output.get("provider_kind"), f"capability_contexts[{index}].outputs[{output_index}].provider_kind"),
                "provider_name": _clean_text(output.get("provider_name"), f"capability_contexts[{index}].outputs[{output_index}].provider_name", required=False),
                "content_kind": _clean_text(output.get("content_kind"), f"capability_contexts[{index}].outputs[{output_index}].content_kind", required=False),
                "content": content,
            })

        if not clean_outputs:
            continue
        normalized.append({
            "source_id": _clean_text(item.get("source_id"), f"capability_contexts[{index}].source_id"),
            "capability_id": _clean_text(item.get("capability_id"), f"capability_contexts[{index}].capability_id"),
            "name": _clean_text(item.get("name"), f"capability_contexts[{index}].name"),
            "category": _clean_text(item.get("category"), f"capability_contexts[{index}].category", required=False),
            "outputs": clean_outputs,
        })
    return normalized


def provider_capability_contexts(
    contexts: Optional[list[dict]],
    provider_kind: str,
) -> list[dict]:
    selected: list[dict] = []
    for item in contexts or []:
        if not isinstance(item, dict):
            continue
        outputs = item.get("outputs")
        if not isinstance(outputs, list):
            continue
        output = next(
            (
                candidate
                for candidate in outputs
                if isinstance(candidate, dict)
                and candidate.get("provider_kind") == provider_kind
                and isinstance(candidate.get("content"), str)
                and candidate.get("content")
            ),
            None,
        )
        if output is None:
            continue
        selected.append({
            "name": str(item.get("name") or item.get("capability_id") or output.get("label") or "Capability"),
            "category": str(item.get("category") or ""),
            "provider_kind": provider_kind,
            "content_kind": str(output.get("content_kind") or ""),
            "content": output["content"],
        })
    return selected


def render_capability_context(contexts: Optional[list[dict]]) -> str:
    blocks: list[str] = []
    for item in contexts or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        name = str(item.get("name") or "Capability")
        category = str(item.get("category") or "capability")
        blocks.append(f"## {name} ({category})\n\n{content.strip()}")
    if not blocks:
        return ""
    return render_prompt(
        "runner/capability_context.md",
        {"blocks": "\n\n".join(blocks)},
    )


def prompt_heading_for_source(source: Any) -> str:
    if source == "team_message":
        return "Message"
    if source == "team_ask":
        return "Ask"
    if isinstance(source, str) and source.strip():
        token = _SOURCE_HEADING_TOKEN_RE.sub("_", source.strip()).strip("_")[:80]
        return f"Injected prompt ({token})" if token else "Injected prompt"
    return "User prompt"


def prepend_capability_context(prompt: str, inputs: dict) -> str:
    context = render_capability_context(inputs.get("capability_contexts") or [])
    if not context:
        return prompt
    if not prompt:
        return context
    return f"{context}\n\n## {prompt_heading_for_source(inputs.get('source'))}\n\n{prompt}"
