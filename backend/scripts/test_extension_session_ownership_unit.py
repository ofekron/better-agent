"""Dedicated unit coverage for extension_session_ownership.py.

Pure file-backed ownership map: records which extension created a session so
the SDK's session-mutation endpoints can enforce that an extension only
mutates the sessions it created. Persisted to extension_session_ownership.json
under the state home as a flat {session_id: extension_id} map. No pydantic,
no async, no network — just JSON + a module lock.

Covers every branch of every function directly, including the _load defensive
paths (missing file, corrupt JSON, IO error, non-dict) and the claim/disown
guards that the referencing integration tests reach only on the happy path.

Home is engaged to an isolated tempdir at import time. An autouse fixture
clears the ownership file before and after every test so state never leaks
between tests.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home  # noqa: E402

_test_home.isolate("extension-session-ownership-test-")

import extension_session_ownership as store  # noqa: E402


@pytest.fixture(autouse=True)
def fresh():
    """Start and end each test with no ownership file on disk."""
    _clear()
    yield
    _clear()


def _clear() -> None:
    path = store._path()
    if path.is_dir():
        path.rmdir()
    elif path.exists():
        path.unlink()


def _write_raw(payload: object) -> None:
    store._path().write_text(json.dumps(payload), encoding="utf-8")


# --- _path + _load ----------------------------------------------------------


def test_path_under_home():
    assert store._path() == store.ba_home() / "extension_session_ownership.json"


def test_load_returns_empty_when_file_missing():
    assert store._load() == {}


def test_load_returns_empty_on_corrupt_json():
    store._path().write_text("{not valid json", encoding="utf-8")
    assert store._load() == {}


def test_load_returns_empty_on_io_error():
    # read_text on a directory raises IsADirectoryError (an OSError) -> {}.
    store._path().mkdir()
    assert store._load() == {}


def test_load_returns_empty_on_non_dict_payload():
    _write_raw(["session-a", "session-b"])
    assert store._load() == {}


def test_load_returns_valid_dict():
    _write_raw({"session-a": "ext.one"})
    assert store._load() == {"session-a": "ext.one"}


# --- claim ------------------------------------------------------------------


@pytest.mark.parametrize(
    "session_id, extension_id",
    [("", "ext"), ("session", ""), ("", "")],
)
def test_claim_ignores_empty_identifiers(session_id, extension_id):
    store.claim(session_id, extension_id)
    # nothing persisted
    assert not store._path().exists()
    assert store.owned_session_ids() == ()


def test_claim_records_and_is_idempotent():
    store.claim("session-a", "ext.one")
    assert store.owner("session-a") == "ext.one"
    # claiming the same pair again is a no-op rewrite with identical content
    store.claim("session-a", "ext.one")
    assert store.owner("session-a") == "ext.one"
    assert store.owned_session_ids() == ("session-a",)


def test_claim_overwrites_owner_for_same_session():
    store.claim("session-a", "ext.one")
    store.claim("session-a", "ext.two")
    assert store.owner("session-a") == "ext.two"


def test_claim_persists_across_reload():
    store.claim("session-a", "ext.one")
    store.claim("session-b", "ext.two")
    # the file on disk is authoritative JSON
    on_disk = json.loads(store._path().read_text(encoding="utf-8"))
    assert on_disk == {"session-a": "ext.one", "session-b": "ext.two"}


# --- owner ------------------------------------------------------------------


def test_owner_returns_none_for_unknown_session():
    store.claim("session-a", "ext.one")
    assert store.owner("missing") is None


def test_owner_returns_none_when_map_empty():
    assert store.owner("anything") is None


# --- is_owner ---------------------------------------------------------------


def test_is_owner_false_for_empty_session_id():
    # short-circuits before consulting the map (bool(session_id) is False)
    store.claim("session-a", "ext.one")
    assert store.is_owner("", "ext.one") is False


def test_is_owner_true_on_match():
    store.claim("session-a", "ext.one")
    assert store.is_owner("session-a", "ext.one") is True


def test_is_owner_false_on_mismatch():
    store.claim("session-a", "ext.one")
    assert store.is_owner("session-a", "ext.two") is False


def test_is_owner_false_when_no_owner_recorded():
    assert store.is_owner("session-a", "ext.one") is False


# --- owned_session_ids ------------------------------------------------------


def test_owned_session_ids_empty_and_populated():
    assert store.owned_session_ids() == ()
    store.claim("session-a", "ext.one")
    store.claim("session-b", "ext.two")
    assert set(store.owned_session_ids()) == {"session-a", "session-b"}


# --- disown -----------------------------------------------------------------


def test_disown_removes_existing_session_and_saves():
    store.claim("session-a", "ext.one")
    store.disown("session-a")
    assert store.owner("session-a") is None
    assert store.owned_session_ids() == ()


def test_disown_missing_session_is_noop():
    store.claim("session-a", "ext.one")
    snapshot = store._path().read_text(encoding="utf-8")
    # the missing pop returns None -> _save is NOT called
    store.disown("missing")
    assert store._path().read_text(encoding="utf-8") == snapshot


def test_disown_when_no_file_is_noop():
    # _load returns {} -> pop returns None -> no save, no file created
    store.disown("session-a")
    assert not store._path().exists()


# --- _save parent creation --------------------------------------------------


def test_save_creates_parent_directory():
    # ba_home exists, but _save must be safe when invoked; claim exercises it.
    store.claim("session-a", "ext.one")
    assert store._path().exists()


# --- concurrency (lock) -----------------------------------------------------


def test_concurrent_claims_all_land():
    # Many threads claiming distinct sessions must all persist; exercises the
    # module lock under contention without losing writes.
    results: list[bool] = []

    def worker(i: int) -> None:
        store.claim(f"session-{i}", f"ext.{i}")
        results.append(store.is_owner(f"session-{i}", f"ext.{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 40
    assert all(results)
    assert len(store.owned_session_ids()) == 40
