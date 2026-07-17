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
            journal_through_seq=7,
            message_heads={"root": 2},
        )
        raise AssertionError("missing database must fail")
    except AuthorityError:
        pass
    assert catalog.current("root").authority == "jsonl"
    database.touch()
    cutover = catalog.commit_sqlite_cutover(
        "root", root.root_generation, database_path=database, canonical_through_seq=4,
        journal_through_seq=7,
        message_heads={"root": 2},
    )
    assert cutover.authority == "sqlite"
    advanced = catalog.advance_coverage(
        "root", root.root_generation, canonical_through_seq=5,
        journal_through_seq=8, message_heads={"root": 3, "fork": 4},
    )
    assert advanced.journal_through_seq == 8
    assert catalog.current("root").message_heads == {"root": 3, "fork": 4}
    try:
        catalog.advance_coverage(
            "root", root.root_generation, canonical_through_seq=5,
            journal_through_seq=8, message_heads={"root": 3},
        )
        raise AssertionError("dropping a covered node head must fail closed")
    except AuthorityError:
        pass
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
    catalog.begin_delete("root", first.root_generation)
    assert catalog.current("root").authority == "deleting"
    catalog.finish_delete("root", first.root_generation)
    reused = catalog.create("root")
    assert reused.root_generation == first.root_generation + 1
    catalog.close()


def test_prior_schema_fails_with_explicit_version_error():
    import sqlite3

    path = HOME / "prior-schema.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE root_authority(root_id TEXT)")
    connection.commit()
    connection.close()
    try:
        RuntimeAuthorityCatalog(path)
        raise AssertionError("prior schema must fail closed")
    except AuthorityError as exc:
        assert "unsupported canonical authority catalog schema" in str(exc)


if __name__ == "__main__":
    test_cutover_is_atomic_and_missing_authority_fails_closed()
    test_delete_and_reuse_mints_generation()
    test_prior_schema_fails_with_explicit_version_error()
    print("canonical authority tests passed")
