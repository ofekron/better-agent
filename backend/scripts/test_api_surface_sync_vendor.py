from __future__ import annotations

import hashlib
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor" / "api-surface-sync" / "0.2.0"
WHEEL = VENDOR / "api_surface_sync-0.2.0-py3-none-any.whl"
SOURCE_COMMIT = "9e33a567286006e0736debee17b45dc9bca8dd15"
WHEEL_SHA256 = "0dbf44582beafd3efb51d45e118634ab51906f706119278cdfc83b872f4a13fb"
REQUIREMENT = (
    "../vendor/api-surface-sync/0.2.0/"
    "api_surface_sync-0.2.0-py3-none-any.whl"
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


def test_vendor_wheel_contains_v02_typed_runtime() -> None:
    with ZipFile(WHEEL) as archive:
        names = set(archive.namelist())
        metadata_name = next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_name).decode("utf-8")

    assert "Name: api-surface-sync\n" in metadata
    assert "Version: 0.2.0\n" in metadata
    assert "api_surface_sync/py.typed" in names
    assert "api_surface_sync/registry.py" in names
    assert "api_surface_sync/sdk.py" in names


if __name__ == "__main__":
    test_vendor_provenance_and_requirement_pin()
    test_vendor_wheel_contains_v02_typed_runtime()
    print("api-surface-sync vendor pin: ok")
