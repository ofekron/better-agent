import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bff_projection_registry import ProjectionRegistry, ProjectionMismatch


def test_revision_survives_payload_eviction_and_schema_mints_epoch():
    path = Path(tempfile.mkdtemp()) / "projection.sqlite"
    registry = ProjectionRegistry(path)
    first = registry.publish("root", canonical_through_seq=4, checksum="a", schema_version=1)
    second = registry.publish("root", canonical_through_seq=5, checksum="b", schema_version=1)
    registry.close()
    reopened = ProjectionRegistry(path)
    state = reopened.get("root")
    assert state.epoch == first.epoch
    assert state.revision == second.revision == 2
    changed = reopened.publish("root", canonical_through_seq=5, checksum="c", schema_version=2)
    assert changed.epoch != state.epoch and changed.revision == 1
    reopened.close()


def test_same_source_revision_must_rebuild_identically():
    path = Path(tempfile.mkdtemp()) / "projection.sqlite"
    registry = ProjectionRegistry(path)
    registry.publish("root", canonical_through_seq=4, checksum="a", schema_version=1)
    try:
        registry.publish("root", canonical_through_seq=4, checksum="different", schema_version=1)
        raise AssertionError("expected mismatch")
    except ProjectionMismatch:
        pass
    registry.close()


if __name__ == "__main__":
    test_revision_survives_payload_eviction_and_schema_mints_epoch()
    test_same_source_revision_must_rebuild_identically()
    print("BFF projection registry tests passed")
