"""A write against a deleted harness profile must answer 404, not 400/500.

The settings editor keys its "the selection was deleted" recovery on that
status: on 404 it reselects Default instead of reporting a dead profile id as
a failure. Collapsing the missing-profile case into a generic validation error
(or letting the resolver error escape as a 500) breaks that recovery.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="harness-profile-missing-")
paths.engage_test_home(_TMP)

import harness_profile_resolver  # noqa: E402
import harness_profile_store  # noqa: E402


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_store_raises_not_found_subclass() -> None:
    for call in (
        lambda: harness_profile_store.set_profile_meta("ghost-profile", {"default_model": "x"}, None),
        lambda: harness_profile_store.apply_override_patch(
            "ghost-profile",
            [{"op": "clear", "path": ["disabled_builtin_extensions"]}],
            None,
        ),
    ):
        try:
            call()
        except harness_profile_store.HarnessProfileNotFoundError:
            continue
        except harness_profile_store.HarnessProfileError as exc:
            raise AssertionError(
                f"missing profile raised the generic validation error, not the 404 signal: {exc}"
            ) from exc
        raise AssertionError("missing profile did not raise")


def test_resolver_raises_missing_subclass() -> None:
    try:
        harness_profile_resolver.resolve_profile("ghost-profile")
    except harness_profile_resolver.HarnessProfileMissingError:
        return
    except harness_profile_resolver.HarnessProfileResolutionError as exc:
        raise AssertionError(
            f"missing profile raised the generic resolution error, not the 404 signal: {exc}"
        ) from exc
    raise AssertionError("missing profile did not raise")


def test_stale_revision_is_not_a_404() -> None:
    """get_profile answers None for both "gone" and "revision moved". Only the
    first is a 404; a stale revision is a conflict the caller fixes by
    reloading, and collapsing them makes the editor drop a live selection."""
    profile = harness_profile_store.create_profile({"name": "Stale Check"})
    try:
        harness_profile_resolver.resolve_profile(profile["id"], "not-the-current-revision")
    except harness_profile_resolver.HarnessProfileMissingError as exc:
        raise AssertionError(f"stale revision reported as a missing profile: {exc}") from exc
    except harness_profile_resolver.HarnessProfileResolutionError:
        return
    raise AssertionError("stale revision did not raise")


def test_subclasses_stay_catchable_as_their_base() -> None:
    _expect(
        issubclass(
            harness_profile_store.HarnessProfileNotFoundError,
            harness_profile_store.HarnessProfileError,
        ),
        "not-found must stay catchable by existing HarnessProfileError handlers",
    )
    _expect(
        issubclass(
            harness_profile_resolver.HarnessProfileMissingError,
            harness_profile_resolver.HarnessProfileResolutionError,
        ),
        "missing must stay catchable by existing resolution-error handlers",
    )


def main() -> int:
    import shutil

    try:
        test_store_raises_not_found_subclass()
        test_resolver_raises_missing_subclass()
        test_stale_revision_is_not_a_404()
        test_subclasses_stay_catchable_as_their_base()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
