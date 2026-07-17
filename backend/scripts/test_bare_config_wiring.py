"""Wiring guard for the bare-config (TestApe-isolated) spawn path. These
assertions lock the cross-file plumbing that needs a full live backend to
exercise end-to-end:

  - runner honors an explicit empty `setting_sources` (no `or` collapse);
  - runner gates the user-facing extras (session-bridge / scheduler /
    open-file-panel / project-updates) OFF for bare, but keeps the
    credential broker for bare device workers;
  - provider_claude.start_run is the single chokepoint: it reads
    `bare_config` off the session record, forces `setting_sources=[]`,
    and promotes the top-level manager turn to runner `mode="manager"`;
  - the manager bootstrap is suppressed for bare sessions in BOTH the CLI
    override and `wrap_cli_prompt`;
  - delegation wires loopback creds for bare workers.

Run with:
    cd backend && .venv/bin/python scripts/test_bare_config_wiring.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + label)
        ok = ok and cond

    runner = (ROOT / "runner.py").read_text()
    extension_registry = (ROOT / "extension_registry.py").read_text()
    provider = (ROOT / "provider_claude.py").read_text()
    cli = (ROOT / "cli.py").read_text()
    native = (ROOT / "orchs" / "native" / "__init__.py").read_text()
    delegation = (ROOT / "orchs" / "manager" / "_delegation.py").read_text()

    # P1: explicit [] honored, the `or` collapse is gone.
    check(
        "runner honors explicit empty setting_sources (is None, not or)",
        '["user", "project", "local"] if _ss is None else _ss' in runner,
    )
    check(
        "runner no longer collapses setting_sources with `or`",
        'inputs.get("setting_sources") or ["user", "project", "local"]' not in runner,
    )

    # Runner bare gating.
    check("runner reads bare_config", '_bare = bool(inputs.get("bare_config", False))' in runner)
    check(
        "user-facing extras OFF for bare",
        "_user_facing_extras = open_file_panel_enabled and not _bare" in runner,
    )
    check(
        "credential broker enabled for bare (workers need it)",
        "_cred_enabled = open_file_panel_enabled or _bare" in runner,
    )
    check(
        "session-bridge gated behind _user_facing_extras",
        "active_builtin_mcp_extensions(" in runner
        and 'interacts_with_user=bool(_user_facing_extras and app_session_id)' in runner
    )
    check(
        "provider-config-sync comes from private extension runtime configs",
        'mcp_servers["provider-config-sync"] = provider_config_sync_mcp_server_config(' not in runner
        and "runtime_mcp_server_configs(" in runner,
    )
    check(
        "project-updates comes from private extension runtime configs",
        "runtime_mcp_server_configs(" in runner
        and '"project-updates"' not in extension_registry,
    )

    # provider_claude chokepoint.
    check("start_run reads session bare_config", '_bare = bool(_sess_rec.get("bare_config"))' in provider)
    check("bare forces empty setting_sources", "setting_sources = []" in provider)
    check("normal sessions pass native provider settings through", '"setting_sources": setting_sources,' in provider)
    check(
        "bare manager promoted to runner mode=manager",
        'mode = "manager"' in provider and "not is_worker" in provider,
    )
    check("bare_config written to runner input", '"bare_config": _bare,' in provider)

    # Bootstrap suppression for bare (both paths).
    check(
        "CLI override suppresses bootstrap for bare",
        'if session.get("bare_config"):' in cli and "return None" in cli,
    )
    check(
        "wrap_cli_prompt suppresses bootstrap for bare",
        'if session.get("bare_config"):' in native and "return prompt" in native,
    )

    # Delegation wires loopback creds for bare workers.
    check(
        "delegation enables loopback for bare workers",
        "missing_parent_should_run_direct(run_mode, worker_session)" in delegation
        and "worker_backend_url = os.environ.get(" in delegation
        and "worker_internal_token = coordinator.internal_token" in delegation,
    )

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
