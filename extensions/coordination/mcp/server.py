from __future__ import annotations

from typing import Any

from better_agent_sdk import Client
from better_agent_sdk.surfaces import OperationSpec, build_mcp_server, run_mcp_or_cli


def lock_ops_response(
    key: str = "",
    keys: list[str] | None = None,
    release: bool = False,
    holder_token: str = "",
    timeout_seconds: float | int | None = None,
    lease_seconds: float | int | None = None,
    op: str = "",
    renew: bool = False,
    validate: bool = False,
    reattach: bool = False,
    owned: bool = False,
) -> dict[str, Any]:
    """Acquire, renew, validate, reattach, list, or release coordination locks."""
    key = (key or "").strip()
    normalized_keys = [str(item or "").strip() for item in keys or [] if str(item or "").strip()]
    normalized_op = (op or "").strip().lower().replace("-", "_")
    if not key and not normalized_keys and not owned and normalized_op not in {"list_owned", "release_owned"}:
        return {"success": False, "error": "key_required"}
    try:
        return Client().lock_ops(
            key,
            keys=normalized_keys or None,
            op=normalized_op,
            release=release,
            renew=renew,
            validate=validate,
            reattach=reattach,
            owned=owned,
            holder_token=holder_token,
            timeout_seconds=timeout_seconds,
            lease_seconds=lease_seconds,
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _specs() -> tuple[OperationSpec, ...]:
    return (
        OperationSpec(
            "lock_ops",
            lock_ops_response,
            operation="coordination_lock_ops",
        ),
    )


def build_server():
    return build_mcp_server("better-agent-coordination", _specs())


def main() -> int:
    return run_mcp_or_cli("better-agent-coordination", _specs())


if __name__ == "__main__":
    raise SystemExit(main())
