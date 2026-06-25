from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from better_agent_sdk import Client


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


def build_server() -> FastMCP:
    server = FastMCP("better-agent-coordination")

    @server.tool()
    def lock_ops(
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
        return lock_ops_response(
            key, keys, release, holder_token, timeout_seconds, lease_seconds,
            op, renew, validate, reattach, owned,
        )

    return server


def main() -> int:
    build_server().run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
