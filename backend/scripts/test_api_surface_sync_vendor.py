from __future__ import annotations

import hashlib
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor" / "api-surface-sync" / "0.2.1"
WHEEL = VENDOR / "api_surface_sync-0.2.1-py3-none-any.whl"
SOURCE_COMMIT = "bc012e8b547f3fc7b405919033b3a49b7ee0816e"
WHEEL_SHA256 = "5561f559b4456306906553a4636d3dcaa09281009dba098889411704031cc881"
REQUIREMENT = (
    "../vendor/api-surface-sync/0.2.1/"
    "api_surface_sync-0.2.1-py3-none-any.whl"
)


def test_vendor_provenance_and_requirement_pin() -> None:
    assert (VENDOR / "SOURCE_COMMIT").read_text(encoding="utf-8").strip() == SOURCE_COMMIT
    assert hashlib.sha256(WHEEL.read_bytes()).hexdigest() == WHEEL_SHA256
    assert (VENDOR / "SHA256SUMS").read_text(encoding="utf-8").strip() == (
        f"{WHEEL_SHA256}  {WHEEL.name}"
    )
    requirements = (ROOT / "backend" / "requirements.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    assert requirements.count(REQUIREMENT) == 1


def test_vendor_wheel_contains_typed_runtime() -> None:
    with ZipFile(WHEEL) as archive:
        names = set(archive.namelist())
        metadata_name = next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_name).decode("utf-8")

    assert "Name: api-surface-sync\n" in metadata
    assert "Version: 0.2.1\n" in metadata
    assert "api_surface_sync/py.typed" in names
    assert "api_surface_sync/registry.py" in names
    assert "api_surface_sync/sdk.py" in names


if __name__ == "__main__":
    test_vendor_provenance_and_requirement_pin()
    test_vendor_wheel_contains_typed_runtime()
    print("api-surface-sync vendor pin: ok")
