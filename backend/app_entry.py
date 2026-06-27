"""Single entrypoint for the PyInstaller-frozen macOS app bundle.

A frozen binary is `sys.executable`; the backend re-execs it to spawn
runner subprocesses, because a frozen app cannot run `python runner.py`.
This entrypoint inspects argv:
  - `--run-dir` present  → run the named runner in-process and exit.
  - `--communicate-mcp` present → run the stdio team-message MCP server.
  - `--open-file-panel-mcp` present → run the stdio file-panel MCP server.
  - `--open-config-panel-mcp` present → run the stdio config-panel MCP server.
  - `--extension-mcp` present → run an installed extension MCP launcher.
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


def _dispatch(argv: list[str]) -> tuple[str, Optional[str], Optional[Path]]:
    """Classify argv. `--run-dir` present → ('runner', kind, run_dir);
    `--serve-node` present → ('node_server', None, None). The desktop
    shell launches the primary server with the explicit `--serve` flag,
    but any non-runner
    invocation starts the server."""
    if "--communicate-mcp" in argv:
        return ("communicate_mcp", None, None)
    if "--open-file-panel-mcp" in argv:
        return ("open_file_panel_mcp", None, None)
    if "--open-config-panel-mcp" in argv:
        return ("open_config_panel_mcp", None, None)
    if "--extension-mcp" in argv:
        return ("extension_mcp", None, None)
    if "--serve-node" in argv:
        return ("node_server", None, None)
    if "--run-dir" not in argv:
        return ("server", None, None)
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument(
        "--runner-kind", default="claude",
        choices=["claude", "gemini", "codex", "fugu", "openai", "agy"],
    )
    args = parser.parse_args(argv)
    return ("runner", args.runner_kind, args.run_dir)


def _main(argv: Optional[list[str]] = None) -> int:
    mode, kind, run_dir = _dispatch(sys.argv[1:] if argv is None else argv)
    if mode == "communicate_mcp":
        from communicate_mcp import main as communicate_main
        return communicate_main()
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
    if mode == "runner":
        if kind == "gemini":
            from runner_gemini import main as runner_main
        elif kind == "codex":
            from runner_codex import main as runner_main
        elif kind == "fugu":
            # Fugu reuses the codex runner; it resolves `codex-fugu`.
            from runner_codex import main as runner_main
        elif kind == "openai":
            # OpenAI-compatible Chat Completions; BA owns the agent loop.
            from runner_openai import main as runner_main
        elif kind == "agy":
            from runner_agy import main as runner_main
        else:
            from runner import main as runner_main
        return runner_main(run_dir)
    import uvicorn
    if mode == "node_server":
        import main_node
        uvicorn.run(
            main_node.app,
            host="0.0.0.0",
            port=_env_port("BETTER_CLAUDE_NODE_PORT", 8002),
        )
        return 0
    import main
    import user_prefs
    uvicorn.run(
        main.app,
        host=user_prefs.get_network_bind_address(),
        port=_env_port("BETTER_CLAUDE_BACKEND_PORT", 8000),
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
