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
from codex_execution import build_codex_execution_contract  # noqa: E402
from model_catalog_authority import (  # noqa: E402
    CatalogAuthority,
    build_catalog_authority,
)
from model_catalog_source_watcher import (  # noqa: E402
    _normalized_directory,
    _normalized_path,
)
from model_catalog_watch_spec import (  # noqa: E402
    _identity_directories,
    _identity_paths,
    _nearest_existing_directory,
    _provider_config_paths,
    _provider_config_root,
    _search_paths,
    build_source_watch_spec,
)
from scripts.codex_execution_test_support import write_executable  # noqa: E402

_PROVIDER_GENERATION = "9b5a6f36-d44c-4c3c-b54f-39554003065d"
_STATE_AUTHORITY = {
    "generation": "778b9bea-b654-4e4b-8799-496d34445062",
    "revision": 11,
    "digest": "a" * 64,
}


def _build_authority(root: Path, provider_id: str, *, with_config_file: bool) -> CatalogAuthority:
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    if with_config_file:
        (config_dir / "config.toml").write_text('profile = "work"\n', encoding="utf-8")
    executable = root / "runtime" / "codex"
    write_executable(executable, b"native")
    provider = {
        "id": provider_id,
        "kind": "codex",
        "generation": _PROVIDER_GENERATION,
        "revision": 7,
        "execution_revision": 3,
        "config_dir": str(config_dir),
        "base_url": "",
        "mode": "subscription",
        "api_key": "must-never-persist",
    }
    contract = build_codex_execution_contract(
        provider,
        launcher_path=str(executable),
        profile="work",
    )
    return build_catalog_authority(
        provider_id=provider["id"],
        provider_generation=provider["generation"],
        provider_revision=provider["revision"],
        provider_state_authority=_STATE_AUTHORITY,
        execution_contract=contract,
        discovery_method="codex.debug_models",
        discovery_version=1,
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


# --- identity paths (authority-present branch) -----------------------------


def test_identity_paths_collects_launcher_components_and_config_file(
    tmp_path: Path,
) -> None:
    authority = _build_authority(tmp_path, "codex-with-file", with_config_file=True)
    paths = _identity_paths(authority)
    contract = authority.execution_contract
    expected: set[Path] = set()
    for identity in {contract.launch_chain.launcher, *contract.launch_chain.components}:
        expected.add(Path(identity.requested_path))
        expected.add(Path(identity.resolved_path))
    for config in contract.config:
        expected.add(Path(config.config_path))
        expected.add(Path(config.root_path))
        assert config.config_file is not None
        expected.add(Path(config.config_file.requested_path))
        expected.add(Path(config.config_file.resolved_path))
    assert paths == expected


def test_identity_paths_skips_absent_config_file(tmp_path: Path) -> None:
    # config.toml absent -> ConfigIdentity.config_file is None, exercising the
    # false branch of the config_file guard inside _identity_paths.
    authority = _build_authority(tmp_path, "codex-no-file", with_config_file=False)
    assert any(c.config_file is None for c in authority.execution_contract.config)
    paths = _identity_paths(authority)
    for config in authority.execution_contract.config:
        assert Path(config.config_path) in paths
        assert Path(config.root_path) in paths


def test_identity_directories_collects_config_parents(tmp_path: Path) -> None:
    authority = _build_authority(tmp_path, "codex-dirs", with_config_file=True)
    expected = {
        Path(config.parent_path) for config in authority.execution_contract.config
    }
    assert _identity_directories(authority) == expected


def test_build_source_watch_spec_attaches_authority_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Provider carries a matching authority but NO config_dir/CODEX_HOME, so the
    # config_root guard takes its None branch while identity paths are attached.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    authority = _build_authority(
        tmp_path / "a", "codex-attach", with_config_file=True
    )
    spec = build_source_watch_spec(
        providers=[{"id": "codex-attach"}],
        authorities={"codex-attach": authority},
    )
    launcher = authority.execution_contract.launch_chain.launcher
    assert _normalized_path(Path(launcher.requested_path)) in set(spec.exact_paths)
    identity_dirs = set(spec.identity_directories)
    assert all(
        _normalized_directory(Path(config.parent_path)) in identity_dirs
        for config in authority.execution_contract.config
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
