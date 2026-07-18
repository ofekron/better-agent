from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

HOME = tempfile.mkdtemp(prefix="ba-independent-switch-")
os.environ["BETTER_AGENT_HOME"] = HOME

from daemonhost import pointer, switch_control  # noqa: E402
from daemonhost.jsonio import read_json, write_json  # noqa: E402
from daemonhost.paths import (  # noqa: E402
    restart_request_path,
    switch_request_path,
)


def make_checkout(path: Path) -> str:
    (path / "backend" / ".venv" / "bin").mkdir(parents=True)
    (path / "backend" / "main.py").write_text("", encoding="utf-8")
    (path / "backend" / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    for relative in switch_control._REQUIRED_CHECKOUT_FILES:
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    return str(path.resolve())


try:
    dev = make_checkout(Path(HOME) / "app")
    main = make_checkout(Path(HOME) / "app-main")
    write_json(Path(HOME) / "switch_lines.json", {"dev": dev, "main": main})
    pointer.set_active(main, "seed")
    pointer.confirm_healthy(main, "seed")

    submitted = switch_control.submit(main, "dev", "req-1")
    assert submitted["status"] == "pending"
    assert pointer.read()["active"] == main, "submission must not mutate launcher state"

    accepted = switch_control.service_tick(main)
    assert accepted["status"] == "accepted"
    assert pointer.read()["status"] == "switching"
    assert pointer.read()["active"] == dev
    assert restart_request_path().read_text(encoding="utf-8") == "req-1"

    assert pointer.reconcile_startup() is False, "matching durable request must survive daemon restart"
    assert switch_control.service_tick(main)["request_id"] == "req-1", "consume must be idempotent"

    pointer.confirm_healthy(dev, "req-1")
    write_json(
        Path(HOME) / "refresh_result.json",
        {"request_id": "req-1", "status": "succeeded", "error": None},
    )
    completed = switch_control.service_tick(dev)
    assert completed["status"] == "succeeded", completed
    assert switch_control.request_status("req-1")["status"] == "succeeded"

    second = switch_control.submit(dev, "main", "req-2")
    assert second["status"] == "pending"
    switch_control.service_tick(dev)
    pointer.revert("backend failed to become healthy", "req-2")
    failed = switch_control.service_tick(dev)
    assert failed["status"] == "failed" and "backend failed" in failed["error"]

    pointer.set_active(main, "orphan")
    switch_request_path().unlink(missing_ok=True)
    assert pointer.reconcile_startup() is True, "unmatched switching intent must still roll back"

    pointer.confirm_healthy(main)
    bootstrap_result: dict[str, object] = {}

    def run_bootstrap() -> None:
        try:
            bootstrap_result["value"] = switch_control.bootstrap(
                main, "dev", request_id="bootstrap-1", timeout=5, poll_interval=0.01
            )
        except BaseException as exc:
            bootstrap_result["error"] = exc

    thread = threading.Thread(target=run_bootstrap)
    thread.start()
    deadline = time.time() + 2
    while pointer.read().get("status") != "switching" and time.time() < deadline:
        time.sleep(0.01)
    pointer.revert("unfinished switch recovered at daemonhost startup", "bootstrap-1")
    deadline = time.time() + 2
    while pointer.read().get("status") != "switching" and time.time() < deadline:
        time.sleep(0.01)
    pointer.confirm_healthy(dev, "bootstrap-1")
    write_json(
        Path(HOME) / "refresh_result.json",
        {"request_id": "bootstrap-1", "status": "succeeded", "error": None},
    )
    thread.join(timeout=2)
    assert not thread.is_alive() and "error" not in bootstrap_result, bootstrap_result
    assert bootstrap_result["value"]["status"] == "succeeded"

    pointer.confirm_healthy(main)
    try:
        switch_control.bootstrap(main, "dev", request_id="bootstrap-timeout", timeout=0.03, poll_interval=0.01)
        raise AssertionError("bootstrap timeout must fail")
    except TimeoutError:
        pass
    timeout_request = switch_control.request_status("bootstrap-timeout")
    assert timeout_request["status"] == "failed"
    assert pointer.read()["status"] != "switching"
    assert not restart_request_path().exists()

    print("OK test_switch_control_independent")
finally:
    shutil.rmtree(HOME)
