from __future__ import annotations

import re
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scripts import _test_home  # noqa: E402


TEST_HOME = _test_home.TestHome.acquire("ba-test-model-catalog-paths-")

import model_catalog_paths  # noqa: E402
from paths import ba_home, reset_home_cache  # noqa: E402


def test_provider_ids_are_confined_to_one_hashed_cache_namespace() -> None:
    root = ba_home()
    provider_ids = (
        "codex",
        "../escape",
        "../../outside/models",
        "/absolute/path",
        r"..\windows\escape",
        "provider/with/slashes",
        "unicode-\N{SNOWMAN}",
    )

    paths = [
        model_catalog_paths.catalog_cache_path(provider_id)
        for provider_id in provider_ids
    ]

    assert len(set(paths)) == len(provider_ids)
    for path in paths:
        assert path.parent == root / "model_catalog"
        assert re.fullmatch(r"[0-9a-f]{64}\.json", path.name)
        assert root in path.parents


def test_cache_path_is_stable_across_state_root_rehydration() -> None:
    before = model_catalog_paths.catalog_cache_path("restart-provider")

    reset_home_cache()
    after = model_catalog_paths.catalog_cache_path("restart-provider")

    assert after == before


def test_symlinked_cache_namespace_is_rejected_without_touching_target() -> None:
    root = ba_home()
    outside = root.parent / f"{root.name}-outside"
    outside.mkdir()
    marker = outside / "untouched"
    marker.write_text("safe", encoding="utf-8")
    namespace = root / "model_catalog"
    namespace.symlink_to(outside, target_is_directory=True)

    try:
        model_catalog_paths.catalog_cache_path("provider")
    except model_catalog_paths.CatalogCachePathError:
        pass
    else:
        raise AssertionError("symlinked catalog namespace was accepted")

    assert marker.read_text(encoding="utf-8") == "safe"
    assert not (outside / model_catalog_paths.cache_filename("provider")).exists()


if __name__ == "__main__":
    test_provider_ids_are_confined_to_one_hashed_cache_namespace()
    test_cache_path_is_stable_across_state_root_rehydration()
    test_symlinked_cache_namespace_is_rejected_without_touching_target()
    print("PASS: model catalog cache paths are confined and stable")
