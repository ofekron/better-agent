from __future__ import annotations

import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-extension-install-atomicity-")

import extension_store


def _make_package(root: Path, marker: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "better-agent-extension.json").write_text(marker, encoding="utf-8")
    (root / "payload.txt").write_text(marker * 64, encoding="utf-8")
    return root


def test_failed_build_leaves_live_target_intact() -> None:
    """A live install directory must survive a failed reinstall.

    Extension records point at the content-addressed version directory and
    runtime-readiness stats it. Wiping it before a valid replacement existed
    left the extension not-runtime-ready, which failed every turn whose
    harness profile named it.
    """
    base = Path(_TMP_HOME) / "failed-build"
    package = _make_package(base / "pkg", "live")
    target = base / "versions" / "sha-live"
    extension_store._install_package_artifact(package, target)
    assert (target / "payload.txt").read_text(encoding="utf-8") == "live" * 64

    original = extension_store._build_package_artifact
    extension_store._build_package_artifact = lambda _pkg: (_ for _ in ()).throw(
        extension_store.ExtensionError("build exploded")
    )
    try:
        extension_store._install_package_artifact(package, target)

        fresh = base / "versions" / "sha-fresh"
        raised = False
        try:
            extension_store._install_package_artifact(package, fresh)
        except extension_store.ExtensionError:
            raised = True
        assert raised, "a failed build into a new path must surface as an error"
        assert not fresh.exists(), "a failed build must not leave a partial tree behind"
    finally:
        extension_store._build_package_artifact = original

    assert target.is_dir(), "failed reinstall destroyed the live install directory"
    assert (target / "payload.txt").read_text(encoding="utf-8") == "live" * 64


def test_corrupt_archive_raises_extension_error_and_spares_target() -> None:
    """A truncated archive must surface as ExtensionError, not EOFError.

    Reconcile catches ExtensionError per extension. A raw EOFError escaped the
    whole reconcile pass, so the store was never rewritten and the destroyed
    directory was never repaired.
    """
    base = Path(_TMP_HOME) / "corrupt"
    package = _make_package(base / "pkg", "keep")
    target = base / "versions" / "sha-keep"
    extension_store._install_package_artifact(package, target)

    truncated = extension_store._build_package_artifact(package)[:-40]
    original = extension_store._build_package_artifact
    extension_store._build_package_artifact = lambda _pkg: truncated
    try:
        raised = ""
        try:
            extension_store._install_package_artifact(package, base / "versions" / "sha-new")
        except extension_store.ExtensionError:
            raised = "extension_error"
        except EOFError:
            raised = "eof_error"
        assert raised == "extension_error", f"expected ExtensionError, got {raised or 'no error'}"
    finally:
        extension_store._build_package_artifact = original

    assert target.is_dir(), "corrupt install destroyed the live install directory"
    assert (target / "payload.txt").read_text(encoding="utf-8") == "keep" * 64


def test_concurrent_installs_under_one_parent_do_not_collide() -> None:
    """Concurrent installs of sibling versions must not share scratch state.

    Extraction staged every build under a fixed name derived from the parent
    directory, so two in-flight installs truncated and unlinked each other's
    archive mid-read.
    """
    base = Path(_TMP_HOME) / "concurrent"
    packages = [_make_package(base / f"pkg{i}", f"pkg{i}-") for i in range(2)]
    failures: list[BaseException] = []
    barrier = threading.Barrier(len(packages))

    def install(index: int) -> None:
        try:
            for round_index in range(6):
                target = base / "versions" / f"sha-{index}-{round_index}"
                barrier.wait(timeout=30)
                extension_store._install_package_artifact(packages[index], target)
                assert (target / "payload.txt").is_file()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the assertion below
            failures.append(exc)
            barrier.abort()

    threads = [threading.Thread(target=install, args=(i,)) for i in range(len(packages))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
        assert not thread.is_alive(), "install thread hung"
    assert not failures, f"concurrent installs collided: {failures[0]!r}"


def test_publish_yields_to_an_already_published_target() -> None:
    """A publish that lost the race must leave the winner's tree in place.

    An installer that starts while the tree is damaged finishes after another
    one has published. Because the path is content-addressed the two trees are
    equivalent, so swapping is pure risk: readers stat this path and would see
    it vanish mid-swap.
    """
    base = Path(_TMP_HOME) / "yield"
    package = _make_package(base / "pkg", "winner")
    target = base / "versions" / "sha-contended"
    target.mkdir(parents=True, exist_ok=True)
    (target / "stale.txt").write_text("stale", encoding="utf-8")

    # A loser that already built its tree while target was still damaged.
    staging = base / "versions" / ".staging-loser"
    extension_store._safe_extract_tar_gz(
        extension_store._build_package_artifact(package), staging
    )

    extension_store._install_package_artifact(package, target)
    assert (target / "better-agent-extension.json").is_file()
    before = target.stat().st_ino

    published = extension_store._publish_package_dir(staging, target)

    assert published is False, "a lost race must not report itself as the publisher"
    assert target.stat().st_ino == before, "publish replaced an already-published tree"
    assert (target / "better-agent-extension.json").is_file()


def test_concurrent_install_of_same_target_reports_no_raw_oserror() -> None:
    """Racing installers of one version must not surface raw OS errors.

    A lost rename escaping as FileNotFoundError aborts the caller's whole
    reconcile pass, since reconcile isolates failures per extension by
    catching ExtensionError.
    """
    for attempt in range(8):
        base = Path(_TMP_HOME) / f"same-target-{attempt}"
        package = _make_package(base / "pkg", "shared")
        target = base / "versions" / "sha-shared"
        target.mkdir(parents=True, exist_ok=True)
        (target / "stale.txt").write_text("stale", encoding="utf-8")

        failures: list[BaseException] = []
        barrier = threading.Barrier(4)

        def install() -> None:
            try:
                barrier.wait(timeout=30)
                extension_store._install_package_artifact(package, target)
            except BaseException as exc:  # noqa: BLE001 - asserted below
                failures.append(exc)
                barrier.abort()

        threads = [threading.Thread(target=install) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            assert not thread.is_alive(), "install thread hung"

        for exc in failures:
            assert isinstance(exc, extension_store.ExtensionError), f"unexpected error type: {exc!r}"
        assert (target / "better-agent-extension.json").is_file(), "no installer published the tree"


def test_reinstall_never_exposes_a_missing_target() -> None:
    """Reinstalling an already-published version must never blank the path.

    The version directory is content-addressed, so a published tree already
    holds the exact content a rebuild would produce. Readers stat this path to
    decide runtime-readiness, so it must stay continuously present.
    """
    base = Path(_TMP_HOME) / "reinstall"
    target = base / "versions" / "sha-stable"
    package = _make_package(base / "pkg", "stable")
    extension_store._install_package_artifact(package, target)

    stop = threading.Event()
    missing: list[str] = []

    def watch() -> None:
        while not stop.is_set():
            if not target.is_dir():
                missing.append("target vanished during reinstall")

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        for _ in range(15):
            created = extension_store._install_package_artifact(package, target)
            assert created is False, "a published tree must not be reported as newly created"
    finally:
        stop.set()
        watcher.join(timeout=30)

    assert not missing, missing[0]
    assert (target / "payload.txt").read_text(encoding="utf-8") == "stable" * 64


def test_incomplete_target_is_repaired() -> None:
    """A tree left incomplete by an older failed install must still be repaired."""
    base = Path(_TMP_HOME) / "repair"
    target = base / "versions" / "sha-broken"
    target.mkdir(parents=True, exist_ok=True)
    (target / "stale.txt").write_text("stale", encoding="utf-8")

    package = _make_package(base / "pkg", "fixed")
    extension_store._install_package_artifact(package, target)

    assert (target / "better-agent-extension.json").read_text(encoding="utf-8") == "fixed"
    assert not (target / "stale.txt").exists(), "repair must replace the incomplete tree"


def main() -> None:
    test_failed_build_leaves_live_target_intact()
    test_corrupt_archive_raises_extension_error_and_spares_target()
    test_concurrent_installs_under_one_parent_do_not_collide()
    test_publish_yields_to_an_already_published_target()
    test_concurrent_install_of_same_target_reports_no_raw_oserror()
    test_reinstall_never_exposes_a_missing_target()
    test_incomplete_target_is_repaired()
    print("extension install atomicity tests passed")


if __name__ == "__main__":
    main()
