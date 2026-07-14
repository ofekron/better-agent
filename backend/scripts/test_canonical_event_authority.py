import os
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="ba-authority-"))
os.environ["BETTER_AGENT_HOME"] = str(HOME)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from canonical_event_authority import AuthorityError, RuntimeAuthorityCatalog


def test_cutover_is_atomic_and_missing_authority_fails_closed():
    catalog = RuntimeAuthorityCatalog(HOME / "catalog.sqlite")
    root = catalog.create("root")
    database = HOME / "root.sqlite"
    try:
        catalog.commit_sqlite_cutover(
            "root", root.root_generation, database_path=database, canonical_through_seq=4,
        )
        raise AssertionError("missing database must fail")
    except AuthorityError:
        pass
    assert catalog.current("root").authority == "jsonl"
    database.touch()
    cutover = catalog.commit_sqlite_cutover(
        "root", root.root_generation, database_path=database, canonical_through_seq=4,
    )
    assert cutover.authority == "sqlite"
    database.unlink()
    try:
        catalog.require_database("root")
        raise AssertionError("missing authoritative database must fail closed")
    except AuthorityError:
        pass
    catalog.close()


def test_delete_and_reuse_mints_generation():
    catalog = RuntimeAuthorityCatalog(HOME / "reuse-catalog.sqlite")
    first = catalog.create("root")
    catalog.delete("root", first.root_generation)
    reused = catalog.create("root")
    assert reused.root_generation == first.root_generation + 1
    catalog.close()


if __name__ == "__main__":
    test_cutover_is_atomic_and_missing_authority_fails_closed()
    test_delete_and_reuse_mints_generation()
    print("canonical authority tests passed")
