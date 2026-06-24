"""Import REAL native sessions into the REAL Better Agent home.

Prefers running the import INSIDE a live backend via the internal-token
route (/api/internal/native-import) — that is the ONLY safe way when a
backend is running, because a separate process writing session.json races
the backend's in-memory cache (it re-persists and clobbers the render
tree, leaving empty assistant bubbles). Falls back to an in-process
standalone import only when no backend is reachable (with a warning).

Usage:
    cd backend && .venv/bin/python scripts/import_real_native.py [--limit N] [--port 8000]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def _internal_token() -> str | None:
    try:
        import paths  # noqa
        p = paths.ba_home() / "internal_token"
        return p.read_text(encoding="utf-8").strip() if p.is_file() else None
    except Exception:
        return None


def _post(url: str, token: str, body: dict) -> dict | None:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Internal-Token": token},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def _get(url: str, token: str) -> dict | None:
    req = urllib.request.Request(url, headers={"X-Internal-Token": token})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def _try_backend(port: int, limit: int, provider_ids: list[str] | None) -> bool:
    """Drive the import through a live backend. Returns True on success."""
    token = _internal_token()
    base = f"http://127.0.0.1:{port}"
    if not token:
        return False
    try:
        status = _post(
            f"{base}/api/internal/native-import",
            token,
            {"limit": limit, **({"provider_ids": provider_ids} if provider_ids else {})},
        )
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False
    print(f"backend on :{port} accepted the job — importing in-process…")
    deadline = time.time() + max(60, limit * 10)
    while status.get("status") == "running" and time.time() < deadline:
        print(f"  imported={status.get('imported')} skipped={status.get('skipped')} "
              f"failed={status.get('failed')}/{status.get('total')}")
        time.sleep(1)
        status = _get(f"{base}/api/internal/native-import/status", token) or status
    print(f"done: status={status.get('status')} imported={status.get('imported')} "
          f"skipped={status.get('skipped')} failed={status.get('failed')}")
    for err in (status.get("errors") or [])[:5]:
        print("  error:", err)
    return True


def _standalone(limit: int, provider_ids: list[str] | None) -> int:
    import native_import  # noqa
    sessions = native_import.enumerate_native_sessions(provider_ids)
    already = native_import.already_imported_keys()
    pending = [s for s in sessions if s.registry_key not in already]
    print(f"enumerated {len(sessions)}; {len(pending)} not yet imported")
    imported = 0
    for sess in pending:
        if imported >= limit:
            break
        try:
            root_id = native_import.import_session(sess)
            imported += 1
            title = (sess.title or f"{sess.provider_kind} {sess.native_id[:8]}")[:60]
            print(f"  [{imported}/{limit}] {sess.provider_kind} {sess.native_id[:12]} root={root_id[:8]} {title}")
        except Exception as exc:
            print(f"  SKIP {sess.registry_key}: {exc}")
    print(f"imported {imported} session(s) into {native_import.paths.ba_home()}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--provider-ids", default="")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    provider_ids = [p for p in args.provider_ids.split(",") if p] or None

    if _try_backend(args.port, args.limit, provider_ids):
        return 0

    print("No backend reachable on :{} (or it lacks the internal route).".format(args.port))
    print("WARNING: standalone import while a backend IS running will race its cache")
    print("         and produce empty assistant bubbles. Proceeding standalone…\n")
    return _standalone(args.limit, provider_ids)


if __name__ == "__main__":
    raise SystemExit(main())
