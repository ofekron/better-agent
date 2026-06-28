from __future__ import annotations

import shutil
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-pending-node-cache-")
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from stores import pending_node_registrations as store  # noqa: E402


def main() -> int:
    try:
        first = store.list_pending()
        if first != []:
            print(f"initial pending not empty: {first!r}")
            return 1

        rec = store.create(
            node_id="node-a",
            address="ws://127.0.0.1:9999",
            cwd_roots=["/tmp/a"],
            secret_hash="hash",
            fingerprint="fingerprint",
        )
        rec["cwd_roots"].append("/mutated")
        listed = store.list_pending()
        if [r.get("node_id") for r in listed] != ["node-a"]:
            print(f"pending cache missing node: {listed!r}")
            return 1
        if listed[0].get("cwd_roots") != ["/tmp/a"]:
            print(f"caller mutation leaked into pending cache: {listed!r}")
            return 1

        listed[0]["cwd_roots"].append("/mutated-again")
        listed_again = store.list_pending()
        if listed_again[0].get("cwd_roots") != ["/tmp/a"]:
            print(f"list mutation leaked into pending cache: {listed_again!r}")
            return 1

        original_read_text = Path.read_text

        def guarded_read_text(self: Path, *args, **kwargs):
            if self.name == "node-a.json":
                raise AssertionError("hot list_pending read pending node JSON")
            return original_read_text(self, *args, **kwargs)

        Path.read_text = guarded_read_text
        try:
            cached = store.list_pending()
        finally:
            Path.read_text = original_read_text
        if [r.get("node_id") for r in cached] != ["node-a"]:
            print(f"cached pending list mismatch: {cached!r}")
            return 1

        approved, reason = store.approve("node-a")
        if reason != "ok" or not approved:
            print(f"approve failed: {(approved, reason)!r}")
            return 1
        if store.list_pending() != []:
            print(f"approved node still pending: {store.list_pending()!r}")
            return 1

        print("PASS test_pending_node_registrations_cache")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
