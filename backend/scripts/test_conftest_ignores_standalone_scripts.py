"""Lock the pytest_ignore_collect hook that keeps standalone scripts out of
the pytest unit suite.

`backend/scripts` holds ~340 standalone assertion scripts named `test_*.py`
that define no `def test_*`. pytest would otherwise IMPORT them during
collection to run their top-level code; the unguarded ones crash the whole
session ("mainloop: caught unexpected SystemExit"). The conftest hook skips
any `test_*.py` that defines no collectable test function or `Test*` class.

This test pins both directions: standalone-only files are ignored, real
test modules always pass through.
"""
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from pytest_collection_guard import should_ignore_test_module
from scripts.standalone_turn_test_runtime import scoped_turn_test_runtime


class _Path:
    """Minimal stand-in matching the attributes pytest_ignore_collect reads."""

    def __init__(self, path: str | Path, body: str):
        self._path = Path(path)
        self.name = self._path.name
        self._body = body
        self.reads = 0

    def read_text(self, encoding="utf-8"):
        self.reads += 1
        return self._body

    def resolve(self, strict=False):
        return self._path.resolve(strict=strict)


class _Config:
    def __init__(
        self,
        selectors: list[str],
        *,
        invocation_dir: Path,
        pyargs: bool = False,
    ) -> None:
        self._selectors = selectors
        self._pyargs = pyargs
        self.invocation_params = SimpleNamespace(dir=invocation_dir)

    def getoption(self, name: str):
        if name == "file_or_dir":
            return self._selectors
        if name == "pyargs":
            return self._pyargs
        raise ValueError(name)


def _hook(path, config=None):
    return should_ignore_test_module(path, config=config)


def test_collection_policy_does_not_mutate_runtime_home_or_profile():
    import installation_profile
    import paths

    env_before = (
        os.environ.get("BETTER_AGENT_HOME"),
        os.environ.get("BETTER_CLAUDE_HOME"),
    )
    state_before = (
        paths.ba_home(),
        installation_profile.BACKEND_ROOT,
        installation_profile.provider_conversations_enabled(),
    )

    assert _hook(_Path("test_real.py", "def test_real(): pass\n")) is None

    assert env_before == (
        os.environ.get("BETTER_AGENT_HOME"),
        os.environ.get("BETTER_CLAUDE_HOME"),
    )
    assert state_before == (
        paths.ba_home(),
        installation_profile.BACKEND_ROOT,
        installation_profile.provider_conversations_enabled(),
    )


def test_module_owned_test_homes_remain_addressable_after_collection():
    import _test_home
    import paths

    original_home = str(paths.ba_home())
    modules = [ModuleType("_home_probe_a"), ModuleType("_home_probe_b")]
    try:
        exec(
            "import _test_home\n"
            "HOME = _test_home.isolate('ba-module-home-0-')\n",
            modules[0].__dict__,
        )
        exec(
            "import _test_home\n"
            "HOME = _test_home.isolate('ba-module-home-1-')\n",
            modules[1].__dict__,
        )
        homes = [
            _test_home.pytest_home_for_module(module.__name__)
            for module in modules
        ]
        assert homes == [module.HOME for module in modules]
        assert homes[0] != homes[1]
    finally:
        _test_home.engage(original_home)


def test_conftest_reengages_the_collected_modules_home():
    source = (Path(__file__).parent / "conftest.py").read_text(encoding="utf-8")
    assert "_test_home.pytest_home_for_module(request.module.__name__)" in source


def test_engage_transitions_loaded_home_owners():
    import _test_home
    import paths
    import session_store
    from session_manager import manager as session_manager
    from stores import worker_store

    module_home = str(paths.ba_home())
    home_a = _test_home.TestHome.acquire("ba-transition-a-")
    home_b = None

    def session_record(session_id: str, name: str, cwd: str) -> dict:
        return {
            "_schema_version": session_store.SCHEMA_VERSION,
            "id": session_id,
            "name": name,
            "model": "gpt-5.5",
            "cwd": cwd,
            "orchestration_mode": "native",
            "kind": "user",
            "parent_session_id": None,
            "forks": [],
            "messages": [],
            "next_seq": 0,
            "created_at": "2026-08-05T00:00:00+00:00",
            "updated_at": "2026-08-05T00:00:00+00:00",
            "source": "test",
        }

    try:
        session_a = session_record("home-a-session", "home-a", "/tmp/home-a")
        session_store.write_session_full(session_a)
        assert session_manager.get(session_a["id"])["id"] == session_a["id"]
        worker_store.upsert_worker(
            "/tmp/home-a",
            session_a["id"],
            "native",
            "native-a",
        )

        home_b = _test_home.TestHome.acquire("ba-transition-b-")
        session_b = session_record(
            "home-b-direct-session",
            "home-b",
            "/tmp/home-b",
        )
        session_store.write_session_full(session_b)
        worker_store.upsert_worker(
            "/tmp/home-b",
            session_b["id"],
            "native",
            "native-b",
        )

        assert (Path(home_b.path) / "sessions" / f"{session_b['id']}.json").exists()
        assert not (Path(home_a.path) / "sessions" / f"{session_b['id']}.json").exists()
        assert session_manager.get(session_b["id"])["id"] == session_b["id"]
        assert session_manager.get(session_a["id"]) is None
        assert worker_store.get_worker("", session_b["id"])["agent_sid"] == "native-b"

        _test_home.engage(home_a.path)
        assert session_manager.get(session_a["id"])["id"] == session_a["id"]
        assert session_manager.get(session_b["id"]) is None
        assert worker_store.get_worker("", session_a["id"])["agent_sid"] == "native-a"
    finally:
        _test_home.engage(module_home)
        if home_b is not None:
            home_b.release()
        home_a.release()


def test_standalone_only_script_is_ignored():
    # No def test_* / Test class → standalone script → must be ignored.
    body = "import sys\nprint('side effect')\nsys.exit(0)\n"
    assert _hook(_Path("test_standalone_thing.py", body)) is True


@pytest.mark.parametrize(
    "filename",
    ["test_rate_limit_retry_cap.py", "test_selector_change_flap_cap.py"],
)
def test_side_effecting_standalone_runners_are_ignored(filename):
    path = Path(__file__).parent / filename
    assert _hook(_Path(path, path.read_text(encoding="utf-8"))) is True


def test_standalone_turn_runtime_overrides_are_scoped():
    import installation_profile
    import session_manager as session_manager_module
    import session_queue_projection
    from session_manager import manager as session_manager

    original = (
        session_manager_module.PERSIST_DEBOUNCE_S,
        installation_profile.integrations_enabled,
        installation_profile.assert_orchestration_mode_allowed,
        session_queue_projection.note_persisted_tree,
        session_manager.flush_root_persist,
        os.environ.get("BETTER_CLAUDE_TEST_AUTH_BYPASS"),
    )
    with scoped_turn_test_runtime():
        assert session_manager_module.PERSIST_DEBOUNCE_S == 0.0
        assert session_manager.flush_root_persist is not original[4]
        assert os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] == "1"
    assert (
        session_manager_module.PERSIST_DEBOUNCE_S,
        installation_profile.integrations_enabled,
        installation_profile.assert_orchestration_mode_allowed,
        session_queue_projection.note_persisted_tree,
        session_manager.flush_root_persist,
        os.environ.get("BETTER_CLAUDE_TEST_AUTH_BYPASS"),
    ) == original


def test_real_test_module_is_not_ignored():
    body = "def test_real():\n    assert True\n"
    assert _hook(_Path("test_real_thing.py", body)) is None


def test_class_based_test_module_is_not_ignored():
    body = "class TestThing:\n    def helper(self):\n        return 1\n"
    assert _hook(_Path("test_class_thing.py", body)) is None


def test_non_test_filename_is_not_ignored():
    body = "x = 1\n"
    assert _hook(_Path("helper.py", body)) is None


def test_unparseable_file_is_not_silently_ignored():
    # A syntax-broken file must surface as a real pytest error, not be hidden.
    assert _hook(_Path("test_broken.py", "def (\n")) is None


def test_explicit_file_selection_does_not_read_unrelated_candidate(tmp_path):
    selected = tmp_path / "test_selected.py"
    unrelated = _Path(tmp_path / "test_unrelated.py", "raise AssertionError\n")
    config = _Config([str(selected)], invocation_dir=tmp_path)

    assert _hook(unrelated, config) is None
    assert unrelated.reads == 0


def test_relative_nodeid_selection_still_classifies_selected_runner(tmp_path):
    selected = _Path(
        tmp_path / "test_selected.py",
        "import sys\ndef test_helper(): pass\nsys.exit(1)\n",
    )
    config = _Config(
        ["test_selected.py::TestGroup::test_case"],
        invocation_dir=tmp_path,
    )

    assert _hook(selected, config) is True
    assert selected.reads == 1


def test_absolute_selection_matches_same_physical_file(tmp_path):
    selected_path = tmp_path / "test_selected.py"
    selected = _Path(selected_path, "def test_real(): pass\n")
    config = _Config([str(selected_path.resolve())], invocation_dir=tmp_path)

    assert _hook(selected, config) is None
    assert selected.reads == 1


def test_multiple_explicit_files_skip_only_unselected_candidates(tmp_path):
    selectors = [str(tmp_path / "test_a.py"), str(tmp_path / "test_b.py")]
    unrelated = _Path(tmp_path / "test_c.py", "raise SystemExit(1)\n")
    config = _Config(selectors, invocation_dir=tmp_path)

    assert _hook(unrelated, config) is None
    assert unrelated.reads == 0


def test_mixed_file_and_directory_selection_preserves_full_classifier(tmp_path):
    candidate = _Path(tmp_path / "test_candidate.py", "raise SystemExit(1)\n")
    config = _Config(
        [str(tmp_path / "test_selected.py"), str(tmp_path)],
        invocation_dir=tmp_path,
    )

    assert _hook(candidate, config) is True
    assert candidate.reads == 1


def test_pyargs_selection_preserves_full_classifier(tmp_path):
    candidate = _Path(tmp_path / "test_candidate.py", "raise SystemExit(1)\n")
    config = _Config(["package_test.py"], invocation_dir=tmp_path, pyargs=True)

    assert _hook(candidate, config) is True
    assert candidate.reads == 1


def test_symlink_selector_matches_physical_candidate(tmp_path):
    physical = tmp_path / "test_physical.py"
    physical.write_text("raise SystemExit(1)\n", encoding="utf-8")
    alias = tmp_path / "test_alias.py"
    try:
        alias.symlink_to(physical)
    except OSError:
        pytest.skip("symlinks unavailable")
    candidate = _Path(physical, physical.read_text(encoding="utf-8"))
    config = _Config([str(alias)], invocation_dir=tmp_path)

    assert _hook(candidate, config) is True
    assert candidate.reads == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows path normalization")
def test_windows_case_variants_match_selected_file(tmp_path):
    selected_path = tmp_path / "Test_Selected.py"
    candidate = _Path(selected_path, "raise SystemExit(1)\n")
    config = _Config([str(selected_path).swapcase()], invocation_dir=tmp_path)

    assert _hook(candidate, config) is True
    assert candidate.reads == 1
