from __future__ import annotations

import argparse
import json
import socket
from pathlib import Path

from switch_control_daemon.line_switch_runtime import control as _control
from switch_control_daemon.line_switch_runtime import requests as _requests
from switch_control_daemon.line_switch_runtime.web import _access_config

_REQUIRED_CHECKOUT_FILES = _control._REQUIRED_CHECKOUT_FILES
_configured_lines = _control._configured_lines
_incompatible = _control._incompatible
request = _control.request
state = _control.state
bootstrap = _requests.bootstrap
activate = _requests.activate
fail = _requests.fail
request_status = _requests.request_status
reserve = _requests.reserve
service_tick = _requests.service_tick
submit = _requests.submit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="line-switch")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("switch", "bootstrap"):
        command = commands.add_parser(name)
        command.add_argument("target")
        command.add_argument("--running-checkout", default=str(Path.cwd()))
        command.add_argument("--request-id", default="")
        command.add_argument("--timeout", type=float, default=180.0)
    tick = commands.add_parser("service-tick")
    tick.add_argument("--running-checkout", default="")
    status_parser = commands.add_parser("status")
    status_parser.add_argument("request_id")
    commands.add_parser("access-url")
    args = parser.parse_args(argv)
    if args.command == "access-url":
        config = _access_config()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.0.2.1", 80))
                host = probe.getsockname()[0]
        except OSError:
            host = "127.0.0.1"
        print(f"http://{host}:{config['port']}/#{config['token']}")
        return 0
    if args.command == "service-tick":
        print(json.dumps(service_tick(args.running_checkout or None)))
        return 0
    if args.command == "status":
        print(json.dumps(request_status(args.request_id)))
        return 0
    request_id = args.request_id or None
    if args.command == "bootstrap":
        result = bootstrap(
            args.running_checkout,
            args.target,
            timeout=args.timeout,
            request_id=request_id,
        )
    else:
        result = submit(args.running_checkout, args.target, request_id)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
