from __future__ import annotations

import os
import sys


def main() -> int:
    package_src = os.environ.get("PROVIDER_CONFIG_SYNC_PACKAGE_SRC", "").strip()
    if package_src:
        sys.path.insert(0, package_src)
    from provider_config_sync_backend.mcp_server import main as mcp_main

    mcp_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
