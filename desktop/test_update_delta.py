from __future__ import annotations

import gzip
import hashlib
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path

from update_delta import DELTA_FORMAT, apply_patches, create_patch

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


@dataclass(order=True)
class _PatchMeta:
    version: str
    custom_internal: dict

    __hash__ = object.__hash__


def _archive(path: Path, content: bytes) -> None:
    with gzip.open(path, "wb") as archive:
        archive.write(content)


def _read_archive(path: Path) -> bytes:
    with gzip.open(path, "rb") as archive:
        return archive.read()


def test_cumulative_xdelta_round_trip() -> bool:
    with tempfile.TemporaryDirectory(prefix="ba-delta-test-") as raw_root:
        root = Path(raw_root)
        source = root / "source.tar.gz"
        middle = root / "middle.tar.gz"
        target = root / "target.tar.gz"
        first_patch = root / "middle.patch"
        second_patch = root / "target.patch"
        reconstructed = root / "reconstructed.tar.gz"

        base = bytearray((b"stable-block-" * 8192) + (b"A" * 2_000_000))
        changed = bytearray(base)
        changed[120_000:120_020] = b"first-version-change"
        final = bytearray(changed)
        final[1_500_000:1_500_020] = b"second-version-edit!"

        _archive(source, bytes(base))
        _archive(middle, bytes(changed))
        _archive(target, bytes(final))

        first_meta = create_patch(source, middle, first_patch)
        second_meta = create_patch(middle, target, second_patch)
        apply_patches(
            source,
            reconstructed,
            {
                _PatchMeta("1.1", first_meta): first_patch,
                _PatchMeta("1.2", second_meta): second_patch,
            },
        )

        return (
            first_meta["delta_format"] == DELTA_FORMAT
            and second_meta["delta_format"] == DELTA_FORMAT
            and _read_archive(reconstructed) == bytes(final)
            and hashlib.sha256(bytes(final)).hexdigest()
            == second_meta["tar_hash"]
        )


def test_wrong_format_fails_without_destination() -> bool:
    with tempfile.TemporaryDirectory(prefix="ba-delta-test-") as raw_root:
        root = Path(raw_root)
        source = root / "source.tar.gz"
        target = root / "target.tar.gz"
        patch = root / "target.patch"
        reconstructed = root / "reconstructed.tar.gz"
        _archive(source, b"source")
        _archive(target, b"target")
        metadata = create_patch(source, target, patch)
        metadata["delta_format"] = "unknown"

        try:
            apply_patches(
                source,
                reconstructed,
                {_PatchMeta("1.1", metadata): patch},
            )
        except ValueError:
            return not reconstructed.exists()
        return False


def test_corrupt_patch_fails_without_destination() -> bool:
    with tempfile.TemporaryDirectory(prefix="ba-delta-test-") as raw_root:
        root = Path(raw_root)
        source = root / "source.tar.gz"
        target = root / "target.tar.gz"
        patch = root / "target.patch"
        reconstructed = root / "reconstructed.tar.gz"
        _archive(source, b"source")
        _archive(target, b"target")
        metadata = create_patch(source, target, patch)
        patch.write_bytes(b"not an xdelta patch")

        try:
            apply_patches(
                source,
                reconstructed,
                {_PatchMeta("1.1", metadata): patch},
            )
        except Exception:
            return not reconstructed.exists()
        return False


TESTS = [
    ("cumulative xdelta patches reconstruct the final archive", test_cumulative_xdelta_round_trip),
    ("unknown patch formats fail without a destination archive", test_wrong_format_fails_without_destination),
    ("corrupt patches fail without a destination archive", test_corrupt_patch_fails_without_destination),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            ok = fn()
        except Exception:
            ok = False
            traceback.print_exc()
        print(f"{PASS if ok else FAIL}  {name}")
        failed += not ok
    return int(failed > 0)


if __name__ == "__main__":
    sys.exit(main_run())
