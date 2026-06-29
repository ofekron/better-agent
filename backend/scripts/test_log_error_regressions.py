from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-log-errors-")

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def check(condition: bool, message: str, failures: list[str]) -> None:
    print(f"  {PASS if condition else FAIL} {message}")
    if not condition:
        failures.append(message)


def test_session_index_skips_malformed_roots(failures: list[str]) -> None:
    import session_store
    from paths import ba_home

    sessions = ba_home() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "bad.json").write_text(json.dumps({"name": "missing id"}), encoding="utf-8")
    good = {
        "_schema_version": session_store.SCHEMA_VERSION,
        "id": "good",
        "name": "good",
        "model": "gpt-5.5",
        "cwd": "/tmp",
        "orchestration_mode": "native",
        "kind": "user",
        "parent_session_id": None,
        "forks": [{"name": "bad fork"}],
        "messages": [],
        "next_seq": 0,
        "created_at": "2026-06-21T00:00:00",
        "updated_at": "2026-06-21T00:00:00",
        "source": "cli",
    }
    (sessions / "good.json").write_text(json.dumps(good), encoding="utf-8")
    session_store._index_loaded = False
    session_store._index_fingerprint = None
    session_store._fork_index.clear()

    try:
        session_store._ensure_index()
    except Exception as exc:
        check(False, f"session index skips malformed root/fork records ({exc})", failures)
        return
    check(session_store._index_loaded is True, "session index loads despite malformed root/fork records", failures)


def test_prompt_templates_use_meipass(failures: list[str]) -> None:
    import prompt_templates

    root = Path(tempfile.mkdtemp(prefix="bc-test-meipass-"))
    old_meipass = getattr(sys, "_MEIPASS", None)
    try:
        prompt = root / "prompts" / "demo"
        prompt.mkdir(parents=True)
        (prompt / "system.md").write_text("Hello $name", encoding="utf-8")
        sys._MEIPASS = str(root)  # type: ignore[attr-defined]
        check(
            prompt_templates.render_prompt("demo/system.md", {"name": "BC"}) == "Hello BC",
            "prompt templates resolve from PyInstaller extraction root",
            failures,
        )
    finally:
        if old_meipass is None:
            try:
                del sys._MEIPASS  # type: ignore[attr-defined]
            except AttributeError:
                pass
        else:
            sys._MEIPASS = old_meipass  # type: ignore[attr-defined]
        shutil.rmtree(root, ignore_errors=True)


def test_jsonl_path_positive_cache(failures: list[str]) -> None:
    import orchs.jsonl_helpers as helpers

    claude_home = Path(tempfile.mkdtemp(prefix="bc-test-claude-home-"))
    old_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    old_glob = helpers.Path.glob
    calls = {"glob": 0}

    def counted_glob(self: Path, pattern: str):
        calls["glob"] += 1
        return old_glob(self, pattern)

    try:
        os.environ["CLAUDE_CONFIG_DIR"] = str(claude_home)
        helpers._JSONL_PATH_CACHE.clear()
        helpers._CLAUDE_PATH_INDEX = None
        helpers._RUN_STATE_PATH_CACHE.clear()
        helpers._RUN_STATE_RECENT_INDEX = None
        project = claude_home / "projects" / "encoded"
        project.mkdir(parents=True)
        target = project / "agent-sid.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        helpers.Path.glob = counted_glob  # type: ignore[method-assign]
        first = helpers.compute_jsonl_path("/tmp", "agent-sid")
        second = helpers.compute_jsonl_path("/tmp", "agent-sid")
        check(first == target and second == target, "jsonl helper returns cached path", failures)
        check(calls["glob"] == 1, "jsonl helper avoids repeated provider directory glob", failures)
    finally:
        helpers.Path.glob = old_glob  # type: ignore[method-assign]
        helpers._JSONL_PATH_CACHE.clear()
        helpers._CLAUDE_PATH_INDEX = None
        helpers._RUN_STATE_PATH_CACHE.clear()
        helpers._RUN_STATE_RECENT_INDEX = None
        if old_config_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old_config_dir
        shutil.rmtree(claude_home, ignore_errors=True)


def test_jsonl_path_encoded_cwd_fast_path(failures: list[str]) -> None:
    import orchs.jsonl_helpers as helpers
    from paths import encode_cwd

    claude_home = Path(tempfile.mkdtemp(prefix="bc-test-claude-home-fast-"))
    project_root = Path(tempfile.mkdtemp(prefix="bc-test-project-fast-"))
    old_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    old_glob = helpers.Path.glob
    calls = {"glob": 0}

    def counted_glob(self: Path, pattern: str):
        calls["glob"] += 1
        return old_glob(self, pattern)

    try:
        os.environ["CLAUDE_CONFIG_DIR"] = str(claude_home)
        helpers._JSONL_PATH_CACHE.clear()
        helpers._CLAUDE_PATH_INDEX = None
        helpers._RUN_STATE_PATH_CACHE.clear()
        helpers._RUN_STATE_RECENT_INDEX = None
        target_dir = claude_home / "projects" / encode_cwd(str(project_root))
        target_dir.mkdir(parents=True)
        target = target_dir / "agent-sid.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        helpers.Path.glob = counted_glob  # type: ignore[method-assign]
        found = helpers.compute_jsonl_path(str(project_root), "agent-sid")
        check(found == target, "jsonl helper resolves encoded cwd path first", failures)
        check(calls["glob"] == 0, "jsonl helper fast path avoids provider glob", failures)
    finally:
        helpers.Path.glob = old_glob  # type: ignore[method-assign]
        helpers._JSONL_PATH_CACHE.clear()
        helpers._CLAUDE_PATH_INDEX = None
        helpers._RUN_STATE_PATH_CACHE.clear()
        helpers._RUN_STATE_RECENT_INDEX = None
        if old_config_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old_config_dir
        shutil.rmtree(claude_home, ignore_errors=True)
        shutil.rmtree(project_root, ignore_errors=True)


def test_jsonl_read_path_uses_session_provider_config(failures: list[str]) -> None:
    import config_store
    import orchs.jsonl_helpers as helpers
    from paths import encode_cwd

    claude_home = Path(tempfile.mkdtemp(prefix="bc-test-claude-home-provider-"))
    project_root = Path(tempfile.mkdtemp(prefix="bc-test-project-provider-"))
    old_glob = helpers.Path.glob
    old_get_provider = config_store.get_provider
    calls = {"glob": 0}

    def counted_glob(self: Path, pattern: str):
        calls["glob"] += 1
        return old_glob(self, pattern)

    try:
        helpers._JSONL_PATH_CACHE.clear()
        helpers._CLAUDE_PATH_INDEX = None
        helpers._RUN_STATE_PATH_CACHE.clear()
        helpers._RUN_STATE_RECENT_INDEX = None
        target_dir = claude_home / "projects" / encode_cwd(str(project_root))
        target_dir.mkdir(parents=True)
        target = target_dir / "agent-sid.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        config_store.get_provider = lambda provider_id: {  # type: ignore[assignment]
            "id": provider_id,
            "config_dir": str(claude_home),
        }
        helpers.Path.glob = counted_glob  # type: ignore[method-assign]
        found = helpers.compute_jsonl_read_path(
            str(project_root),
            "agent-sid",
            session={"id": "s", "node_id": "primary", "provider_id": "provider-zai"},
        )
        check(found == target, "jsonl read helper uses session provider config", failures)
        check(calls["glob"] == 0, "provider-aware jsonl fast path avoids provider glob", failures)
    finally:
        helpers.Path.glob = old_glob  # type: ignore[method-assign]
        config_store.get_provider = old_get_provider  # type: ignore[assignment]
        helpers._JSONL_PATH_CACHE.clear()
        helpers._CLAUDE_PATH_INDEX = None
        helpers._RUN_STATE_PATH_CACHE.clear()
        helpers._RUN_STATE_RECENT_INDEX = None
        shutil.rmtree(claude_home, ignore_errors=True)
        shutil.rmtree(project_root, ignore_errors=True)


def test_jsonl_path_negative_cache(failures: list[str]) -> None:
    import orchs.jsonl_helpers as helpers

    claude_home = Path(tempfile.mkdtemp(prefix="bc-test-claude-home-miss-"))
    old_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    old_glob = helpers.Path.glob
    calls = {"glob": 0}

    def counted_glob(self: Path, pattern: str):
        calls["glob"] += 1
        return old_glob(self, pattern)

    try:
        os.environ["CLAUDE_CONFIG_DIR"] = str(claude_home)
        (claude_home / "projects").mkdir(parents=True)
        helpers._JSONL_PATH_CACHE.clear()
        helpers._CLAUDE_PATH_INDEX = None
        helpers._RUN_STATE_PATH_CACHE.clear()
        helpers._RUN_STATE_RECENT_INDEX = None
        helpers.Path.glob = counted_glob  # type: ignore[method-assign]
        first = helpers.compute_jsonl_path("/tmp", "missing-agent-sid")
        second = helpers.compute_jsonl_path("/tmp", "missing-agent-sid")
        check(first is None and second is None, "jsonl helper returns missing path consistently", failures)
        check(calls["glob"] == 1, "jsonl helper caches missing provider path", failures)
    finally:
        helpers.Path.glob = old_glob  # type: ignore[method-assign]
        helpers._JSONL_PATH_CACHE.clear()
        helpers._CLAUDE_PATH_INDEX = None
        helpers._RUN_STATE_PATH_CACHE.clear()
        helpers._RUN_STATE_RECENT_INDEX = None
        if old_config_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old_config_dir
        shutil.rmtree(claude_home, ignore_errors=True)


def test_jsonl_path_indexes_multiple_misses(failures: list[str]) -> None:
    import orchs.jsonl_helpers as helpers

    claude_home = Path(tempfile.mkdtemp(prefix="bc-test-claude-home-index-"))
    old_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    old_glob = helpers.Path.glob
    calls = {"glob": 0}

    def counted_glob(self: Path, pattern: str):
        calls["glob"] += 1
        return old_glob(self, pattern)

    try:
        os.environ["CLAUDE_CONFIG_DIR"] = str(claude_home)
        (claude_home / "projects").mkdir(parents=True)
        helpers._JSONL_PATH_CACHE.clear()
        helpers._CLAUDE_PATH_INDEX = None
        helpers._RUN_STATE_PATH_CACHE.clear()
        helpers._RUN_STATE_RECENT_INDEX = None
        helpers.Path.glob = counted_glob  # type: ignore[method-assign]
        first = helpers.compute_jsonl_path("/tmp", "missing-agent-a")
        second = helpers.compute_jsonl_path("/tmp", "missing-agent-b")
        check(first is None and second is None, "jsonl helper indexes multiple missing paths", failures)
        check(calls["glob"] == 1, "jsonl helper shares provider index across missing sids", failures)
    finally:
        helpers.Path.glob = old_glob  # type: ignore[method-assign]
        helpers._JSONL_PATH_CACHE.clear()
        helpers._CLAUDE_PATH_INDEX = None
        helpers._RUN_STATE_PATH_CACHE.clear()
        helpers._RUN_STATE_RECENT_INDEX = None
        if old_config_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old_config_dir
        shutil.rmtree(claude_home, ignore_errors=True)


def test_jsonl_path_coalesces_concurrent_provider_index(failures: list[str]) -> None:
    import orchs.jsonl_helpers as helpers

    claude_home = Path(tempfile.mkdtemp(prefix="bc-test-claude-home-concurrent-"))
    old_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    old_glob = helpers.Path.glob
    calls = {"glob": 0}
    calls_lock = threading.Lock()

    def counted_glob(self: Path, pattern: str):
        with calls_lock:
            calls["glob"] += 1
        time.sleep(0.05)
        return old_glob(self, pattern)

    try:
        os.environ["CLAUDE_CONFIG_DIR"] = str(claude_home)
        (claude_home / "projects").mkdir(parents=True)
        helpers._JSONL_PATH_CACHE.clear()
        helpers._CLAUDE_PATH_INDEX = None
        helpers._RUN_STATE_PATH_CACHE.clear()
        helpers._RUN_STATE_RECENT_INDEX = None
        helpers.Path.glob = counted_glob  # type: ignore[method-assign]

        results: list[Path | None] = []
        threads = [
            threading.Thread(
                target=lambda sid=sid: results.append(
                    helpers.compute_jsonl_path("/tmp", sid)
                )
            )
            for sid in ("missing-a", "missing-b", "missing-c", "missing-d")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        check(
            results == [None, None, None, None],
            "jsonl helper returns concurrent misses consistently",
            failures,
        )
        check(
            calls["glob"] == 1,
            "jsonl helper coalesces concurrent provider index builds",
            failures,
        )
    finally:
        helpers.Path.glob = old_glob  # type: ignore[method-assign]
        helpers._JSONL_PATH_CACHE.clear()
        helpers._CLAUDE_PATH_INDEX = None
        helpers._RUN_STATE_PATH_CACHE.clear()
        helpers._RUN_STATE_RECENT_INDEX = None
        if old_config_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old_config_dir
        shutil.rmtree(claude_home, ignore_errors=True)


def test_jsonl_path_targets_run_state_by_sid(failures: list[str]) -> None:
    import orchs.jsonl_helpers as helpers
    from paths import ba_home

    claude_home = Path(tempfile.mkdtemp(prefix="bc-test-claude-home-run-state-"))
    old_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    try:
        os.environ["CLAUDE_CONFIG_DIR"] = str(claude_home)
        (claude_home / "projects").mkdir(parents=True)
        runs = ba_home() / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        older = runs / "older"
        newer = runs / "newer"
        older.mkdir()
        newer.mkdir()
        older_jsonl = older / "session_events.jsonl"
        newer_jsonl = newer / "session_events.jsonl"
        older_jsonl.write_text("{}\n", encoding="utf-8")
        newer_jsonl.write_text("{}\n", encoding="utf-8")
        (older / "state.json").write_text(
            json.dumps({"session_id": "agent-sid", "jsonl_path": str(older_jsonl)}),
            encoding="utf-8",
        )
        (newer / "state.json").write_text(
            json.dumps({"session_id": "agent-sid", "jsonl_path": str(newer_jsonl)}),
            encoding="utf-8",
        )
        os.utime(older / "state.json", (1_700_000_000, 1_700_000_000))
        os.utime(newer / "state.json", (1_700_000_100, 1_700_000_100))
        helpers._JSONL_PATH_CACHE.clear()
        helpers._CLAUDE_PATH_INDEX = None
        helpers._RUN_STATE_PATH_CACHE.clear()
        helpers._RUN_STATE_RECENT_INDEX = None

        found = helpers.compute_jsonl_path("/tmp", "agent-sid")
        check(found == newer_jsonl, "jsonl helper targets run-state by sid", failures)
    finally:
        helpers._JSONL_PATH_CACHE.clear()
        helpers._CLAUDE_PATH_INDEX = None
        helpers._RUN_STATE_PATH_CACHE.clear()
        helpers._RUN_STATE_RECENT_INDEX = None
        if old_config_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old_config_dir
        shutil.rmtree(claude_home, ignore_errors=True)


def test_jsonl_path_reuses_recent_run_state_index(failures: list[str]) -> None:
    import orchs.jsonl_helpers as helpers
    from paths import ba_home

    claude_home = Path(tempfile.mkdtemp(prefix="bc-test-claude-home-run-cache-"))
    old_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    original_candidates = helpers._recent_state_candidates
    calls = {"candidates": 0}

    def counted_candidates(*args, **kwargs):
        calls["candidates"] += 1
        return original_candidates(*args, **kwargs)

    try:
        os.environ["CLAUDE_CONFIG_DIR"] = str(claude_home)
        (claude_home / "projects").mkdir(parents=True)
        runs = ba_home() / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        for sid in ("agent-a", "agent-b"):
            run_dir = runs / sid
            run_dir.mkdir()
            jsonl = run_dir / "session_events.jsonl"
            jsonl.write_text("{}\n", encoding="utf-8")
            (run_dir / "state.json").write_text(
                json.dumps({"session_id": sid, "jsonl_path": str(jsonl)}),
                encoding="utf-8",
            )
        helpers._JSONL_PATH_CACHE.clear()
        helpers._CLAUDE_PATH_INDEX = None
        helpers._RUN_STATE_PATH_CACHE.clear()
        helpers._RUN_STATE_RECENT_INDEX = None
        helpers._recent_state_candidates = counted_candidates  # type: ignore[assignment]

        first = helpers.compute_jsonl_path("/tmp", "agent-a")
        second = helpers.compute_jsonl_path("/tmp", "agent-b")
        check(first is not None and second is not None, "jsonl helper resolves cached run-state sids", failures)
        check(calls["candidates"] == 1, "jsonl helper reuses recent run-state index", failures)
    finally:
        helpers._recent_state_candidates = original_candidates  # type: ignore[assignment]
        helpers._JSONL_PATH_CACHE.clear()
        helpers._CLAUDE_PATH_INDEX = None
        helpers._RUN_STATE_PATH_CACHE.clear()
        helpers._RUN_STATE_RECENT_INDEX = None
        if old_config_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old_config_dir
        shutil.rmtree(claude_home, ignore_errors=True)


def main() -> int:
    failures: list[str] = []
    try:
        test_session_index_skips_malformed_roots(failures)
        test_prompt_templates_use_meipass(failures)
        test_jsonl_path_positive_cache(failures)
        test_jsonl_path_encoded_cwd_fast_path(failures)
        test_jsonl_read_path_uses_session_provider_config(failures)
        test_jsonl_path_negative_cache(failures)
        test_jsonl_path_indexes_multiple_misses(failures)
        test_jsonl_path_coalesces_concurrent_provider_index(failures)
        test_jsonl_path_targets_run_state_by_sid(failures)
        test_jsonl_path_reuses_recent_run_state_index(failures)
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if failures:
        print("FAILED:", failures)
        return 1
    print("all log error regressions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
