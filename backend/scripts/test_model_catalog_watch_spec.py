from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scripts import _test_home  # noqa: E402

TEST_HOME = _test_home.TestHome.acquire("ba-test-watch-spec-")

import pytest  # noqa: E402

import model_catalog_watch_spec as watch_spec  # noqa: E402
from model_catalog_watch_spec import (  # noqa: E402
    _nearest_existing_directory,
    _provider_config_paths,
    _provider_config_root,
    _search_paths,
    build_source_watch_spec,
)


# --- _provider_config_root -------------------------------------------------


def test_provider_config_root_prefers_absolute_config_dir(tmp_path: Path) -> None:
    assert _provider_config_root({"config_dir": str(tmp_path)}) == tmp_path


def test_provider_config_root_rejects_relative_config_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    # A non-empty but relative config_dir resolves to a relative path -> None.
    assert _provider_config_root({"config_dir": "relative/codex"}) is None


def test_provider_config_root_blank_value_falls_back_to_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    # A whitespace-only config_dir is treated as empty, falling back to env.
    assert _provider_config_root({"config_dir": "   "}) == tmp_path
    assert _provider_config_root({}) == tmp_path


def test_provider_config_root_none_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert _provider_config_root({}) is None


# --- _provider_config_paths ------------------------------------------------


def test_provider_config_paths_returns_canonical_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert _provider_config_paths({}) == {
        tmp_path,
        tmp_path / "config.toml",
        tmp_path / "auth.json",
    }


def test_provider_config_paths_empty_when_root_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert _provider_config_paths({"config_dir": "rel"}) == set()


# --- _nearest_existing_directory ------------------------------------------


def test_nearest_existing_directory_resolves_chain(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    # an existing directory returns itself
    assert _nearest_existing_directory(deep) == deep
    # a file resolves to its containing directory
    f = deep / "x.toml"
    f.write_text("x")
    assert _nearest_existing_directory(f) == deep
    # a missing path resolves to its nearest existing ancestor
    assert _nearest_existing_directory(deep / "x.toml" / "nested") == deep


def test_nearest_existing_directory_none_when_no_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Absolute-path parent chains always terminate at an existing root on a
    # real filesystem, so the None return is only reachable when no entry in
    # the chain reports as a directory. Simulate that to assert the defensive
    # None contract that the caller (build_source_watch_spec) depends on.
    monkeypatch.setattr(Path, "is_dir", lambda self: False)
    assert _nearest_existing_directory(Path("/does/not/exist")) is None


# --- _search_paths ---------------------------------------------------------


def test_search_paths_classifies_path_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "bin"
    existing.mkdir()
    absent_file = tmp_path / "missing" / "codex"
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            [
                "",  # empty entry skipped
                "relative/dir",  # relative entry skipped
                str(existing),  # existing dir -> search directories
                str(absent_file),  # non-dir absolute -> exact, walked up
            ]
        ),
    )
    directories, exact = _search_paths()
    assert existing in directories
    assert absent_file in exact
    # the walk-up stops at the first existing ancestor (tmp_path), promoting
    # it to a search directory rather than an exact path.
    assert tmp_path in directories


def test_search_paths_no_directory_in_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When no directory anywhere in an absolute path's chain exists (a state
    # impossible on a real filesystem), the walk-up adds ancestors to exact
    # but promotes no directory. Asserts the defensive empty-directory
    # contract.
    monkeypatch.setattr(Path, "is_dir", lambda self: False)
    monkeypatch.setenv("PATH", "/no/such/codex")
    directories, exact = _search_paths()
    assert directories == set()
    assert Path("/no/such/codex") in exact


# --- build_source_watch_spec -----------------------------------------------


def test_build_source_watch_spec_feeds_config_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    spec = build_source_watch_spec(
        providers=[{"id": "codex", "config_dir": str(tmp_path)}],
        authorities={},
    )
    assert str(tmp_path / "config.toml") in spec.exact_paths
    assert str(tmp_path / "auth.json") in spec.exact_paths
    assert any(p == str(tmp_path) for p in spec.identity_directories)
    assert any(p == str(tmp_path) for p in spec.search_directories)


def test_build_source_watch_spec_skips_config_path_with_no_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # White-box: when _nearest_existing_directory returns None for one config
    # path, the loop skips adding it to watch roots but still succeeds for the
    # rest. This is the defensive None-skip branch of the config loop.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    real_nearest = watch_spec._nearest_existing_directory

    def nearest_or_none(path: Path) -> Path | None:
        if path == tmp_path / "auth.json":
            return None
        return real_nearest(path)

    monkeypatch.setattr(watch_spec, "_nearest_existing_directory", nearest_or_none)
    spec = build_source_watch_spec(
        providers=[{"id": "codex", "config_dir": str(tmp_path)}],
        authorities={},
    )
    assert str(tmp_path / "auth.json") in spec.exact_paths


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
