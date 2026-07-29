from __future__ import annotations

import sys
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parent.parent
if (
    (_SOURCE_ROOT / "switch_control_daemon").is_dir()
    and str(_SOURCE_ROOT) not in sys.path
):
    sys.path.insert(0, str(_SOURCE_ROOT))

from switch_control_daemon.line_switch_runtime.restart_request import (  # noqa: E402,F401
    clear_restart_request,
    consume_restart_request,
    main,
    new_restart_request_id,
    remove_restart_request,
    valid_restart_request_id,
    write_restart_request,
)


if __name__ == "__main__":
    raise SystemExit(main())
