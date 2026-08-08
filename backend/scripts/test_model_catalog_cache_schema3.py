from __future__ import annotations

import copy
import json
import math
import os
import sys
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from codex_execution import build_codex_execution_contract  # noqa: E402
from model_catalog_authority import (  # noqa: E402
    CatalogAuthority,
    CatalogAuthorityError,
    build_catalog_authority,
)
from model_catalog_cache import (  # noqa: E402
    CACHE_SCHEMA_VERSION,
    CatalogCacheError,
    CatalogSnapshot,
    build_catalog_snapshot,
    read_catalog_cache,
    write_catalog_cache,
)
import model_catalog_cache as catalog_cache  # noqa: E402
import provider_sync_authority  # noqa: E402
from scripts.codex_execution_test_support import write_executable  # noqa: E402


PROVIDER_ID = "codex-provider"
PROVIDER_GENERATION = "9b5a6f36-d44c-4c3c-b54f-39554003065d"
STATE_GENERATION = "778b9bea-b654-4e4b-8799-496d34445062"


def _provider(
    config_dir: Path, revision: int = 7, execution_revision: int = 3,
) -> dict:
    return {
        "id": PROVIDER_ID,
        "kind": "codex",
        "generation": PROVIDER_GENERATION,
        "revision": revision,
        "execution_revision": execution_revision,
        "config_dir": str(config_dir),
        "base_url": "",
        "mode": "subscription",
        "api_key": "must-never-persist",
    }


def _state_authority(revision: int = 11) -> dict:
    return {
        "generation": STATE_GENERATION,
        "revision": revision,
        "digest": "a" * 64,
    }


def _authority(root: Path, *, revision: int = 7) -> CatalogAuthority:
    config_dir = root / f"config-{revision}"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'profile = "work"\n',
        encoding="utf-8",
    )
    executable = root / f"runtime-{revision}" / "codex"
    write_executable(executable, b"native")
    provider = _provider(config_dir, revision)
    contract = build_codex_execution_contract(
        provider,
        launcher_path=str(executable),
        profile="work",
        catalog_args=("-c", 'model_provider="openai"'),
    )
    return build_catalog_authority(
        provider_id=provider["id"],
        provider_generation=provider["generation"],
        provider_revision=provider["revision"],
        provider_state_authority=_state_authority(),
        execution_contract=contract,
        discovery_method="codex.debug_models",
        discovery_version=1,
    )


def _snapshot(authority: CatalogAuthority) -> CatalogSnapshot:
    return build_catalog_snapshot(
        authority=authority,
        models=["gpt-5.6-sol", "gpt-5.6-terra"],
        retired=[
            {
                "id": "gpt-5.5",
                "first_absent_at": 1_722_000_000.25,
            },
        ],
        last_refreshed_at=1_722_000_001.5,
        last_fetch_state="ok",
    )


def _write_raw(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_schema3_round_trip_binds_existing_authorities_without_secrets() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        path = root / "models.json"
        authority = _authority(root)
        snapshot = _snapshot(authority)

        assert write_catalog_cache(path, snapshot)
        result = read_catalog_cache(path, expected_authority=authority)
        persisted = json.loads(path.read_text(encoding="utf-8"))

        assert result.status == "matching"
        assert result.snapshot == snapshot
        assert persisted["schema"] == CACHE_SCHEMA_VERSION == 3
        assert persisted["authority"]["provider"] == {
            "id": PROVIDER_ID,
            "generation": PROVIDER_GENERATION,
            "revision": 7,
            "execution_revision": 3,
        }
        assert persisted["authority"]["provider_state"] == _state_authority()
        assert (
            persisted["authority"]["source"]["catalog_fingerprint"]
            == authority.execution_contract.catalog_fingerprint
        )
        assert persisted["authority"]["discovery"] == {
            "method": "codex.debug_models",
            "version": 1,
        }
        assert "must-never-persist" not in json.dumps(persisted, sort_keys=True)
        assert "api_key" not in json.dumps(persisted, sort_keys=True)


def test_schema2_corrupt_missing_and_tampered_authority_are_removed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        path = root / "models.json"
        authority = _authority(root)
        valid = _snapshot(authority).to_dict()
        invalid_payloads: list[object] = [
            {
                "schema": 2,
                "models": ["untrusted-static-model"],
                "retired": [],
                "last_refreshed_at": 10.0,
                "last_fetch_state": "ok",
            },
            "{",
            {key: value for key, value in valid.items() if key != "authority"},
        ]
        tampered_digest = copy.deepcopy(valid)
        tampered_digest["models"] = ["forged-model"]
        invalid_payloads.append(tampered_digest)
        tampered_authority = copy.deepcopy(valid)
        tampered_authority["authority"]["provider"]["revision"] += 1
        invalid_payloads.append(tampered_authority)
        tampered_contract = copy.deepcopy(valid)
        tampered_contract["authority"]["source"]["execution_contract"][
            "profile"
        ] = "forged"
        invalid_payloads.append(tampered_contract)

        for payload in invalid_payloads:
            if isinstance(payload, str):
                path.write_text(payload, encoding="utf-8")
            else:
                _write_raw(path, payload)
            result = read_catalog_cache(path, expected_authority=authority)
            assert result.status == "invalid"
            assert result.snapshot is None
            assert not path.exists()


def test_oversize_and_symlink_cache_files_are_removed_without_following() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        path = root / "models.json"
        authority = _authority(root)

        path.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
        result = read_catalog_cache(path, expected_authority=authority)
        assert result.status == "invalid"
        assert not path.exists()

        target = root / "unrelated.json"
        target.write_text('{"secret":"unchanged"}', encoding="utf-8")
        path.symlink_to(target)
        result = read_catalog_cache(path, expected_authority=authority)
        assert result.status == "invalid"
        assert not path.exists()
        assert target.read_text(encoding="utf-8") == '{"secret":"unchanged"}'


def test_path_replacement_is_rejected_before_reading_replacement_bytes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        path = root / "models.json"
        target = root / "unrelated"
        authority = _authority(root)
        write_catalog_cache(path, _snapshot(authority))
        target.write_text("sensitive unrelated content", encoding="utf-8")
        real_open = catalog_cache.os.open
        real_read = catalog_cache.os.read
        reads = 0
        replaced = False

        def replacing_open(raw_path: object, flags: int, *args: object) -> int:
            nonlocal replaced
            if Path(raw_path) == path and not replaced:
                replaced = True
                path.unlink()
                os.link(target, path)
            return real_open(raw_path, flags, *args)

        def tracking_read(descriptor: int, size: int) -> bytes:
            nonlocal reads
            reads += 1
            return real_read(descriptor, size)

        catalog_cache.os.open = replacing_open
        catalog_cache.os.read = tracking_read
        try:
            result = read_catalog_cache(path, expected_authority=authority)
        finally:
            catalog_cache.os.open = real_open
            catalog_cache.os.read = real_read

        assert result.status == "invalid"
        assert reads == 0
        assert target.read_text(encoding="utf-8") == "sensitive unrelated content"


def test_access_time_change_does_not_invalidate_stable_cache() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        path = root / "models.json"
        authority = _authority(root)
        write_catalog_cache(path, _snapshot(authority))
        real_fstat = catalog_cache.os.fstat
        calls = 0

        def changing_atime(descriptor: int) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            observed = real_fstat(descriptor)
            return SimpleNamespace(
                st_mode=observed.st_mode,
                st_size=observed.st_size,
                st_mtime_ns=observed.st_mtime_ns,
                st_ctime_ns=observed.st_ctime_ns,
                st_dev=observed.st_dev,
                st_ino=observed.st_ino,
                st_atime_ns=observed.st_atime_ns + calls,
            )

        catalog_cache.os.fstat = changing_atime
        try:
            result = read_catalog_cache(path, expected_authority=authority)
        finally:
            catalog_cache.os.fstat = real_fstat

        assert calls == 2
        assert result.status == "matching"


def test_valid_but_different_source_is_preserved_and_never_authorized() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        path = root / "models.json"
        original = _authority(root, revision=7)
        current = _authority(root, revision=8)
        write_catalog_cache(path, _snapshot(original))
        before = path.read_bytes()

        result = read_catalog_cache(path, expected_authority=current)

        assert result.status == "source_changed"
        assert result.snapshot == _snapshot(original)
        assert result.stored_authority == original
        assert path.read_bytes() == before


def test_runtime_only_arguments_do_not_change_catalog_authority() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        authority = _authority(root)
        runtime_variant = replace(
            authority.execution_contract,
            runtime_args=("-c", "features.shell_snapshot=false"),
        )
        same_authority = build_catalog_authority(
            provider_id=authority.provider_id,
            provider_generation=authority.provider_generation,
            provider_revision=authority.provider_revision,
            provider_state_authority={
                "generation": authority.provider_state_generation,
                "revision": authority.provider_state_revision,
                "digest": authority.provider_state_digest,
            },
            execution_contract=runtime_variant,
            discovery_method=authority.discovery_method,
            discovery_version=authority.discovery_version,
        )
        catalog_variant = replace(
            authority.execution_contract,
            catalog_args=("-c", 'model_provider="other"'),
        )
        changed_authority = build_catalog_authority(
            provider_id=authority.provider_id,
            provider_generation=authority.provider_generation,
            provider_revision=authority.provider_revision,
            provider_state_authority={
                "generation": authority.provider_state_generation,
                "revision": authority.provider_state_revision,
                "digest": authority.provider_state_digest,
            },
            execution_contract=catalog_variant,
            discovery_method=authority.discovery_method,
            discovery_version=authority.discovery_version,
        )

        assert same_authority == authority
        assert changed_authority != authority


def test_strict_shapes_types_and_bounds_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        authority = _authority(root)
        valid_authority = authority.to_dict()
        authority_mutations = []
        boolean_revision = copy.deepcopy(valid_authority)
        boolean_revision["provider"]["revision"] = True
        authority_mutations.append(boolean_revision)
        noncanonical_generation = copy.deepcopy(valid_authority)
        noncanonical_generation["provider"]["generation"] = (
            PROVIDER_GENERATION.upper()
        )
        authority_mutations.append(noncanonical_generation)
        unknown_field = copy.deepcopy(valid_authority)
        unknown_field["unexpected"] = True
        authority_mutations.append(unknown_field)
        bad_state_digest = copy.deepcopy(valid_authority)
        bad_state_digest["provider_state"]["digest"] = "not-a-digest"
        authority_mutations.append(bad_state_digest)

        for mutation in authority_mutations:
            try:
                CatalogAuthority.from_dict(mutation)
            except CatalogAuthorityError:
                continue
            raise AssertionError(f"invalid authority was accepted: {mutation}")

        invalid_snapshots = [
            {"models": ["duplicate", "duplicate"]},
            {"models": [""]},
            {"retired": [{"id": "retired", "first_absent_at": math.inf}]},
            {"last_refreshed_at": True},
            {"last_fetch_state": "fresh"},
        ]
        for mutation in invalid_snapshots:
            kwargs = {
                "authority": authority,
                "models": ["model"],
                "retired": [],
                "last_refreshed_at": 1.0,
                "last_fetch_state": "ok",
                **mutation,
            }
            try:
                build_catalog_snapshot(**kwargs)
            except CatalogCacheError:
                continue
            raise AssertionError(f"invalid snapshot was accepted: {mutation}")


def test_write_is_idempotent_atomic_and_concurrency_safe() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        path = root / "models.json"
        authority = _authority(root)
        first = _snapshot(authority)
        second = build_catalog_snapshot(
            authority=authority,
            models=["gpt-5.6-luna"],
            retired=[],
            last_refreshed_at=1_722_000_002.0,
            last_fetch_state="ok",
        )

        assert write_catalog_cache(path, first)
        os.utime(path, ns=(123_456_789, 123_456_789))
        first_mtime = path.stat().st_mtime_ns
        assert not write_catalog_cache(path, first)
        assert path.stat().st_mtime_ns == first_mtime

        barrier = threading.Barrier(5)
        errors: list[BaseException] = []

        def writer(snapshot: CatalogSnapshot) -> None:
            try:
                barrier.wait()
                for _ in range(20):
                    write_catalog_cache(path, snapshot)
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(snapshot,))
            for snapshot in (first, second, first, second)
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        assert not errors
        result = read_catalog_cache(path, expected_authority=authority)
        assert result.status == "matching"
        assert result.snapshot in {first, second}
        assert not list(root.glob(f".{path.name}.*.tmp"))


def test_provider_state_authority_shape_parser_is_shared() -> None:
    valid = _state_authority()
    assert provider_sync_authority.parse_authority(valid) == valid
    for invalid in (
        {**valid, "revision": True},
        {**valid, "generation": STATE_GENERATION.upper()},
        {**valid, "digest": "x" * 64},
        {**valid, "extra": 1},
    ):
        try:
            provider_sync_authority.parse_authority(invalid)
        except ValueError:
            continue
        raise AssertionError(f"invalid provider state authority was accepted: {invalid}")


TESTS = (
    test_schema3_round_trip_binds_existing_authorities_without_secrets,
    test_schema2_corrupt_missing_and_tampered_authority_are_removed,
    test_oversize_and_symlink_cache_files_are_removed_without_following,
    test_path_replacement_is_rejected_before_reading_replacement_bytes,
    test_access_time_change_does_not_invalidate_stable_cache,
    test_valid_but_different_source_is_preserved_and_never_authorized,
    test_runtime_only_arguments_do_not_change_catalog_authority,
    test_strict_shapes_types_and_bounds_fail_closed,
    test_write_is_idempotent_atomic_and_concurrency_safe,
    test_provider_state_authority_shape_parser_is_shared,
)


if __name__ == "__main__":
    for test in TESTS:
        test()
    print("PASS model catalog cache schema 3")
