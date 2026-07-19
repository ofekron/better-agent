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
os.environ["BETTER_AGENT_PARALLEL_LINES"] = "1"

from daemonhost import pointer, switch_control  # noqa: E402
from daemonhost.jsonio import read_json, write_json  # noqa: E402
from daemonhost.paths import (  # noqa: E402
    restart_request_path,
    switch_request_path,
)
from switch_control_daemon.line_switch_runtime.requests import _release_preparation_owner  # noqa: E402


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
    conventional_dev = make_checkout(Path(HOME) / "app")
    dev = make_checkout(Path(HOME) / "custom-dev-checkout")
    main = make_checkout(Path(HOME) / "app-main")
    preview = make_checkout(Path(HOME) / "preview-checkout")
    legacy = make_checkout(Path(HOME) / "legacy-checkout")
    write_json(
        Path(HOME) / "switch_lines.json",
        {
            "dev": {"checkout": dev, "backend_port": 18765},
            "main": main,
            "preview": preview,
            "legacy": legacy,
        },
    )
    pointer.set_active(main, "seed")
    pointer.confirm_healthy(main, "seed")
    assert switch_control.state(main)["lines"]["preview"] == preview
    assert switch_control.state(main)["lines"]["dev"] == dev
    parallel = switch_control.submit(main, "dev", "parallel-dev")
    assert parallel["status"] == "succeeded"
    assert parallel["target_url"] == "http://127.0.0.1:18765"
    assert conventional_dev != dev

    reservation = switch_control.reserve(main, "preview", "reservation-owner")
    duplicate = switch_control.reserve(main, "preview", "reservation-duplicate")
    assert reservation["_reservation_created"] is True
    assert duplicate["_reservation_created"] is False
    assert duplicate["request_id"] == "reservation-owner"
    _release_preparation_owner(reservation["_preparation_token"])
    takeover = switch_control.reserve(main, "preview", "reservation-takeover")
    assert takeover["_reservation_created"] is True
    assert takeover["request_id"] == "reservation-owner"
    _release_preparation_owner(takeover["_preparation_token"])
    assert switch_control.service_tick(main)["status"] == "failed"

    submitted = switch_control.submit(main, "preview", "req-1")
    assert submitted["status"] == "pending"
    assert pointer.read()["active"] == main, "submission must not mutate launcher state"

    accepted = switch_control.service_tick(main)
    assert accepted["status"] == "accepted"
    assert pointer.read()["status"] == "switching"
    assert pointer.read()["active"] == preview
    assert restart_request_path().read_text(encoding="utf-8") == "req-1"

    assert pointer.reconcile_startup() is False, "matching durable request must survive daemon restart"
    restart_request_path().unlink()
    assert switch_control.service_tick(main)["request_id"] == "req-1", "consume must be idempotent"
    assert not restart_request_path().exists(), "accepted request must not emit a second restart"

    pointer.confirm_healthy(preview, "req-1")
    write_json(
        Path(HOME) / "refresh_result.json",
        {"request_id": "req-1", "status": "succeeded", "error": None},
    )
    completed = switch_control.service_tick(preview)
    assert completed["status"] == "succeeded", completed
    assert switch_control.request_status("req-1")["status"] == "succeeded"

    second = switch_control.submit(preview, "legacy", "req-2")
    assert second["status"] == "pending"
    switch_control.service_tick(preview)
    pointer.revert("backend failed to become healthy", "req-2")
    failed = switch_control.service_tick(preview)
    assert failed["status"] == "failed" and "backend failed" in failed["error"]

    pointer.confirm_healthy(preview)
    switch_control.submit(preview, "legacy", "req-build-fail")
    switch_control.service_tick(preview)
    restart_request_path().unlink()
    pointer.confirm_healthy(legacy, "req-build-fail")
    write_json(
        Path(HOME) / "refresh_result.json",
        {"request_id": "req-build-fail", "status": "failed", "error": "frontend build failed"},
    )
    failed_build = switch_control.service_tick(legacy)
    assert failed_build["status"] == "failed"
    assert pointer.read()["active"] == preview and pointer.read()["status"] == "reverted"
    assert restart_request_path().read_text(encoding="utf-8") == "req-build-fail"
    restart_request_path().unlink()
    switch_control.service_tick(preview)
    assert not restart_request_path().exists(), "terminal failure must not repeat restart"

    pointer.confirm_healthy(preview)
    switch_control.submit(preview, "legacy", "req-invalid-record")
    switch_control.service_tick(preview)
    restart_request_path().unlink()
    invalid = read_json(switch_request_path())
    invalid["target_path"] = preview
    write_json(switch_request_path(), invalid)
    invalid_result = switch_control.service_tick(preview)
    assert invalid_result["status"] == "failed"
    assert pointer.read()["active"] == preview and pointer.read()["status"] == "reverted"
    assert restart_request_path().read_text(encoding="utf-8") == "req-invalid-record"

    pointer.set_active(main, "orphan")
    switch_request_path().unlink(missing_ok=True)
    assert pointer.reconcile_startup() is True, "unmatched switching intent must still roll back"

    pointer.confirm_healthy(main)
    bootstrap_result: dict[str, object] = {}

    def run_bootstrap() -> None:
        try:
            bootstrap_result["value"] = switch_control.bootstrap(
                main, "preview", request_id="bootstrap-1", timeout=5, poll_interval=0.01
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
    pointer.confirm_healthy(preview, "bootstrap-1")
    write_json(
        Path(HOME) / "refresh_result.json",
        {"request_id": "bootstrap-1", "status": "succeeded", "error": None},
    )
    thread.join(timeout=2)
    assert not thread.is_alive() and "error" not in bootstrap_result, bootstrap_result
    assert bootstrap_result["value"]["status"] == "succeeded"

    pointer.confirm_healthy(main)
    try:
        switch_control.bootstrap(main, "preview", request_id="bootstrap-timeout", timeout=0.03, poll_interval=0.01)
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
