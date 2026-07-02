"""Typed loopback client for integration subprocesses.

Mirrors core's ``/api/internal/*`` surface (the same endpoints the built-in
extension MCP calls) behind one authenticated client. Each method returns
the parsed JSON body core sends; logical failures arrive as ``{"success":
False, "error": ...}`` over HTTP 200, while transport/auth failures raise
:class:`BetterAgentError`.
"""
from __future__ import annotations

import time
import json
import os
import base64
import urllib.error
import urllib.request
from typing import Any

_LONG_TIMEOUT = 24 * 60 * 60
_UNSET = object()


def _agent_env_name(name: str) -> str:
    if name.startswith("BETTER_CLAUDE_"):
        return "BETTER_AGENT_" + name.removeprefix("BETTER_CLAUDE_")
    return name


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(_agent_env_name(name), "") or os.environ.get(name, "") or default).strip()


class BetterAgentError(RuntimeError):
    """Raised when core is unreachable or returns a non-JSON / non-2xx response."""


def _http_error_message(exc: "urllib.error.HTTPError") -> str:
    """Surface the core's error ``detail`` instead of the opaque HTTP reason.

    FastAPI returns ``{"detail": "..."}`` on 4xx/5xx; without this the caller
    only sees ``HTTP 400: Bad Request`` and cannot tell what was rejected.
    Falls back to the HTTP reason when the body is missing or not JSON."""
    detail = ""
    try:
        body = exc.read()
        if body:
            parsed = json.loads(body.decode("utf-8"))
            raw = parsed.get("detail") if isinstance(parsed, dict) else parsed
            detail = raw if isinstance(raw, str) else (json.dumps(raw) if raw else "")
    except Exception:
        detail = ""
    suffix = f": {detail}" if detail else f": {exc.reason}"
    return f"core returned HTTP {exc.code}{suffix}"


class Client:
    """One core loopback client. kwargs override env-derived defaults."""

    def __init__(self, **overrides: Any) -> None:
        self.backend_url = (
            overrides.get("backend_url") or _env("BETTER_CLAUDE_BACKEND_URL") or "http://localhost:8000"
        ).rstrip("/")
        self.internal_token = overrides.get("internal_token") or _env("BETTER_CLAUDE_INTERNAL_TOKEN")
        self.app_session_id = overrides.get("app_session_id") or _env("BETTER_CLAUDE_APP_SESSION_ID")
        self.cwd = overrides.get("cwd") or _env("BETTER_CLAUDE_CWD")
        self.extension_id = overrides.get("extension_id") or _env("BETTER_CLAUDE_EXTENSION_ID")
        self.model = overrides.get("model") or _env("BETTER_CLAUDE_MODEL")
        self.provider_id = overrides.get("provider_id") or _env("BETTER_CLAUDE_PROVIDER_ID")

    def _headers(self) -> dict[str, str]:
        # Identity is carried by the per-extension internal token alone: the
        # backend derives WHICH extension is calling from X-Internal-Token. There
        # is no identity header to send — you cannot act as another extension.
        headers = {"Content-Type": "application/json"}
        if self.internal_token:
            headers["X-Internal-Token"] = self.internal_token
        return headers

    def _post(self, path: str, payload: dict[str, Any], *, timeout: float = 60.0) -> dict[str, Any]:
        if not self.internal_token:
            raise BetterAgentError("BETTER_AGENT_INTERNAL_TOKEN or BETTER_CLAUDE_INTERNAL_TOKEN is required")
        request = urllib.request.Request(
            self.backend_url + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise BetterAgentError(_http_error_message(exc)) from exc
        except urllib.error.URLError as exc:
            raise BetterAgentError(f"core unreachable: {exc.reason}") from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BetterAgentError("core returned a non-JSON response") from exc

    def _get(self, path: str, *, timeout: float = 60.0) -> dict[str, Any]:
        request = urllib.request.Request(
            self.backend_url + path,
            method="GET",
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise BetterAgentError(_http_error_message(exc)) from exc
        except urllib.error.URLError as exc:
            raise BetterAgentError(f"core unreachable: {exc.reason}") from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BetterAgentError("core returned a non-JSON response") from exc

    # ── right panel ──────────────────────────────────────────────────
    def set_right_panel(
        self,
        *,
        open: bool | None = None,
        tab: str | None = None,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Open/close the right panel and/or switch its active tab.

        ``tab`` must match a valid tab id registered in core
        (e.g. ``"files"``, ``"canvas"``, ``"screen"``)."""
        sid = session_id or self.app_session_id
        if not sid:
            raise BetterAgentError("set_right_panel requires app_session_id")
        body: dict[str, Any] = {}
        if open is not None:
            body["open"] = bool(open)
        if tab is not None:
            body["tab"] = tab
        if not body:
            raise BetterAgentError("set_right_panel requires at least one of open/tab")
        return self._post(f"/api/internal/sessions/{sid}/right-panel", body, timeout=10.0)

    # ── team definitions / runtime teams ─────────────────────────────
    def list_team_definitions(self) -> dict[str, Any]:
        return self._post(
            "/api/internal/team-definitions/list",
            {},
            timeout=10.0,
        )

    def plan_team_definition(
        self,
        source_id: str,
        profile: str,
        team_instance_id: str,
        variables: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/team-definitions/plan",
            {
                "source_id": source_id,
                "profile": profile,
                "team_instance_id": team_instance_id,
                "variables": variables or {},
            },
            timeout=10.0,
        )

    def create_team(
        self,
        root_session_id: str,
        *,
        definition_ref: str = "",
        profile: str = "",
        team_id: str = "",
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/teams/create",
            {
                "root_session_id": root_session_id,
                "definition_ref": definition_ref,
                "profile": profile,
                "team_id": team_id,
            },
            timeout=10.0,
        )

    def register_team_member(
        self,
        team_instance_id: str,
        member_id: str,
        member_type: str,
        agent_session_id: str,
        role: str,
        **metadata: Any,
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/teams/register-member",
            {
                "team_instance_id": team_instance_id,
                "member_id": member_id,
                "member_type": member_type,
                "agent_session_id": agent_session_id,
                "role": role,
                **metadata,
            },
            timeout=10.0,
        )

    def provision_workers(
        self,
        cwd: str,
        workers: list[dict[str, Any]],
        *,
        team_instance_id: str = "",
        bare_config: bool = False,
        timeout: float = _LONG_TIMEOUT,
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/workers/provision",
            {
                "cwd": cwd,
                "workers": workers,
                "team_instance_id": team_instance_id,
                "bare_config": bare_config,
            },
            timeout=timeout,
        )

    def start_team_activation(
        self,
        root_session_id: str,
        *,
        plan: dict[str, Any] | None = None,
        source_id: str = "",
        profile: str = "",
        team_instance_id: str = "",
        variables: dict[str, str] | None = None,
        cwd: str = "",
        bare_config: bool = False,
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/team-definitions/activate",
            {
                "root_session_id": root_session_id,
                "plan": plan,
                "source_id": source_id,
                "profile": profile,
                "team_instance_id": team_instance_id,
                "variables": variables or {},
                "cwd": cwd or self.cwd,
                "bare_config": bare_config,
            },
            timeout=10.0,
        )

    def get_team_activation(self, activation_id: str) -> dict[str, Any]:
        return self._get(f"/api/internal/team-definitions/activate/{activation_id}", timeout=10.0)

    def wait_team_activation(
        self,
        activation_id: str,
        *,
        poll_interval: float = 0.5,
        timeout: float = _LONG_TIMEOUT,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            result = self.get_team_activation(activation_id)
            activation = result.get("activation") if isinstance(result, dict) else None
            if isinstance(activation, dict):
                status = activation.get("status")
                if status == "complete":
                    return result
                if status == "failed":
                    error = activation.get("error") or activation.get("message") or "team activation failed"
                    raise BetterAgentError(str(error))
            if time.monotonic() >= deadline:
                raise BetterAgentError(f"team activation timed out: {activation_id}")
            time.sleep(max(0.05, poll_interval))

    def activate_team_definition(
        self,
        root_session_id: str,
        *,
        plan: dict[str, Any] | None = None,
        source_id: str = "",
        profile: str = "",
        team_instance_id: str = "",
        variables: dict[str, str] | None = None,
        cwd: str = "",
        bare_config: bool = False,
        wait: bool = True,
        poll_interval: float = 0.5,
        timeout: float = _LONG_TIMEOUT,
    ) -> dict[str, Any]:
        result = self.start_team_activation(
            root_session_id,
            plan=plan,
            source_id=source_id,
            profile=profile,
            team_instance_id=team_instance_id,
            variables=variables,
            cwd=cwd,
            bare_config=bare_config,
        )
        if not wait:
            return result
        activation = result.get("activation") if isinstance(result, dict) else None
        activation_id = str((activation or {}).get("id") or "")
        if not activation_id:
            raise BetterAgentError("core did not return a team activation id")
        return self.wait_team_activation(activation_id, poll_interval=poll_interval, timeout=timeout)

    def create_session(
        self,
        name: str,
        cwd: str,
        *,
        orchestration_mode: str = "native",
        sender_session_id: str = "",
        node_id: str = "",
        provider_id: str = "",
        model: str = "",
        reasoning_effort: str = "",
        bare_config: bool = False,
        capability_contexts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/create-session",
            {
                "name": name,
                "cwd": cwd,
                "orchestration_mode": orchestration_mode,
                "sender_session_id": sender_session_id,
                "node_id": node_id,
                "provider_id": provider_id,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "bare_config": bare_config,
                "capability_contexts": capability_contexts or [],
            },
            timeout=10.0,
        )

    # ── provisioned sessions ──────────────────────────────────────────
    def create_provisioned_session(
        self, spec_key: str, query: str, ctx: dict[str, Any] | None = None, *, timeout: float = _LONG_TIMEOUT
    ) -> dict[str, Any]:
        """Run one provisioned-session fork for a registered spec.

        Requires the calling extension to declare the ``spawn_runs`` permission.
        """
        return self._post(
            "/api/internal/provisioned-sessions",
            {"spec_key": spec_key, "query": query, "ctx": ctx or {}},
            timeout=timeout,
        )

    def create_inline_provisioned_session(
        self,
        inline_spec: dict[str, Any],
        query: str = "",
        ctx: dict[str, Any] | None = None,
        *,
        timeout: float = _LONG_TIMEOUT,
    ) -> dict[str, Any]:
        """Run one provisioned-session fork for an extension-owned inline spec."""
        return self._post(
            "/api/internal/provisioned-sessions",
            {"inline_spec": inline_spec, "query": query, "ctx": ctx or {}},
            timeout=timeout,
        )

    # ── extension settings ────────────────────────────────────────────
    def get_settings(self) -> dict[str, Any]:
        """Read this extension's own declared settings (manifest
        ``entrypoints.settings``). Secrets are resolved server-side from the
        keychain; nothing sensitive transits the environment."""
        return self._post("/api/internal/extension-settings", {}, timeout=10.0)

    def get_setting(self, key: str) -> dict[str, Any]:
        return self._post("/api/internal/extension-settings", {"key": key}, timeout=10.0)

    # ── coordination ─────────────────────────────────────────────────
    def lock_ops(
        self,
        key: str,
        *,
        keys: list[str] | None = None,
        release: bool = False,
        holder_token: str = "",
        timeout_seconds: float | int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"key": key, "release": release, "holder_token": holder_token}
        if keys is not None:
            body["keys"] = keys
        if timeout_seconds is not None:
            body["timeout_seconds"] = timeout_seconds
        return self._post(
            "/api/internal/coordination/lock-ops",
            body,
            timeout=max(10.0, float(timeout_seconds or 0) + 5.0),
        )

    # ── session bridge ────────────────────────────────────────────────
    def search_sessions(self, query: str, limit: int = 5, *, timeout: float = 15 * 60 + 30) -> dict[str, Any]:
        return self._post(
            "/api/internal/session-bridge/search",
            {"query": query, "limit": limit},
            timeout=timeout,
        )

    def recall_history(self, query: str, k: int = 5) -> dict[str, Any]:
        return self._post(
            "/api/internal/session-bridge/recall",
            {"app_session_id": self.app_session_id, "query": query, "k": k},
            timeout=100.0,
        )

    def delegate_to_session(
        self,
        prompt: str,
        run_mode: str,
        approval: str,
        *,
        session_id: str = "",
        display_prompt: str = "",
        source: str = "",
        client_id: str = "",
        provider_id: str = "",
        model: str = "",
        reasoning_effort: str = "",
        timeout: float = _LONG_TIMEOUT,
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/session-bridge/delegate",
            {
                "app_session_id": self.app_session_id,
                "session_id": session_id,
                "prompt": prompt,
                "display_prompt": display_prompt,
                "source": source,
                "client_id": client_id,
                "run_mode": run_mode,
                "approval": approval,
                "provider_id": provider_id,
                "model": model,
                "reasoning_effort": reasoning_effort,
            },
            timeout=timeout,
        )

    # ── session organization ─────────────────────────────────────────
    def get_session_organization(self, project_id: str = "") -> dict[str, Any]:
        return self._post(
            "/api/internal/session-organization/snapshot",
            {"project_id": project_id or None},
            timeout=10.0,
        )

    def query_sessions_by_organization(self, query: dict[str, Any]) -> dict[str, Any]:
        return self._post("/api/internal/session-organization/query", query, timeout=10.0)

    def create_session_folder(
        self,
        project_id: str,
        name: str,
        *,
        parent_folder_id: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/session-organization/create-folder",
            {
                "project_id": project_id,
                "name": name,
                "parent_folder_id": parent_folder_id,
            },
            timeout=10.0,
        )

    def update_session_folder(self, folder_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        return self._post(
            "/api/internal/session-organization/update-folder",
            {"folder_id": folder_id, "patch": patch},
            timeout=10.0,
        )

    def delete_session_folder(
        self,
        folder_id: str,
        *,
        mode: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/session-organization/delete-folder",
            {"folder_id": folder_id, "mode": mode},
            timeout=10.0,
        )

    def create_session_tag(
        self,
        name: str,
        *,
        project_id: str = "",
        color: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/session-organization/create-tag",
            {
                "name": name,
                "project_id": project_id or None,
                "color": color,
            },
            timeout=10.0,
        )

    def update_session_tag(self, tag_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        return self._post(
            "/api/internal/session-organization/update-tag",
            {"tag_id": tag_id, "patch": patch},
            timeout=10.0,
        )

    def delete_session_tag(self, tag_id: str) -> dict[str, Any]:
        return self._post(
            "/api/internal/session-organization/delete-tag",
            {"tag_id": tag_id},
            timeout=10.0,
        )

    def update_session_organization(
        self,
        session_id: str,
        *,
        folder_id: str | None | object = _UNSET,
        tag_ids: list[str] | None = None,
        add_tag_ids: list[str] | None = None,
        remove_tag_ids: list[str] | None = None,
        tag_source: str = "",
        sync_tag_source: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"session_id": session_id}
        if folder_id is not _UNSET:
            payload["folder_id"] = folder_id
        if tag_ids is not None:
            payload["tag_ids"] = tag_ids
        if add_tag_ids is not None:
            payload["add_tag_ids"] = add_tag_ids
        if remove_tag_ids is not None:
            payload["remove_tag_ids"] = remove_tag_ids
        if tag_source:
            payload["tag_source"] = tag_source
        if sync_tag_source:
            payload["sync_tag_source"] = sync_tag_source
        return self._post(
            "/api/internal/session-organization/update-session",
            payload,
            timeout=10.0,
        )

    def auto_tagging_action(self, action: str, **payload: Any) -> dict[str, Any]:
        return self._post(
            "/api/internal/auto-tagging",
            {"action": action, **payload},
            timeout=30.0,
        )

    # ── provisioned-session discovery ─────────────────────────────────
    def list_provisioned_specs(self) -> dict[str, Any]:
        """List the registered provisioned-session spec types this client may
        invoke via :meth:`create_provisioned_session`."""
        return self._get("/api/internal/provisioned-sessions/specs", timeout=10.0)

    # ── events ────────────────────────────────────────────────────────
    def broadcast_session_event(
        self, event_type: str, data: dict[str, Any] | None = None, *, session_id: str = ""
    ) -> dict[str, Any]:
        """Emit a per-session WebSocket event. ``source`` is pinned to this
        extension server-side; one extension cannot impersonate another."""
        return self._post(
            "/api/internal/broadcast-session",
            {
                "session_id": session_id or self.app_session_id,
                "event_type": event_type,
                "data": data or {},
            },
            timeout=10.0,
        )

    def publish_session_event(
        self, event_type: str, data: dict[str, Any] | None = None, *, session_id: str = ""
    ) -> dict[str, Any]:
        return self.broadcast_session_event(event_type, data, session_id=session_id)

    # ── extension-scoped storage ─────────────────────────────────────
    def storage_get(self, key: str) -> dict[str, Any]:
        return self._post("/api/internal/extension-storage/get", {"key": key}, timeout=10.0)

    def storage_get_bytes(self, key: str) -> bytes | None:
        result = self.storage_get(key)
        if not result.get("found"):
            return None
        return base64.b64decode(str(result.get("value_base64") or ""), validate=True)

    def storage_put(self, key: str, value: bytes | str) -> dict[str, Any]:
        raw = value.encode("utf-8") if isinstance(value, str) else value
        return self._post(
            "/api/internal/extension-storage/put",
            {"key": key, "value_base64": base64.b64encode(raw).decode("ascii")},
            timeout=10.0,
        )

    def storage_delete(self, key: str) -> dict[str, Any]:
        return self._post("/api/internal/extension-storage/delete", {"key": key}, timeout=10.0)

    # ── session-message mutation (extension-owned sessions) ───────────
    def append_session_message(
        self,
        role: str,
        content: str,
        *,
        session_id: str = "",
        message_id: str = "",
        timestamp: str = "",
        is_streaming: bool = False,
    ) -> dict[str, Any]:
        """Append a user/assistant message to a session this extension created.
        Ownership is claimed at creation and bound to the caller's per-extension
        token identity (not a header), so only the creating extension can mutate
        the session."""
        return self._post(
            "/api/internal/session-messages/append",
            {
                "session_id": session_id or self.app_session_id,
                "role": role,
                "content": content,
                "message_id": message_id,
                "timestamp": timestamp,
                "is_streaming": is_streaming,
            },
            timeout=10.0,
        )

    def update_session_message_content(
        self, message_id: str, content: str, *, session_id: str = ""
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/session-messages/update-content",
            {"session_id": session_id or self.app_session_id, "message_id": message_id, "content": content},
            timeout=10.0,
        )

    def set_session_message_streaming(
        self, message_id: str, streaming: bool, *, session_id: str = ""
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/session-messages/set-streaming",
            {"session_id": session_id or self.app_session_id, "message_id": message_id, "streaming": streaming},
            timeout=10.0,
        )

    # ── scoped session-field access (declared, non-owned) ───────────
    def get_session_fields(
        self, fields: list[str] | None = None, *, session_id: str = ""
    ) -> dict[str, Any]:
        """Read session-record fields this extension declared under
        ``permissions.reads_session_fields``."""
        return self._post(
            "/api/internal/session-fields",
            {"session_id": session_id or self.app_session_id, "fields": fields or []},
            timeout=10.0,
        )

    def update_session_field(
        self, field: str, value: Any, *, session_id: str = ""
    ) -> dict[str, Any]:
        """Mutate a session-record field this extension declared under
        ``permissions.mutates_session_fields`` — e.g. an externalized
        supervisor stamping a verdict on a session it didn't create. Core
        enforces the per-extension allowlist and routes to the matching
        session_manager setter."""
        return self._post(
            "/api/internal/session-field",
            {"session_id": session_id or self.app_session_id, "field": field, "value": value},
            timeout=10.0,
        )

    # ── virtual sessions (extension-owned projections) ───────────────
    def _virtual_session_id(self, session_id: str) -> str:
        if session_id and not session_id.startswith("virtual:") and self.extension_id:
            return f"virtual:{self.extension_id}:{session_id}"
        return session_id

    def upsert_virtual_session(
        self,
        session_id: str,
        *,
        name: str,
        messages: list[dict[str, Any]] | None = None,
        backing_session_ids: list[str] | None = None,
        cwd: str = "",
        model: str = "",
        provider_id: str = "",
        node_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        virtual_id = self._virtual_session_id(session_id)
        return self._post(
            "/api/internal/virtual-sessions/upsert",
            {
                "id": virtual_id,
                "name": name,
                "synthetic_messages": messages or [],
                "backing_session_ids": backing_session_ids or [],
                "cwd": cwd or self.cwd,
                "model": model or self.model,
                "provider_id": provider_id,
                "node_id": node_id,
                "metadata": metadata or {},
            },
            timeout=10.0,
        )

    def append_virtual_session_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        message_id: str = "",
        timestamp: str = "",
        is_streaming: bool = False,
        backing_session_id: str = "",
        backing_message_id: str = "",
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/virtual-sessions/append-message",
            {
                "session_id": self._virtual_session_id(session_id),
                "message": {
                    "id": message_id,
                    "role": role,
                    "content": content,
                    "timestamp": timestamp,
                    "is_streaming": is_streaming,
                    "backing_session_id": backing_session_id,
                    "backing_message_id": backing_message_id,
                },
            },
            timeout=10.0,
        )

    def delete_virtual_session(self, session_id: str) -> dict[str, Any]:
        return self._post(
            "/api/internal/virtual-sessions/delete",
            {"session_id": self._virtual_session_id(session_id)},
            timeout=10.0,
        )

    def inject_synthetic_message(
        self,
        session_id: str,
        prompt: str,
        *,
        display_prompt: str = "",
        source: str = "synthetic",
        client_id: str = "",
        model: str = "",
        cwd: str = "",
        orchestration_mode: str = "",
        capability_contexts: list[dict[str, Any]] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/synthetic-messages/inject",
            {
                "session_id": session_id,
                "prompt": prompt,
                "display_prompt": display_prompt,
                "source": source,
                "client_id": client_id,
                "model": model or self.model,
                "cwd": cwd or self.cwd,
                "orchestration_mode": orchestration_mode,
                "capability_contexts": capability_contexts or [],
            },
            timeout=timeout,
        )

    def run_managed(
        self,
        managed_session_id: str,
        prompt: str,
        *,
        parent_session_id: str = "",
        init_prompt: str = "",
        agent_sid: str = "",
        event_prefix: str = "managed_run",
        extra_env: dict[str, str] | None = None,
        model: str = "",
        cwd: str = "",
        timeout: float = _LONG_TIMEOUT,
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/managed-runs/run",
            {
                "managed_session_id": managed_session_id,
                "parent_session_id": parent_session_id or self.app_session_id,
                "prompt": prompt,
                "init_prompt": init_prompt,
                "agent_sid": agent_sid,
                "event_prefix": event_prefix,
                "extra_env": extra_env or {},
                "model": model or self.model,
                "cwd": cwd or self.cwd,
            },
            timeout=timeout,
        )

    def create_managed_session(
        self,
        name: str,
        *,
        parent_session_id: str = "",
        cwd: str = "",
        model: str = "",
        provider_id: str = "",
        node_id: str = "",
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/managed-runs/create-session",
            {
                "name": name,
                "parent_session_id": parent_session_id or self.app_session_id,
                "cwd": cwd or self.cwd,
                "model": model or self.model,
                "provider_id": provider_id or self.provider_id,
                "node_id": node_id,
            },
            timeout=timeout,
        )

    # ── delegation & team messaging ───────────────────────────────────
    def create_sub_session(
        self,
        sender_session_id: str,
        *,
        description: str = "",
        provider_id: str = "",
        model: str = "",
        reasoning_effort: str = "",
        cwd: str = "",
        node_id: str = "",
        disallowed_tools: list[str] | None = None,
        disabled_builtin_extensions: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/create-sub-session",
            {
                "sender_session_id": sender_session_id,
                "description": description,
                "provider_id": provider_id,
                "model": model or self.model,
                "reasoning_effort": reasoning_effort,
                "cwd": cwd or self.cwd,
                "node_id": node_id,
                "disallowed_tools": disallowed_tools,
                "disabled_builtin_extensions": disabled_builtin_extensions,
            },
            timeout=10.0,
        )

    def resolve_internal_llm(self, task_key: str) -> dict[str, Any]:
        return self._post(
            "/api/internal/extension-internal-llm/resolve",
            {"task_key": task_key},
            timeout=10.0,
        )

    def delegate_task(
        self,
        task: str,
        *,
        sender_session_id: str = "",
        target_session_id: str | None = None,
        provider_id: str = "",
        model: str = "",
        reasoning_effort: str = "",
        sub_session: bool = True,
        cwd: str = "",
        run_mode: str = "direct",
    ) -> dict[str, Any]:
        """Smart delegation router — resolves a target (auto-route or create)
        and dispatches fire-and-forget."""
        return self._post(
            "/api/internal/delegate-task",
            {
                "sender_session_id": sender_session_id or self.app_session_id,
                "task": task,
                "target_session_id": target_session_id,
                "provider_id": provider_id,
                "model": model or self.model,
                "reasoning_effort": reasoning_effort,
                "sub_session": sub_session,
                "cwd": cwd or self.cwd,
                "run_mode": run_mode,
            },
            timeout=30.0,
        )

    def ask_fork(
        self,
        instructions: str,
        worker_session_id: str,
        model: str,
        cwd: str,
        *,
        worker_description: str = "",
        justification: str = "",
        proposed_orchestration_mode: str = "",
        client_delegation_id: str = "",
        node_id: str = "",
        run_mode: str = "fork",
        worker_registry_cwd: str = "",
        ephemeral: bool = False,
        machine_completion: bool = False,
        provision_prompt: str = "",
        timeout: float = _LONG_TIMEOUT,
    ) -> dict[str, Any]:
        """Fork-mode delegation to a worker session; streams events back and
        returns aggregate results. Requires ``spawn_runs``."""
        return self._post(
            "/api/internal/ask-fork",
            {
                "app_session_id": self.app_session_id,
                "instructions": instructions,
                "worker_session_id": worker_session_id,
                "worker_description": worker_description,
                "model": model or self.model,
                "cwd": cwd or self.cwd,
                "justification": justification,
                "proposed_orchestration_mode": proposed_orchestration_mode,
                "client_delegation_id": client_delegation_id,
                "node_id": node_id,
                "run_mode": run_mode,
                "worker_registry_cwd": worker_registry_cwd,
                "ephemeral": ephemeral,
                "machine_completion": machine_completion,
                "provision_prompt": provision_prompt,
            },
            timeout=timeout,
        )

    def run_headless(
        self,
        prompt: str,
        *,
        cwd: str = "",
        session_id: str = "",
        resume_sid: str = "",
        fork: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """One-shot non-interactive provider run (``claude -p`` style) — a raw
        LLM result, not a managed turn. Fresh run by default; pass ``fork`` or
        ``resume_sid``/``session_id`` to continue/fork an existing session.
        Requires ``spawn_runs``. Used by externalized lifecycle extensions
        (e.g. rearranger) that need a raw result without touching the render tree."""
        payload: dict[str, Any] = {"prompt": prompt, "fork": fork}
        if cwd or self.cwd:
            payload["cwd"] = cwd or self.cwd
        if session_id:
            payload["session_id"] = session_id
        if resume_sid:
            payload["resume_sid"] = resume_sid
        if timeout is not None:
            payload["timeout"] = timeout
        return self._post("/api/internal/headless-run", payload, timeout=(timeout or 60.0) + 30.0)

    def ask(
        self,
        target_session_id: str = "",
        message: str = "",
        *,
        target_worker_id: str = "",
        target_worker_pool: str = "",
        pool_affinity_key: str = "",
        ask_id: str = "",
        provider_id: str = "",
        model: str = "",
        reasoning_effort: str = "",
        mode: str = "wait_and_grab_last_mssg_in_turn",
        timeout: float = _LONG_TIMEOUT,
    ) -> dict[str, Any]:
        """Send a message to one session, worker, or worker pool via ask mode."""
        return self._post(
            "/api/internal/ask",
            {
                "sender_session_id": self.app_session_id,
                "target_session_id": target_session_id,
                "target_worker_id": target_worker_id,
                "target_worker_pool": target_worker_pool,
                "pool_affinity_key": pool_affinity_key,
                "message": message,
                "ask_id": ask_id,
                "mode": mode,
                "provider_id": provider_id,
                "model": model,
                "reasoning_effort": reasoning_effort,
            },
            timeout=timeout,
        )

    def mssg(
        self,
        target_session_id: str = "",
        message: str = "",
        *,
        target_worker_id: str = "",
        target_worker_pool: str = "",
        pool_affinity_key: str = "",
        provider_id: str = "",
        model: str = "",
        reasoning_effort: str = "",
        collapse_key: str = "",
        collapse_policy: str = "",
    ) -> dict[str, Any]:
        """Send a fire-and-forget message to one session, worker, or worker pool."""
        return self._post(
            "/api/internal/mssg",
            {
                "sender_session_id": self.app_session_id,
                "target_session_id": target_session_id,
                "target_worker_id": target_worker_id,
                "target_worker_pool": target_worker_pool,
                "pool_affinity_key": pool_affinity_key,
                "message": message,
                "provider_id": provider_id,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "collapse_key": collapse_key,
                "collapse_policy": collapse_policy,
            },
            timeout=30.0,
        )

    def ask_propose(
        self, session_ids: list[str], *, reasoning: str = "", proposed_project_path: str = ""
    ) -> dict[str, Any]:
        """Stamp an inline session picker on the caller's in-flight assistant message."""
        return self._post(
            "/api/internal/ask-propose",
            {
                "caller_sid": self.app_session_id,
                "session_ids": session_ids,
                "reasoning": reasoning,
                "proposed_project_path": proposed_project_path,
            },
            timeout=30.0,
        )

    # ── UI panels / config ────────────────────────────────────────────
    def open_file_panel(
        self,
        path: str,
        *,
        mode: str = "panel",
        start_line: int | None = None,
        end_line: int | None = None,
        selected_start: int | None = None,
        selected_end: int | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/open-file-panel",
            {
                "app_session_id": self.app_session_id,
                "mode": mode,
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
                "selected_start": selected_start,
                "selected_end": selected_end,
            },
            timeout=10.0,
        )

    def request_user_input(
        self,
        questions: list[dict[str, Any]],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/api/internal/user-input/request",
            {
                "app_session_id": self.app_session_id,
                "questions": questions,
                "timeout_seconds": timeout_seconds,
            },
            timeout=timeout_seconds or 24 * 60 * 60,
        )

    def get_internal_llm(self) -> dict[str, Any]:
        """Read the internal-LLM task→model assignments (app settings)."""
        return self._get("/api/settings/internal-llm", timeout=10.0)

    # ── verb-preserving loopback (generic proxies) ───────────────────
    def request_internal(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        query: str = "",
        timeout: float = 60.0,
    ) -> tuple[int, bytes]:
        """Verb-preserving raw loopback to a core ``/api/internal/*`` endpoint.

        Unlike :meth:`call_internal` (POST-only, JSON in/out, body merged with
        ``app_session_id``), this preserves the HTTP method and query string and
        passes the raw body through untouched — for generic frontend proxies that
        forward arbitrary methods (GET/POST/PUT/PATCH/DELETE) to a core internal
        sub-surface. Reuses this client's base URL, internal-token auth, and
        extension-id headers so there is one transport. ``path`` MUST start with
        ``/api/internal/`` (rejected otherwise). Returns ``(status, raw_bytes)``;
        raises :class:`BetterAgentError` on transport/auth failure.
        """
        if not path.startswith("/api/internal/"):
            raise BetterAgentError("request_internal path must start with /api/internal/")
        if not self.internal_token:
            raise BetterAgentError("BETTER_AGENT_INTERNAL_TOKEN or BETTER_CLAUDE_INTERNAL_TOKEN is required")
        url = self.backend_url + path
        if query:
            url = f"{url}?{query}"
        request = urllib.request.Request(
            url,
            data=body,
            method=method.upper(),
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise BetterAgentError(f"core unreachable: {exc.reason}") from exc

    # ── core loopback substrate ──────────────────────────────────────
    def call_internal(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """POST to a core ``/api/internal/*`` endpoint — the loopback substrate
        extension-local typed wrappers build on. The shared SDK carries no
        feature methods; each extension owns its own typed surface on top of
        this primitive (:meth:`call_extension` reaches another extension's
        surface). ``app_session_id`` is injected so callers don't repeat it; a
        value already present in ``body`` wins. ``path`` MUST start with
        ``/api/internal/`` (rejected otherwise) so an extension addresses only
        core's internal surface, never an arbitrary URL. ``timeout`` overrides
        the default for long-running endpoints (e.g. get-requirements).
        """
        if not path.startswith("/api/internal/"):
            raise BetterAgentError("call_internal path must start with /api/internal/")
        payload = dict(body or {})
        payload.setdefault("app_session_id", self.app_session_id)
        return self._post(path, payload, timeout=timeout)

    # ── inter-extension calls ─────────────────────────────────────────
    def call_extension(
        self,
        target_extension_id: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        method: str = "POST",
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """Invoke another extension's exposed backend surface at ``path``
        (routed under that extension's ``/api/extensions/{id}/backend/*``).

        Extensions expose their own SDKs (feature-specific capabilities like
        requirements, scheduler, credentials live in per-extension surfaces);
        one extension reaches another through this generic primitive, so the
        shared SDK stays generic and the core never bakes in feature logic."""
        return self._post(
            "/api/internal/extension-call",
            {
                "target_extension_id": target_extension_id,
                "path": path,
                "method": method,
                "body": body or {},
            },
            timeout=timeout,
        )
