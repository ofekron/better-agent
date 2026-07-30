"""Single entrypoint for the PyInstaller-frozen macOS app bundle.

A frozen binary is `sys.executable`; the backend re-execs it to spawn
runner subprocesses, because a frozen app cannot run `python runner.py`.
This entrypoint inspects argv:
  - `--run-dir` present  → run the named runner in-process and exit.
  - `--communicate-mcp` present → run the stdio team-message MCP server.
  - `--capabilities-mcp` present → run the stdio capability-management MCP server.
  - `--open-file-panel-mcp` present → run the stdio file-panel MCP server.
  - `--open-config-panel-mcp` present → run the stdio config-panel MCP server.
  - `--extension-mcp` present → run an installed extension MCP launcher.
  - `--operation-cli` present → run the generated operation CLI dispatcher.
  - otherwise            → start the uvicorn server.

In a dev checkout the backend is launched via `run.sh`/`uvicorn` and the
runners via `python runner*.py` directly, so this module runs only
inside the frozen bundle. `_dispatch` is kept pure so it stays testable
without freezing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from env_compat import get_env


def _env_port(name: str, default: int) -> int:
    raw = get_env(name)
    if raw is None or not raw.strip():
        return default
    port = int(raw)
    if port < 1 or port > 65535:
        raise RuntimeError(f"{name} must be between 1 and 65535")
    return port


def _dispatch(
    argv: list[str],
) -> tuple[str, Optional[str], Optional[Path], Optional[str]]:
    """Classify argv. `--run-dir` present → ('runner', kind, run_dir,
    runner_module); `--serve-node` present → ('node_server', None, None,
    None). The desktop shell launches the primary server with the explicit
    `--serve` flag, but any non-runner invocation starts the server."""
    if "--communicate-mcp" in argv:
        return ("communicate_mcp", None, None, None)
    if "--capabilities-mcp" in argv:
        return ("capabilities_mcp", None, None, None)
    if "--open-file-panel-mcp" in argv:
        return ("open_file_panel_mcp", None, None, None)
    if "--open-config-panel-mcp" in argv:
        return ("open_config_panel_mcp", None, None, None)
    if "--extension-mcp" in argv:
        return ("extension_mcp", None, None, None)
    if "--operation-cli" in argv:
        return ("operation_cli", None, None, None)
    if "--frozen-artifact-smoke" in argv:
        return ("frozen_artifact_smoke", None, None, None)
    if "--serve-node" in argv:
        return ("node_server", None, None, None)
    if "--run-dir" not in argv:
        return ("server", None, None, None)
    import argparse
    import provider_manifest
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument(
        "--runner-kind", default="claude",
        choices=provider_manifest.runner_kinds(),
    )
    # Explicit module override: "claude" maps to two different runner
    # modules depending on the configured runner ("runner" native,
    # "runner_better_agent" subscription-via-Better-Agent-runner) — the
    # launcher (provider_runner_launch.py) already knows which one it
    # captured and passes it through rather than making this entrypoint
    # guess from --runner-kind alone. Absent (older/other callers) falls
    # back to the per-kind manifest default.
    parser.add_argument("--runner-module", default="")
    args = parser.parse_args(argv)
    return ("runner", args.runner_kind, args.run_dir, args.runner_module)


def _main(argv: Optional[list[str]] = None) -> int:
    mode, kind, run_dir, runner_module = _dispatch(sys.argv[1:] if argv is None else argv)
    if mode in {"server", "node_server"}:
        from resilient_stdio import protect_standard_streams
        protect_standard_streams()
    if mode == "communicate_mcp":
        from communicate_mcp import main as communicate_main
        return communicate_main()
    if mode == "capabilities_mcp":
        from capabilities_mcp import main as capabilities_main
        return capabilities_main()
    if mode == "open_file_panel_mcp":
        from open_file_panel_mcp import main as open_file_panel_main
        return open_file_panel_main()
    if mode == "open_config_panel_mcp":
        from open_config_panel_mcp import main as open_config_panel_main
        return open_config_panel_main()
    if mode == "extension_mcp":
        from extension_mcp_launcher import main as extension_mcp_main
        index = (sys.argv[1:] if argv is None else argv).index("--extension-mcp")
        return extension_mcp_main((sys.argv[1:] if argv is None else argv)[index + 1:])
    if mode == "operation_cli":
        from operation_cli import main as operation_cli_main
        values = sys.argv[1:] if argv is None else argv
        index = values.index("--operation-cli")
        return operation_cli_main(values[index + 1:])
    if mode == "frozen_artifact_smoke":
        from provider_frozen_artifact_smoke import main as artifact_smoke_main
        return artifact_smoke_main(
            sys.argv[1:] if argv is None else argv,
        )
    if mode == "runner":
        # Runner module per kind comes from the canonical manifest; "runner"
        # is the default Claude runner. (codex + fugu both resolve to
        # runner_codex; the launcher binary differs, not the runner.)
        # `runner_module` (from --runner-module) overrides that lookup when
        # the launcher passed one explicitly — needed because "claude" maps
        # to two different modules depending on the configured runner.
        import importlib
        import provider_manifest
        module = runner_module or provider_manifest.runner_module_for(kind)
        runner_main = importlib.import_module(module).main
        return runner_main(run_dir)
    if mode == "server":
        from backend_launch_authority import assert_primary_backend_launch_authorized
        assert_primary_backend_launch_authorized()
    import uvicorn
    from server_config import graceful_shutdown_timeout_seconds

    timeout_graceful_shutdown = graceful_shutdown_timeout_seconds()
    if mode == "node_server":
        import main_node
        uvicorn.run(
            main_node.app,
            host="0.0.0.0",
            port=_env_port("BETTER_CLAUDE_NODE_PORT", 8002),
            proxy_headers=False,
            timeout_graceful_shutdown=timeout_graceful_shutdown,
            ws_per_message_deflate=False,
        )
        return 0
    import main
    import user_prefs
    uvicorn.run(
        main.app,
        host=user_prefs.get_network_bind_address(),
        port=_env_port("BETTER_CLAUDE_BACKEND_PORT", 8000),
        proxy_headers=False,
        timeout_graceful_shutdown=timeout_graceful_shutdown,
        ws_per_message_deflate=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
