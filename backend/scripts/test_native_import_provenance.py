"""Locks the durable spawn-provenance + always-on junk filter for native
import classification.

Covers:
- spawn_ledger append/read + dedup + persistence.
- runs_dir.reap_run_dir harvests the run dir's session_id into the ledger
  BEFORE removing the dir (so provenance survives the reap).
- _ba_managed_native_ids unions the durable ledger.
- _is_junk_session: real-cwd path + claude un-hydrated encoded-dir path,
  with the /tmpwork false-positive guard.
- enumerate_native_sessions drops junk (temp-cwd) sessions unconditionally
  (no project filter), keeping real ones.

Run with:
    cd backend && .venv/bin/python scripts/test_native_import_provenance.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-import-provenance-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import spawn_ledger  # noqa: E402
import runs_dir  # noqa: E402
import native_import as ni  # noqa: E402
logging.getLogger("config_store").setLevel(logging.CRITICAL)
logging.getLogger("keyring").setLevel(logging.CRITICAL)
import config_store  # noqa: E402

CASES = {"n": 0}


def check(cond, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    CASES["n"] += 1


def _claude_sess(nid: str, projdir: str, cwd: str = "") -> ni.NativeSession:
    return ni.NativeSession(
        provider_id="p", provider_kind="claude", native_id=nid,
        jsonl_path=f"/x/projects/{projdir}/{nid}.jsonl", cwd=cwd,
    )


def test_ledger() -> None:
    check(spawn_ledger.all_sids() == set(), "ledger starts empty")
    spawn_ledger.add("sid-A")
    spawn_ledger.add("sid-A")  # dedup on read
    spawn_ledger.add("sid-B")
    spawn_ledger.add("")       # no-op
    check(spawn_ledger.all_sids() == {"sid-A", "sid-B"}, "ledger add+dedup")


def test_reap_harvests() -> None:
    rd = Path(_TMP_HOME) / "runs" / "run-xyz"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "state.json").write_text(json.dumps({"session_id": "reap-sid"}), encoding="utf-8")
    removed = runs_dir.reap_run_dir(rd)
    check(removed is True, "reap returns True")
    check(not rd.exists(), "reap removed the dir")
    check("reap-sid" in spawn_ledger.all_sids(), "reap harvested sid into ledger")


def test_reap_harvests_from_backend_state() -> None:
    # When state.json is absent, harvest falls back to backend_state.json —
    # same sid key the live run-dir scan uses, so the ledger mirrors it.
    rd = Path(_TMP_HOME) / "runs" / "run-bs"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "backend_state.json").write_text(json.dumps({"session_id": "bs-sid"}), encoding="utf-8")
    runs_dir.reap_run_dir(rd)
    check(not rd.exists() and "bs-sid" in spawn_ledger.all_sids(),
          "reap harvests from backend_state.json fallback")


def test_managed_unions_ledger() -> None:
    spawn_ledger.add("managed-via-ledger")
    check("managed-via-ledger" in ni._ba_managed_native_ids(),
          "_ba_managed_native_ids unions the durable ledger")


def test_is_junk_session() -> None:
    # real cwd present → _is_junk_cwd
    check(ni._is_junk_session(_claude_sess("a", "proj", cwd="/private/tmp/x")), "junk by real cwd")
    check(not ni._is_junk_session(_claude_sess("b", "proj", cwd="/Users/ofekron/work")), "real cwd kept")
    # claude un-hydrated (cwd=""): infer from encoded project dir name
    check(ni._is_junk_session(_claude_sess("c", "-private-tmp-claude-501-bc-int-x")), "junk by encoded dir")
    check(ni._is_junk_session(_claude_sess("d", "-var-folders-ab-bc-test-y")), "junk var-folders dir")
    check(not ni._is_junk_session(_claude_sess("e", "-Users-ofekron-work")), "normal encoded dir kept")
    # false-positive guard: /tmpwork must NOT match the -tmp- prefix
    check(not ni._is_junk_session(_claude_sess("f", "-tmpwork-proj")), "/tmpwork not junk")
    # codex with empty cwd is not inferable → not junk (no jsonl-dir signal)
    codex = ni.NativeSession(provider_id="p", provider_kind="codex", native_id="g",
                             jsonl_path="/x/r.jsonl", cwd="")
    check(not ni._is_junk_session(codex), "codex empty cwd not junk")


def _make_claude_layout(root: Path, projdir: str, sid: str) -> None:
    d = root / "projects" / projdir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "uuid": str(uuid.uuid4()),
                    "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}) + "\n",
        encoding="utf-8",
    )


def test_enumerate_drops_junk() -> None:
    home = Path(_TMP_HOME) / "claude-home"
    _make_claude_layout(home, "-Users-ofekron-realproj", "real1")
    _make_claude_layout(home, "-private-tmp-claude-501-bc-int-zz", "junk1")
    prov = config_store.add_provider({
        "name": "prov-test", "kind": "claude", "mode": "subscription", "config_dir": str(home),
    })
    pid = prov["id"]
    try:
        got = ni.enumerate_native_sessions([pid], hydrate=False)
        ids = {s.native_id for s in got}
        check("real1" in ids, "real session kept")
        check("junk1" not in ids, "junk temp session dropped without project filter")
    finally:
        config_store.delete_provider(pid)


def main() -> None:
    test_ledger()
    test_reap_harvests()
    test_reap_harvests_from_backend_state()
    test_managed_unions_ledger()
    test_is_junk_session()
    test_enumerate_drops_junk()
    print(f"OK — {CASES['n']} checks passed")


if __name__ == "__main__":
    main()
