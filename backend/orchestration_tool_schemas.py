from __future__ import annotations

from typing import Any

import provider_manifest


RUNTIME_PROFILE_RUNNER_IDS: tuple[str, ...] = tuple(dict.fromkeys(
    runner
    for kind in provider_manifest.runner_kinds()
    for runner in provider_manifest.runner_choices_for(kind)
))


def runtime_profile_runner_input_property(
    *,
    nullable: bool = False,
    description: str,
) -> dict[str, Any]:
    allowed_values: list[Any] = list(RUNTIME_PROFILE_RUNNER_IDS)
    if nullable:
        allowed_values.append(None)
    return {
        "type": ["string", "null"] if nullable else "string",
        "enum": allowed_values,
        "description": description,
    }


def normalize_runtime_profile_runner(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if type(value) is not str:
        raise ValueError("runner must be a string or null")
    runner = value.strip()
    if not runner:
        return None
    if runner not in RUNTIME_PROFILE_RUNNER_IDS:
        allowed = ", ".join(RUNTIME_PROFILE_RUNNER_IDS)
        raise ValueError(
            f"runner must be a runtime-profile runner ID; available: {allowed}",
        )
    return runner


SESSION_ORGANIZATION_INPUT_PROPERTIES: dict[str, Any] = {
    "folder_id": {
        "type": "string",
        "description": "OPTIONAL - folder id to assign only if a new session is created.",
    },
    "tag_ids": {
        "type": "array",
        "items": {"type": "string"},
        "description": "OPTIONAL - tag ids to assign only if a new session is created.",
    },
}


HARNESS_PROFILE_INPUT_PROPERTIES: dict[str, Any] = {
    "harness_profile_id": {
        "type": "string",
        "description": (
            "OPTIONAL - harness profile id applied to a newly-created session. "
            "The profile decides which instructions, skills, and MCP servers "
            "that session gets, and equally which ones it does NOT get. Omit "
            "to use the default profile."
        ),
    },
}

CREATE_SESSION_CWD_INPUT_PROPERTY: dict[str, Any] = {
    "cwd": {
        "type": ["string", "null"],
        "description": (
            "OPTIONAL - working directory for the new session. Defaults to "
            "(inherits) the creating session's cwd. Set this when targeting a "
            "node whose filesystem path differs from the creating node."
        ),
    },
}


def resolve_tool_cwd(args: dict[str, Any], inherited_cwd: str) -> str:
    return str(args.get("cwd") or "").strip() or inherited_cwd


def harness_profile_wire_fields(profile_id: Any = "") -> dict[str, Any]:
    """Normalize a tool's harness-profile argument into the wire field every
    session-creating backend route reads. Blank means "no explicit selection",
    which the route resolves to the default profile. A session names the
    profile, never a revision of it: the profile's current content is what
    every turn resolves, so an edit applies to sessions already on it."""
    return {"harness_profile_id": str(profile_id or "").strip() or None}


STOP_TURN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_session_id": {
            "type": "string",
            "description": "Better Agent session id whose current turn should be stopped.",
        },
    },
    "required": ["target_session_id"],
    "additionalProperties": False,
}


INBOX_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recipient_session_id": {
            "type": "string",
            "description": (
                "Existing Better Agent session to receive message. Required when "
                "sending; omit when reading your own inbox."
            ),
        },
        "message": {
            "type": "string",
            "maxLength": 10000,
            "description": (
                "Non-empty message to send. Omit both fields to read your own "
                "unread inbox."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
}


READ_INBOX_HISTORY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": {
            "type": "integer",
            "description": "Maximum messages to return, clamped to 1..200. Defaults to 50.",
        },
        "before_seq": {
            "type": ["integer", "null"],
            "description": "Return messages older than this sequence. Omit for newest history.",
        },
    },
    "required": [],
    "additionalProperties": False,
}


DELEGATE_TASK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "The task to hand off. Routed automatically unless target_session_id is set.",
        },
        "target_session_id": {
            "type": "string",
            "description": (
                "OPTIONAL - set ONLY to bypass auto-routing and send to a "
                "specific session. Omit to let the router pick (search or create)."
            ),
        },
        "provider_id": {
            "type": "string",
            "description": "OPTIONAL - provider for auto-routing search and newly-created target sessions. Omit to search across providers and create fallback targets with the caller's provider; pass a provider to constrain search/creation to it. ANY also searches across providers.",
        },
        "runtime_profile_id": {
            "type": "string",
            "description": "OPTIONAL - runtime profile for newly-created target sessions; supplies provider+runner (conflicting provider_id is rejected). Model/effort params still override its defaults.",
        },
        "model": {
            "type": "string",
            "description": "OPTIONAL - model for a newly-created target session. Defaults to the creating session's model.",
        },
        "reasoning_effort": {
            "type": "string",
            "description": "OPTIONAL - reasoning effort for a newly-created target session. Defaults to the creating session's effort.",
        },
        "runner": {
            "type": "string",
            "description": "OPTIONAL - runner for a newly-created target session. Defaults to the creating session's runner.",
        },
        "sub_session": {
            "type": "boolean",
            "description": "OPTIONAL - default true. If false, auto-created targets are standalone native sessions instead of hidden sub-sessions.",
        },
        "cwd": {
            "type": "string",
            "description": "OPTIONAL - working directory for a newly-created target session. Defaults to (inherits) the creating session's cwd. Ignored when delegating to an existing target_session_id.",
        },
        **SESSION_ORGANIZATION_INPUT_PROPERTIES,
        **HARNESS_PROFILE_INPUT_PROPERTIES,
    },
    "required": ["task"],
}


ENSURE_NAMED_WORKER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Stable worker name. Matched (case-sensitive) as the session "
                "name `worker:<name>` together with cwd — that (name, cwd) "
                "pair is the singleton key. Reuses the existing worker if "
                "present, otherwise creates one."
            ),
        },
        "cwd": {
            "type": "string",
            "description": (
                "OPTIONAL - registry cwd for the worker. Together with name it "
                "identifies the singleton. Defaults to (inherits) the creating "
                "session's cwd; set it only to target a different project root."
            ),
        },
        "orchestration_mode": {
            "type": "string",
            "enum": ["team", "native"],
            "description": (
                "'native' = a plain session that does work directly. 'team' = "
                "a sub-coordinator that can itself delegate to workers."
            ),
        },
        "provision_prompt": {
            "type": "string",
            "description": (
                "OPTIONAL first-turn prompt applied ONLY on first creation "
                "(ignored when reusing an existing worker). Use to seed the "
                "worker's role/expertise."
            ),
        },
        "description": {
            "type": "string",
            "description": "OPTIONAL human-readable description; defaults to `worker:<name>`.",
        },
        "provider_id": {
            "type": "string",
            "description": "OPTIONAL - provider for the worker. Defaults to the creating session's provider.",
        },
        "runtime_profile_id": {
            "type": "string",
            "description": "OPTIONAL - runtime profile for the worker; supplies provider+runner. Model/effort params still override its defaults.",
        },
        "model": {
            "type": "string",
            "description": "OPTIONAL - model for the worker. Defaults to the creating session's model.",
        },
        "reasoning_effort": {
            "type": "string",
            "description": "OPTIONAL - reasoning effort for the worker. Defaults to the creating session's effort.",
        },
        "runner": {
            "type": "string",
            "description": "OPTIONAL - runner for the worker. Defaults to the creating session's runner.",
        },
        "node_id": {
            "type": "string",
            "description": "OPTIONAL - worker node id. Defaults to the session's node_id.",
        },
        **SESSION_ORGANIZATION_INPUT_PROPERTIES,
        **HARNESS_PROFILE_INPUT_PROPERTIES,
    },
    "required": ["name", "orchestration_mode"],
}


LIST_AVAILABLE_PROVIDER_MODELS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "provider": {
            "type": "string",
            "description": "Optional fuzzy provider id, name, or kind filter.",
        },
        "model": {
            "type": "string",
            "description": "Optional fuzzy model filter.",
        },
        "reasoning_effort": {
            "type": "string",
            "description": "Optional fuzzy reasoning effort filter.",
        },
        "runner": {
            "type": "string",
            "description": "Optional fuzzy runner filter.",
        },
    },
}
