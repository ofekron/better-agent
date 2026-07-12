from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grouped_durability_writer import BatchSnapshot, GroupedDurabilityWriter


def _test_grouped_replace_unlink_and_ack(root: Path) -> None:
    parent = root / "state"
    removed = parent / "removed.json"
    parent.mkdir(parents=True)
    removed.write_bytes(b"old")
    snapshots: list[tuple[str, BatchSnapshot]] = []
    release = threading.Event()

    def hook(phase: str, snapshot: BatchSnapshot) -> None:
        snapshots.append((phase, snapshot))
        if phase == "after_temp_flush":
            release.wait(2)

    writer = GroupedDurabilityWriter(max_batch_size=3, max_batch_age_s=0.2, crash_hook=hook)
    receipts = [
        writer.replace(parent / "session.json", b'{"generation":1}'),
        writer.replace(parent / "drafts.json", b'{"generation":2}'),
        writer.unlink(removed),
    ]
    release.set()
    writer.drain(timeout=3)
    assert (parent / "session.json").read_bytes() == b'{"generation":1}'
    assert (parent / "drafts.json").read_bytes() == b'{"generation":2}'
    assert not removed.exists()
    assert [receipt.wait() for receipt in receipts] == [3, 3, 3]
    dir_snapshot = next(snapshot for phase, snapshot in snapshots if phase == "after_dir_fsync")
    assert dir_snapshot.parent_dirs == (parent,)
    writer.close(timeout=3)


def _test_crash_boundaries(root: Path) -> None:
    phases = ("after_temp_flush", "after_file_fsync", "after_mutation", "after_dir_fsync", "before_ack")
    for phase_to_fail in phases:
        target = root / phase_to_fail / "session.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old")

        def hook(phase: str, _snapshot: BatchSnapshot, expected: str = phase_to_fail) -> None:
            if phase == expected:
                raise RuntimeError(expected)

        writer = GroupedDurabilityWriter(max_batch_age_s=0, crash_hook=hook)
        receipt = writer.replace(target, b"new")
        try:
            receipt.wait(timeout=3)
        except RuntimeError as exc:
            assert str(exc) == phase_to_fail
        else:
            raise AssertionError(f"{phase_to_fail} did not fail its generation")
        writer.close(timeout=3)
        debris = [path for path in target.parent.iterdir() if path.name.endswith(".durability.tmp")]
        assert debris == [], debris
        expected = b"old" if phase_to_fail in ("after_temp_flush", "after_file_fsync") else b"new"
        assert target.read_bytes() == expected


def _test_bounded_batch_and_shutdown_drain(root: Path) -> None:
    batches: list[tuple[int, ...]] = []

    def hook(phase: str, snapshot: BatchSnapshot) -> None:
        if phase == "before_ack":
            batches.append(snapshot.generations)

    writer = GroupedDurabilityWriter(max_batch_size=2, max_batch_age_s=0.05, crash_hook=hook)
    receipts = [writer.replace(root / f"{index}.json", str(index).encode()) for index in range(5)]
    writer.close(timeout=3)
    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert [receipt.wait() for receipt in receipts] == [2, 2, 4, 4, 5]
    for index in range(5):
        assert (root / f"{index}.json").read_bytes() == str(index).encode()
    try:
        writer.unlink(root / "late")
    except RuntimeError:
        pass
    else:
        raise AssertionError("closed writer accepted new work")


def _test_receipt_identity_survives_post_ack_replacement(root: Path) -> None:
    target = root / "session.json"
    writer = GroupedDurabilityWriter(max_batch_age_s=0)
    receipt = writer.replace(target, b"committed")
    receipt.wait(timeout=3)
    committed = receipt.signature
    assert committed is not None
    replacement = root / "replacement.tmp"
    replacement.write_bytes(b"external")
    os.replace(replacement, target)
    current = GroupedDurabilityWriter._signature(target.stat())
    assert receipt.signature == committed
    assert current != committed
    writer.close(timeout=3)


def _test_same_inode_mutation_before_ack_is_rejected(root: Path) -> None:
    for phase_to_mutate in ("after_mutation", "after_dir_fsync", "before_ack"):
        phase_root = root / phase_to_mutate
        phase_root.mkdir()
        target = phase_root / "session.json"
        other = phase_root / "other.json"

        def mutate(phase: str, _snapshot: BatchSnapshot) -> None:
            if phase != phase_to_mutate:
                return
            with target.open("r+b") as handle:
                handle.seek(0)
                handle.write(b"intruder!")
                handle.flush()
                os.fsync(handle.fileno())

        def canonical(path: Path):
            return GroupedDurabilityWriter._path_signature(path)

        writer = GroupedDurabilityWriter(
            max_batch_size=2,
            max_batch_age_s=0.2,
            crash_hook=mutate,
            signature_resolver=canonical,
        )
        receipts = (
            writer.replace(target, b"committed"),
            writer.replace(other, b"untouched"),
        )
        for receipt in receipts:
            try:
                receipt.wait(timeout=3)
            except RuntimeError as exc:
                assert "identity changed" in str(exc)
            else:
                raise AssertionError(
                    f"same-inode mutation at {phase_to_mutate} was acknowledged"
                )
        writer.close(timeout=3)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="grouped-durability-") as tmp:
        root = Path(tmp)
        _test_grouped_replace_unlink_and_ack(root / "group")
        _test_crash_boundaries(root / "crashes")
        _test_bounded_batch_and_shutdown_drain(root / "bounded")
        (root / "identity").mkdir()
        _test_receipt_identity_survives_post_ack_replacement(root / "identity")
        (root / "in-place").mkdir()
        _test_same_inode_mutation_before_ack_is_rejected(root / "in-place")
    print("grouped durability writer: ok")


if __name__ == "__main__":
    main()
