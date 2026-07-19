from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path
from typing import Any


def request(control_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(str(control_path))
        connection.sendall(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        raw = b""
        while b"\n" not in raw:
            chunk = connection.recv(4096)
            if not chunk:
                break
            raw += chunk
            if len(raw) > 32 * 1024:
                raise RuntimeError("control response is too large")
    finally:
        connection.close()
    response = json.loads(raw.split(b"\n", 1)[0].decode("utf-8"))
    if not isinstance(response, dict) or response.get("ok") is not True:
        error = response.get("error") if isinstance(response, dict) else "invalid response"
        raise RuntimeError(str(error))
    return response


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("--checkout", required=True)
    start.add_argument("--host", required=True)
    start.add_argument("--port", required=True, type=int)
    signal_parser = subparsers.add_parser("signal")
    signal_parser.add_argument("--signal", required=True, choices=("INT", "TERM", "KILL"))
    subparsers.add_parser("status")
    subparsers.add_parser("shutdown")
    args = parser.parse_args(argv)
    payload: dict[str, Any] = {"op": args.operation}
    if args.operation == "start":
        payload.update(checkout=args.checkout, host=args.host, port=args.port)
    elif args.operation == "signal":
        payload["signal"] = args.signal
    response = request(args.control, payload)
    if args.operation == "start":
        print(response["pid"])
    elif args.operation == "status":
        print(json.dumps(response, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
