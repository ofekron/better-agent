#!/usr/bin/env python3
from __future__ import annotations

import json
import multiprocessing
import os
import sys
import tempfile
import threading
import time
import types
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import active_run_catalog
import event_journal
import json_store
import runs_dir
from event_ingester import EventIngester
from event_journal import Event, EventJournalWriter, TurnBoundary


def test_active_catalog_rebuild_and_retire() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "runs"
        run = root / "run-1"
        run.mkdir(parents=True)
        state = {"run_id": "run-1", "provider_id": "claude:test"}
        (run / "backend_state.json").write_text(json.dumps(state))
        catalog, rebuilt = active_run_catalog.load_or_rebuild(root)
        assert rebuilt and catalog == {"run-1": {"provider_id": "claude:test"}}
        active_run_catalog._path(root).write_text("{")
        catalog, rebuilt = active_run_catalog.load_or_rebuild(root)
        assert rebuilt and "run-1" in catalog
        active_run_catalog.mark_dirty(root)
        catalog, rebuilt = active_run_catalog.load_or_rebuild(root)
        assert rebuilt and "run-1" in catalog
        active_run_catalog.retire(root, "run-1")
        assert active_run_catalog.load(root) == {}


def test_rebuild_retains_unreadable_runs_and_batch_retires_once() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "runs"
        for index in range(1000):
            run = root / f"run-{index}"
            run.mkdir(parents=True)
            if index:
                (run / "backend_state.json").write_text(json.dumps({"provider_id": "p"}))
            else:
                (run / "backend_state.json").write_text("{")
        catalog, rebuilt = active_run_catalog.load_or_rebuild(root)
        assert rebuilt and len(catalog) == 1000
        assert catalog["run-0"] == {"provider_id": None}
        writes = 0
        original_write = active_run_catalog._write
        def counted_write(path, runs):
            nonlocal writes
            writes += 1
            return original_write(path, runs)
        active_run_catalog._write = counted_write
        try:
            active_run_catalog.mark_dirty(root)
            active_run_catalog.retire_many(root, [f"run-{index}" for index in range(500)])
        finally:
            active_run_catalog._write = original_write
        assert writes == 1
        assert len(active_run_catalog.load(root) or {}) == 500


def test_catalog_registration_is_path_confined() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "runs"
        path = root / "run-1" / "backend_state.json"
        path.parent.mkdir(parents=True)
        active_run_catalog.register(path, {"run_id": "run-1", "provider_id": "codex:test"})
        assert active_run_catalog.load(root) == {"run-1": {"provider_id": "codex:test"}}
        try:
            active_run_catalog.register(path, {"run_id": "../escape"})
        except ValueError:
            pass
        else:
            raise AssertionError("traversal run id accepted")


def test_catalog_two_writer_dirty_generation_survives_peer_crash() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "runs"
        ready = threading.Barrier(2)
        first_done = threading.Event()

        def writer(run_id: str, complete: bool) -> None:
            run_dir = root / run_id
            run_dir.mkdir(parents=True)
            state = {"run_id": run_id, "provider_id": f"claude:{run_id}"}
            token = active_run_catalog.mark_dirty(root)
            (run_dir / "backend_state.json").write_text(json.dumps(state))
            ready.wait()
            if complete:
                active_run_catalog.register(
                    run_dir / "backend_state.json",
                    state,
                    dirty_token=token,
                )
                first_done.set()
            else:
                first_done.wait()

        first = threading.Thread(target=writer, args=("run-a", True))
        crashed = threading.Thread(target=writer, args=("run-b", False))
        first.start()
        crashed.start()
        first.join()
        crashed.join()
        assert active_run_catalog.load(root) is None
        assert len(list(root.glob("active_run_catalog.dirty.*"))) == 1
        rebuilt, did_rebuild = active_run_catalog.load_or_rebuild(root)
        assert did_rebuild
        assert set(rebuilt) == {"run-a", "run-b"}
        assert not list(root.glob("active_run_catalog.dirty*"))


def test_typed_catalog_intents_repair_crash_windows_without_full_scan() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "runs"
        root.mkdir()
        active_run_catalog.load_or_rebuild(root)

        missing_register = active_run_catalog.mark_dirty(root, {
            "operation": "register",
            "runs": [{"run_id": "not-durable", "provider_id": "claude:test"}],
        })
        assert (root / f"active_run_catalog.dirty.{missing_register}").exists()
        catalog, repaired = active_run_catalog.load_or_rebuild(root)
        assert repaired and "not-durable" not in catalog

        durable = root / "durable"
        durable.mkdir()
        state = {"run_id": "durable", "provider_id": "claude:test"}
        token = active_run_catalog.mark_dirty(root, {
            "operation": "register",
            "runs": [{"run_id": "durable", "provider_id": "claude:test"}],
        })
        json_store.write_json_durable(durable / "backend_state.json", state)
        assert (root / f"active_run_catalog.dirty.{token}").exists()
        catalog, repaired = active_run_catalog.load_or_rebuild(root)
        assert repaired and catalog["durable"] == {"provider_id": "claude:test"}

        token = active_run_catalog.mark_dirty(root, {
            "operation": "retire",
            "runs": [{"run_id": "durable", "provider_id": "claude:test"}],
        })
        catalog, repaired = active_run_catalog.load_or_rebuild(root)
        assert repaired and "durable" in catalog

        token = active_run_catalog.mark_dirty(root, {
            "operation": "retire",
            "runs": [{"run_id": "durable", "provider_id": "claude:test"}],
        })
        from ingestion_versions import current_ingestion_version
        json_store.write_json_durable(durable / "reconciled.marker", {
            "provider_kind": "claude",
            "ingestion_version": current_ingestion_version("claude"),
        })
        active_run_catalog.mark_dirty(root, {
            "operation": "register",
            "runs": [{"run_id": "durable", "provider_id": "claude:test"}],
        })
        catalog, repaired = active_run_catalog.load_or_rebuild(root)
        assert repaired and "durable" not in catalog


def test_typed_catalog_repair_is_bounded_by_dirty_intents() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "runs"
        root.mkdir()
        active_run_catalog.load_or_rebuild(root)
        for index in range(20_000):
            (root / f"historical-{index}").mkdir()
        active = root / "active"
        active.mkdir()
        state = {"run_id": "active", "provider_id": "claude:test"}
        token = active_run_catalog.mark_dirty(root, {
            "operation": "register",
            "runs": [{"run_id": "active", "provider_id": "claude:test"}],
        })
        json_store.write_json_durable(active / "backend_state.json", state)
        original_scan = active_run_catalog._scan
        active_run_catalog._scan = lambda _root: (_ for _ in ()).throw(
            AssertionError("typed repair performed exhaustive scan")
        )
        started = time.perf_counter()
        try:
            catalog, repaired = active_run_catalog.load_or_rebuild(root)
        finally:
            active_run_catalog._scan = original_scan
        elapsed = time.perf_counter() - started
        assert repaired and catalog == {"active": {"provider_id": "claude:test"}}
        assert elapsed < 1.0, f"typed repair took {elapsed:.3f}s"
        assert not (root / f"active_run_catalog.dirty.{token}").exists()


def test_corrupt_typed_catalog_intent_falls_back_to_authority_scan() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "runs"
        run = root / "run-1"
        run.mkdir(parents=True)
        state = {"run_id": "run-1", "provider_id": "claude:test"}
        json_store.write_json_durable(run / "backend_state.json", state)
        active_run_catalog.load_or_rebuild(root)
        token = active_run_catalog.mark_dirty(root, {
            "operation": "register",
            "runs": [{"run_id": "run-1", "provider_id": "claude:test"}],
        })
        (root / f"active_run_catalog.dirty.{token}").write_text("{}")
        scans = 0
        original_scan = active_run_catalog._scan

        def counted_scan(path: Path) -> dict[str, dict]:
            nonlocal scans
            scans += 1
            return original_scan(path)

        active_run_catalog._scan = counted_scan
        try:
            catalog, rebuilt = active_run_catalog.load_or_rebuild(root)
        finally:
            active_run_catalog._scan = original_scan
        assert rebuilt and scans == 1 and "run-1" in catalog


def test_public_catalog_apis_recover_crashes_without_scan_or_lost_run() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "runs"
        root.mkdir()
        active_run_catalog.load_or_rebuild(root)
        run = root / "run-public"
        run.mkdir()
        state = {"run_id": "run-public", "provider_id": "claude:test"}
        json_store.write_json_durable(run / "backend_state.json", state)

        original_write = active_run_catalog._write
        active_run_catalog._write = lambda *_args: (_ for _ in ()).throw(
            OSError("injected catalog register crash")
        )
        try:
            try:
                active_run_catalog.register(run / "backend_state.json", state)
            except OSError:
                pass
            else:
                raise AssertionError("register crash was not injected")
        finally:
            active_run_catalog._write = original_write
        assert list(root.glob("active_run_catalog.dirty.*"))

        original_scan = active_run_catalog._scan
        active_run_catalog._scan = lambda _root: (_ for _ in ()).throw(
            AssertionError("public register recovery performed exhaustive scan")
        )
        try:
            catalog, repaired = active_run_catalog.load_or_rebuild(root)
        finally:
            active_run_catalog._scan = original_scan
        assert repaired and catalog["run-public"] == {"provider_id": "claude:test"}

        from ingestion_versions import current_ingestion_version
        json_store.write_json_durable(run / "reconciled.marker", {
            "provider_kind": "claude",
            "ingestion_version": current_ingestion_version("claude"),
        })
        active_run_catalog._write = lambda *_args: (_ for _ in ()).throw(
            OSError("injected catalog retire crash")
        )
        try:
            try:
                active_run_catalog.retire_many(root, ["run-public"])
            except OSError:
                pass
            else:
                raise AssertionError("retire crash was not injected")
        finally:
            active_run_catalog._write = original_write
        assert list(root.glob("active_run_catalog.dirty.*"))

        active_run_catalog._scan = lambda _root: (_ for _ in ()).throw(
            AssertionError("public retire recovery performed exhaustive scan")
        )
        try:
            catalog, repaired = active_run_catalog.load_or_rebuild(root)
        finally:
            active_run_catalog._scan = original_scan
        assert repaired and "run-public" not in catalog


def test_windows_typed_intent_is_atomic_and_clear_flushes_root() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "runs"
        root.mkdir()
        writes: list[tuple[str, bytes]] = []
        deleted: list[str] = []
        flushed: list[str] = []

        class FakeStat:
            reparse = False

        class FakeWindowsNativeOps:
            def open_root(self, path: Path):
                return path

            def stat(self, _handle):
                return FakeStat()

            def delete_relative(self, handle: Path, name: str) -> None:
                deleted.append(name)
                (handle / name).unlink(missing_ok=True)

            def flush(self, handle: Path) -> None:
                flushed.append(str(handle))

            def close(self, _handle) -> None:
                return None

        def fake_write_atomic_file(_ops, path: Path, name: str, payload: bytes):
            writes.append((name, payload))
            (path / name).write_bytes(payload)

        fake_module = types.SimpleNamespace(
            WindowsNativeOps=FakeWindowsNativeOps,
            write_atomic_file=fake_write_atomic_file,
        )

        class OsProxy:
            name = "nt"

            def __getattr__(self, name: str):
                return getattr(os, name)

        original_os = active_run_catalog.os
        original_windows_module = sys.modules.get("windows_handle_marker")
        active_run_catalog.os = OsProxy()
        sys.modules["windows_handle_marker"] = fake_module
        try:
            token = active_run_catalog.mark_dirty(root, {
                "operation": "register",
                "runs": [{"run_id": "run-win", "provider_id": "codex:test"}],
            })
            token_name = f"active_run_catalog.dirty.{token}"
            payload = dict(writes)[token_name]
            assert json.loads(payload) == {
                "version": 1,
                "operation": "register",
                "runs": [{"run_id": "run-win", "provider_id": "codex:test"}],
            }
            assert "active_run_catalog.dirty" in dict(writes)
            active_run_catalog.clear_dirty(root, token)
            assert token_name in deleted
            assert "active_run_catalog.dirty" in deleted
            assert flushed == [str(root)]
        finally:
            active_run_catalog.os = original_os
            if original_windows_module is None:
                sys.modules.pop("windows_handle_marker", None)
            else:
                sys.modules["windows_handle_marker"] = original_windows_module


def test_explicit_transaction_token_cannot_be_stolen_by_implicit_writer() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "runs"
        root.mkdir()
        with active_run_catalog.transaction(root) as catalog:
            abandoned = catalog.mark_dirty()
        run_dir = root / "implicit-run"
        run_dir.mkdir()
        state = {"run_id": "implicit-run", "provider_id": "claude:test"}
        (run_dir / "backend_state.json").write_text(json.dumps(state))
        active_run_catalog.register(run_dir / "backend_state.json", state)
        assert (root / f"active_run_catalog.dirty.{abandoned}").exists()
        assert active_run_catalog.load(root) is None


def test_catalog_read_is_pinned_across_root_swap() -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as temp:
        parent = Path(temp)
        root = parent / "runs"
        displaced = parent / "runs-displaced"
        root.mkdir()
        active_run_catalog.load_or_rebuild(root)
        replacement = parent / "replacement"
        original_read = os.read
        swapped = False

        def swapping_read(fd: int, size: int) -> bytes:
            nonlocal swapped
            data = original_read(fd, size)
            if not swapped:
                swapped = True
                root.rename(displaced)
                replacement.mkdir()
                replacement.rename(root)
            return data

        os.read = swapping_read
        replacement_written = False
        try:
            try:
                active_run_catalog.load_or_rebuild(root)
            except OSError:
                pass
            else:
                raise AssertionError("catalog root swap during pinned read was accepted")
        finally:
            os.read = original_read
            replacement_written = (root / "active_run_catalog.json").exists()
            if root.exists():
                root.rmdir()
            displaced.rename(root)
        assert not replacement_written


def test_rebuild_rejects_swap_after_marker_or_backend_state_read() -> None:
    if os.name == "nt":
        return
    for target_name in ("reconciled.marker", "backend_state.json"):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            root = parent / "runs"
            displaced = parent / "runs-displaced"
            root.mkdir()
            run_dir = root / "run-a"
            run_dir.mkdir()
            (run_dir / "backend_state.json").write_text(json.dumps({
                "run_id": "run-a", "provider_id": "claude:test",
            }))
            if target_name == "reconciled.marker":
                (run_dir / target_name).write_text("{}")
            active_run_catalog.mark_dirty(root)
            original_read = active_run_catalog.read_relative
            swapped = False

            def swapping_read(requested: Path, *components: str) -> bytes:
                nonlocal swapped
                data = original_read(requested, *components)
                if not swapped and components[-1] == target_name:
                    swapped = True
                    requested.rename(displaced)
                    requested.mkdir()
                return data

            active_run_catalog.read_relative = swapping_read
            replacement_written = False
            try:
                try:
                    active_run_catalog.load_or_rebuild(root)
                except OSError:
                    pass
                else:
                    raise AssertionError(f"root swap after {target_name} read was accepted")
            finally:
                active_run_catalog.read_relative = original_read
                replacement_written = bool(list(root.iterdir()))
                root.rmdir()
                displaced.rename(root)
            assert swapped
            assert not replacement_written


def _catalog_writer_crash_after_authority(
    root_text: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    root = Path(root_text)
    run_dir = root / "run-crash"
    run_dir.mkdir(parents=True, exist_ok=True)
    state = {"run_id": "run-crash", "provider_id": "claude:test"}
    with active_run_catalog.transaction(root) as catalog:
        catalog.mark_dirty()
        ready.set()
        release.wait(10)
        json_store.write_json_durable(run_dir / "backend_state.json", state)
        os._exit(91)


def test_rebuild_waits_for_live_writer_and_recovers_crash() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "runs"
        root.mkdir()
        active_run_catalog.load_or_rebuild(root)
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        child = context.Process(
            target=_catalog_writer_crash_after_authority,
            args=(str(root), ready, release),
        )
        child.start()
        assert ready.wait(5)
        result: list[tuple[dict[str, dict], bool]] = []
        recovery = threading.Thread(
            target=lambda: result.append(active_run_catalog.load_or_rebuild(root)),
        )
        recovery.start()
        time.sleep(0.1)
        assert recovery.is_alive(), "rebuild bypassed live writer transaction"
        release.set()
        child.join(5)
        assert child.exitcode == 91
        recovery.join(5)
        assert not recovery.is_alive()
        assert result[0][1] is True
        assert result[0][0]["run-crash"]["provider_id"] == "claude:test"


def test_catalog_rejects_runs_root_swap_before_lock() -> None:
    if not hasattr(os, "symlink"):
        return
    with tempfile.TemporaryDirectory() as temp:
        parent = Path(temp)
        root = parent / "runs"
        original = parent / "runs-original"
        outside = parent / "outside"
        root.mkdir()
        outside.mkdir()
        real_lock = runs_dir.run_catalog_lock

        @contextmanager
        def swapped_lock(requested: Path):
            requested.rename(original)
            requested.symlink_to(outside, target_is_directory=True)
            with real_lock(requested):
                yield

        runs_dir.run_catalog_lock = swapped_lock
        try:
            try:
                with active_run_catalog.transaction(root) as catalog:
                    catalog.mark_dirty()
            except OSError:
                pass
            else:
                raise AssertionError("replaced runs root was accepted")
        finally:
            runs_dir.run_catalog_lock = real_lock
            if root.is_symlink():
                root.unlink()
            if original.exists():
                original.rename(root)
        assert not list(outside.iterdir())


def test_catalog_rejects_runs_root_swap_during_transaction() -> None:
    if not hasattr(os, "symlink"):
        return
    with tempfile.TemporaryDirectory() as temp:
        parent = Path(temp)
        root = parent / "runs"
        original = parent / "runs-original"
        outside = parent / "outside"
        root.mkdir()
        outside.mkdir()
        with active_run_catalog.transaction(root) as catalog:
            root.rename(original)
            root.symlink_to(outside, target_is_directory=True)
            try:
                catalog.mark_dirty()
            except OSError:
                pass
            else:
                raise AssertionError("runs root swap during transaction was accepted")
            root.unlink()
            original.rename(root)
        assert not list(outside.iterdir())


def test_ownership_checkpoint_validation_and_tail_cursor() -> None:
    with tempfile.TemporaryDirectory() as temp:
        artifacts = Path(temp) / "root"
        artifacts.mkdir()
        journal = artifacts / "events.jsonl"
        journal.write_bytes(b'{"seq":1}\n{"seq":2}\n')
        original = event_journal._session_artifacts_dir
        original_ingester = event_journal.event_ingester
        ingester = EventIngester()
        ingester._events_path = lambda _root_id: journal
        ingester._root_dir = lambda _root_id: artifacts
        event_journal._session_artifacts_dir = lambda _root_id: artifacts
        event_journal.event_ingester = ingester
        try:
            writer = EventJournalWriter()
            writer._turn_messages[("root", "turn")] = "msg"
            writer._turn_boundaries[("root", "root")] = [
                TurnBoundary(datetime.now(timezone.utc), "turn", "msg")
            ]
            writer._pending_events["root"] = {
                2: Event("root", "root", "assistant", {}, "test")
            }
            writer._write_ownership_checkpoint("root", 2)
            restored = EventJournalWriter()
            assert restored._load_ownership_checkpoint("root") == 1
            assert restored._turn_messages[("root", "turn")] == "msg"
            checkpoint = writer._ownership_checkpoint_path("root")
            raw = json.loads(checkpoint.read_text())
            raw["journal"]["covered_size"] = journal.stat().st_size + 1
            checkpoint.write_text(json.dumps(raw))
            invalid = EventJournalWriter()
            assert invalid._load_ownership_checkpoint("root") is None
            writer._write_ownership_checkpoint("root", 1)
            with journal.open("r+b") as handle:
                handle.seek(0)
                handle.write(b"X")
            tampered = EventJournalWriter()
            assert tampered._load_ownership_checkpoint("root") is None
            writer.close()
            restored.close()
            invalid.close()
            tampered.close()
        finally:
            event_journal._session_artifacts_dir = original
            event_journal.event_ingester = original_ingester
            ingester.close("root")


def test_ingester_fence_for_thousand_rows() -> None:
    with tempfile.TemporaryDirectory() as temp:
        journal = Path(temp) / "events.jsonl"
        lines = [json.dumps({"seq": index + 1}).encode() + b"\n" for index in range(1000)]
        journal.write_bytes(b"".join(lines))
        ingester = EventIngester()
        original = ingester._events_path
        original_root = ingester._root_dir
        ingester._events_path = lambda _root_id: journal
        ingester._root_dir = lambda _root_id: Path(temp)
        try:
            token = ingester.ownership_checkpoint_token("root")
            fence = ingester.commit_ownership_snapshot(
                "root", token=token, covered_seq=999,
                checkpoint_path=Path(temp) / "ownership.json",
                payload={"version": 1, "state": {}},
            )
            assert fence is not None
            assert fence["covered_size"] == sum(map(len, lines[:999]))
            assert len(fence["digest"]) == 64
        finally:
            ingester._events_path = original
            ingester._root_dir = original_root
            ingester.close("root")


def main() -> int:
    test_active_catalog_rebuild_and_retire()
    test_catalog_registration_is_path_confined()
    test_catalog_two_writer_dirty_generation_survives_peer_crash()
    test_typed_catalog_intents_repair_crash_windows_without_full_scan()
    test_typed_catalog_repair_is_bounded_by_dirty_intents()
    test_corrupt_typed_catalog_intent_falls_back_to_authority_scan()
    test_public_catalog_apis_recover_crashes_without_scan_or_lost_run()
    test_windows_typed_intent_is_atomic_and_clear_flushes_root()
    test_explicit_transaction_token_cannot_be_stolen_by_implicit_writer()
    test_catalog_read_is_pinned_across_root_swap()
    test_rebuild_rejects_swap_after_marker_or_backend_state_read()
    test_rebuild_waits_for_live_writer_and_recovers_crash()
    test_catalog_rejects_runs_root_swap_before_lock()
    test_catalog_rejects_runs_root_swap_during_transaction()
    test_rebuild_retains_unreadable_runs_and_batch_retires_once()
    test_ownership_checkpoint_validation_and_tail_cursor()
    test_ingester_fence_for_thousand_rows()
    print("PASS recovery performance indexes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
