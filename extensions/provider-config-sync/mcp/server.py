from __future__ import annotations

def main() -> int:
    from provider_config_sync_backend.mcp_server import main as mcp_main

    mcp_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
