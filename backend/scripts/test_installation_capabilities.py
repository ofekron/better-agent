#!/usr/bin/env python3
"""100% unit coverage for installation_capabilities.

The module is the user-intent toggle store for the two runtime capabilities
(``mobile``, ``integrations``). Its contract has three load-bearing parts,
each tested here as a real semantic rather than a line touch:

* **Fail-closed validation of untrusted persisted state** — ``_stored`` must
  reject any on-disk shape that is not exactly ``{schema_version, capabilities
  {mobile: bool, integrations: bool}}`` and fall back to the seed. Persisted
  JSON crosses a trust boundary (disk can be hand-edited, partially written,
  or migrated from an older schema), so a malformed file can never widen what
  a capability gate reads.
* **Provisioning truth** — ``provisioned`` reports whether THIS interpreter can
  serve a capability by probing importable modules; ``self_provisionable`` and
  ``in_app_restart_supported`` report whether a mismatch is recoverable.
* **Frozen active snapshot** — ``active``/``snapshot`` capture
  ``enabled AND provisioned`` once so a subsystem never sees a capability flip
  mid-run; ``snapshot`` reports ``restart_required`` only when the wanted state
  genuinely differs from what is active AND the runtime can reach it.
"""

from __future__ import annotations

import atexit
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import _test_home  # noqa: E402

TEST_HOME = _test_home.TestHome.acquire("ba-installation-capabilities-")
atexit.register(TEST_HOME.release)

import installation_capabilities as ic  # noqa: E402
import installation_profile as ip  # noqa: E402

MOBILE = ic.MOBILE
INTEGRATIONS = ic.INTEGRATIONS
DEFAULT = ip.DEFAULT
DESKTOP_UI_ONLY = ip.DESKTOP_UI_ONLY
MOBILE_DESKTOP_UI_ONLY = ip.MOBILE_DESKTOP_UI_ONLY


@pytest.fixture(autouse=True)
def _reset_capability_state():
    """Each test starts with no frozen snapshot and no persisted file."""
    ic.forget_active()
    path = ic._path()
    if path.exists():
        path.unlink()
    yield
    ic.forget_active()
    if path.exists():
        path.unlink()


def _write_raw(payload: object) -> None:
    """Bypass the durable writer to plant an exact (possibly malformed) shape."""
    ic._path().write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------- #
# _seed: install-time defaults per mode
# --------------------------------------------------------------------------- #
def test_seed_derives_defaults_per_install_mode():
    assert ic._seed(DEFAULT) == {MOBILE: True, INTEGRATIONS: True}
    assert ic._seed(DESKTOP_UI_ONLY) == {MOBILE: False, INTEGRATIONS: False}
    assert ic._seed(MOBILE_DESKTOP_UI_ONLY) == {MOBILE: True, INTEGRATIONS: False}
    # No profile chosen yet -> conservative: nothing enabled.
    assert ic._seed(None) == {MOBILE: True, INTEGRATIONS: False}


# --------------------------------------------------------------------------- #
# _stored: fail-closed validation of untrusted persisted state
# --------------------------------------------------------------------------- #
def test_stored_round_trips_a_valid_file_through_set_enabled():
    ic.set_enabled(MOBILE, False, DEFAULT)
    ic.set_enabled(INTEGRATIONS, True, DEFAULT)
    assert ic._stored() == {MOBILE: False, INTEGRATIONS: True}


def test_stored_rejects_wrong_schema_version_and_falls_back_to_seed():
    _write_raw({"schema_version": 999, "capabilities": {MOBILE: True, INTEGRATIONS: True}})
    assert ic._stored() is None
    # settings falls back to the seed derived from the install mode.
    assert ic.settings(DEFAULT) == ic._seed(DEFAULT)


def test_stored_rejects_non_dict_capabilities():
    _write_raw({"schema_version": 1, "capabilities": [MOBILE, INTEGRATIONS]})
    assert ic._stored() is None


def test_stored_rejects_wrong_capability_keys():
    _write_raw({"schema_version": 1, "capabilities": {MOBILE: True}})
    assert ic._stored() is None


def test_stored_rejects_non_bool_values():
    # int 1 is not a bool; the gate may never widen on a coerced truthy value.
    _write_raw({"schema_version": 1, "capabilities": {MOBILE: 1, INTEGRATIONS: True}})
    assert ic._stored() is None


def test_stored_returns_none_for_unparseable_or_missing_file():
    ic._path().write_text("{not json", encoding="utf-8")
    assert ic._stored() is None
    # No file at all -> None, settings seeds.
    ic._path().unlink()
    assert ic._stored() is None
    assert ic.settings(DESKTOP_UI_ONLY) == ic._seed(DESKTOP_UI_ONLY)


# --------------------------------------------------------------------------- #
# enabled / set_enabled
# --------------------------------------------------------------------------- #
def test_enabled_reads_current_intent_and_set_enabled_persists_it():
    assert ic.enabled(MOBILE, DEFAULT) is True
    assert ic.enabled(INTEGRATIONS, DEFAULT) is True
    updated = ic.set_enabled(MOBILE, False, DEFAULT)
    assert updated == {MOBILE: False, INTEGRATIONS: True}
    assert ic.enabled(MOBILE, DEFAULT) is False
    # set_enabled coerces to bool.
    ic.set_enabled(INTEGRATIONS, "no", DEFAULT)
    assert ic.enabled(INTEGRATIONS, DEFAULT) is True


# --------------------------------------------------------------------------- #
# _assert_toggleable: fail-closed on unknown capability
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("call", [
    lambda: ic.enabled("bogus", DEFAULT),
    lambda: ic.set_enabled("bogus", True, DEFAULT),
    lambda: ic.provisioned("bogus"),
    lambda: ic.active("bogus", DEFAULT),
])
def test_non_toggleable_capability_raises(call):
    with pytest.raises(ic.InstallationCapabilityError):
        call()


# --------------------------------------------------------------------------- #
# provisioned: import probes
# --------------------------------------------------------------------------- #
def test_provisioned_integrations_has_no_probe_so_always_true():
    assert ic._PROBES[INTEGRATIONS] == ()
    assert ic.provisioned(INTEGRATIONS) is True


def test_provisioned_true_when_probe_module_resolves(monkeypatch):
    monkeypatch.setattr(ic.importlib.util, "find_spec", lambda module: object())
    assert ic.provisioned(MOBILE) is True


def test_provisioned_false_when_probe_module_missing(monkeypatch):
    monkeypatch.setattr(ic.importlib.util, "find_spec", lambda module: None)
    assert ic.provisioned(MOBILE) is False


@pytest.mark.parametrize("exc", [ImportError("no"), ValueError("bad name")])
def test_provisioned_false_when_probe_raises(monkeypatch, exc):
    def boom(module):
        raise exc

    monkeypatch.setattr(ic.importlib.util, "find_spec", boom)
    assert ic.provisioned(MOBILE) is False


# --------------------------------------------------------------------------- #
# self_provisionable / in_app_restart_supported
# --------------------------------------------------------------------------- #
def test_self_provisionable_false_only_for_frozen_bundle(monkeypatch):
    assert ic.self_provisionable() is True
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert ic.self_provisionable() is False


def test_in_app_restart_supported_requires_run_sh_supervisor(monkeypatch):
    monkeypatch.delenv("BETTER_CLAUDE_RUN_SH_SUPERVISOR", raising=False)
    assert ic.in_app_restart_supported() is False
    monkeypatch.setenv("BETTER_CLAUDE_RUN_SH_SUPERVISOR", "1")
    assert ic.in_app_restart_supported() is True


# --------------------------------------------------------------------------- #
# capture_active / forget_active / active: frozen snapshot
# --------------------------------------------------------------------------- #
def test_capture_active_freezes_intersection_of_enabled_and_provisioned(monkeypatch):
    monkeypatch.setattr(ic.importlib.util, "find_spec", lambda module: None)
    ic.set_enabled(MOBILE, True, DEFAULT)  # wanted, but not provisionable here
    captured = ic.capture_active(DEFAULT)
    assert captured == {MOBILE: False, INTEGRATIONS: True}
    # The snapshot is frozen: dropping it makes the next read re-capture.
    ic.forget_active()
    assert ic._active is None


def test_active_captures_on_first_read_then_is_stable(monkeypatch):
    monkeypatch.setattr(ic.importlib.util, "find_spec", lambda module: None)
    assert ic._active is None
    first = ic.active(MOBILE, DEFAULT)
    assert first is False  # enabled AND provisioned (mobile not provisionable)
    assert ic._active is not None
    # A later intent change does not move the already-frozen active value.
    ic.set_enabled(MOBILE, True, DEFAULT)
    assert ic.active(MOBILE, DEFAULT) is False


# --------------------------------------------------------------------------- #
# snapshot / _capability_state: restart_required pending logic
# --------------------------------------------------------------------------- #
def test_snapshot_no_restart_when_wanted_matches_active(monkeypatch):
    # Both capabilities provisioned -> active == enabled -> nothing pending.
    monkeypatch.setattr(ic.importlib.util, "find_spec", lambda module: object())
    snap = ic.snapshot(DEFAULT)
    for capability in ic.TOGGLEABLE:
        state = snap[capability]
        assert state["enabled"] == state["active"]
        assert state["restart_required"] is False
        assert state["self_provisionable"] is True
        assert state["in_app_restart_supported"] is False


def test_snapshot_reports_restart_when_disabling_a_provisioned_capability(monkeypatch):
    # integrations is provisionable (no probe), so disabling it after capture
    # is a real pending change reachable by a restart (the `not wanted` arm).
    monkeypatch.setattr(ic.importlib.util, "find_spec", lambda module: object())
    ic.capture_active(DEFAULT)  # active integrations = True
    ic.set_enabled(INTEGRATIONS, False, DEFAULT)
    state = ic.snapshot(DEFAULT)[INTEGRATIONS]
    assert state["enabled"] is False
    assert state["active"] is True
    assert state["provisioned"] is True
    assert state["restart_required"] is True


def test_snapshot_reports_restart_when_enabling_unprovisioned_self_provisionable(monkeypatch):
    # mobile wanted but not provisioned; runtime is self-provisionable, so a
    # restart can install what it needs (the `can_provision` arm).
    monkeypatch.setattr(ic.importlib.util, "find_spec", lambda module: None)
    ic.set_enabled(MOBILE, True, DEFAULT)
    ic.capture_active(DEFAULT)  # active mobile = False (not provisioned)
    state = ic.snapshot(DEFAULT)[MOBILE]
    assert state["enabled"] is True
    assert state["active"] is False
    assert state["provisioned"] is False
    assert state["restart_required"] is True


def test_snapshot_no_restart_for_unprovisionable_unreachable_want(monkeypatch):
    # mobile wanted, not provisioned, and a frozen bundle cannot install it:
    # wanting something the bundle was never built with is reported by
    # self_provisionable, not as a pending restart.
    monkeypatch.setattr(ic.importlib.util, "find_spec", lambda module: None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    ic.set_enabled(MOBILE, True, DEFAULT)
    ic.capture_active(DEFAULT)
    state = ic.snapshot(DEFAULT)[MOBILE]
    assert state["enabled"] is True
    assert state["active"] is False
    assert state["self_provisionable"] is False
    assert state["restart_required"] is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
