"""Unit tests for ``extension_applied_config`` — the applied-config tag-rule
translator for installed extensions.

This module is the stateless layer that turns each extension manifest's
``entrypoints.applied_config.tag_rules`` (declarations like
``NEEDS_USER_DECISION`` that wrap user-visible prose) into the flat rule
dicts the core renders with. The authoritative enable state lives in
``extension_store`` (``extensions.json``); everything here is a disposable
projection rebuilt on every enable/disable/uninstall and on startup.

Pins:
  1. ``_tag_rules_from_record``: empty when there is no id, when the
     extension is inactive, or when no rules are declared; otherwise a flat
     rule dict per tag with the right optional fields and a stamp of the
     owning extension id, defaulting ``strip_wrapper`` to True and skipping
     non-dict entries.
  2. ``_rebuild_registry``: merges every enabled record's rules into one
     list handed to ``file_ref_resolver.set_tag_rules``.
  3. ``reconcile``: rebuilds, then purges markers only when the record is
     now disabled.
  4. ``reconcile_all``: rebuilds, then purges markers for every inactive id.
  5. ``clear_for_uninstall``: rebuilds, then purges the record's markers.
  6. ``tag_watch_rules``: maps only rules that declare a marker, carrying
     the extension id, marker, and clear_on.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_extension_applied_config_unit.py -v
"""

from __future__ import annotations

import os
import sys

import pytest

import _test_home

_test_home.isolate("bc-test-applied-config-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_applied_config as eac  # noqa: E402
import extension_store  # noqa: E402
import file_ref_resolver  # noqa: E402
import session_manager  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #


def _rule(**overrides) -> dict:
    base = {"tag": "NEEDS_USER_DECISION"}
    base.update(overrides)
    return base


def _record(extension_id: str = "ext-1", tag_rules=None, manifest=None) -> dict:
    if manifest is None:
        manifest = {"id": extension_id, "entrypoints": {"applied_config": {"tag_rules": tag_rules or []}}}
    return {"manifest": manifest, "enabled": True}


@pytest.fixture
def captured_rules(monkeypatch):
    """Capture the list handed to ``file_ref_resolver.set_tag_rules``."""
    seen: list[list[dict]] = []

    def _capture(rules):
        seen.append(list(rules))

    monkeypatch.setattr(file_ref_resolver, "set_tag_rules", _capture)
    return seen


@pytest.fixture
def active_ext(monkeypatch):
    """Mark exactly the given ids as active in ``extension_store``."""
    active = {"ext-1"}

    monkeypatch.setattr(extension_store, "is_extension_active", lambda eid: eid in active)
    return active


# --------------------------------------------------------------------------- #
# _tag_rules_from_record
# --------------------------------------------------------------------------- #


def test_rules_empty_when_manifest_id_missing(monkeypatch, active_ext):
    record = {"manifest": {"entrypoints": {"applied_config": {"tag_rules": [_rule()]}}}, "enabled": True}
    assert eac._tag_rules_from_record(record) == []


def test_rules_empty_when_manifest_id_empty(monkeypatch, active_ext):
    record = {"manifest": {"id": "", "entrypoints": {"applied_config": {"tag_rules": [_rule()]}}}, "enabled": True}
    assert eac._tag_rules_from_record(record) == []


def test_rules_empty_when_extension_inactive(monkeypatch):
    monkeypatch.setattr(extension_store, "is_extension_active", lambda _eid: False)
    record = _record(tag_rules=[_rule()])
    assert eac._tag_rules_from_record(record) == []


def test_rules_empty_when_no_tag_rules_declared(monkeypatch, active_ext):
    record = _record(tag_rules=[])
    assert eac._tag_rules_from_record(record) == []


def test_rules_skip_non_dict_entries(monkeypatch, active_ext):
    record = _record(tag_rules=["not-a-dict", None, _rule(tag="KEEP")])
    rules = eac._tag_rules_from_record(record)
    assert len(rules) == 1
    assert rules[0]["tag"] == "KEEP"


def test_minimal_rule_defaults_strip_wrapper_true(monkeypatch, active_ext):
    record = _record(tag_rules=[_rule()])  # only a tag
    rules = eac._tag_rules_from_record(record)
    assert rules == [{"tag": "NEEDS_USER_DECISION", "strip_wrapper": True, "_extension_id": "ext-1"}]


def test_strip_wrapper_can_be_disabled(monkeypatch, active_ext):
    record = _record(tag_rules=[_rule(strip_wrapper=False)])
    rules = eac._tag_rules_from_record(record)
    assert rules[0]["strip_wrapper"] is False


def test_full_rule_carries_every_optional_field(monkeypatch, active_ext):
    record = _record(
        tag_rules=[
            _rule(
                bold=True,
                font_scale=1.2,
                highlight="#ff0",
                marker="decision",
                clear_on="view",
            )
        ]
    )
    rules = eac._tag_rules_from_record(record)
    assert rules == [
        {
            "tag": "NEEDS_USER_DECISION",
            "strip_wrapper": True,
            "_extension_id": "ext-1",
            "bold": True,
            "font_scale": 1.2,
            "highlight": "#ff0",
            "marker": "decision",
            "clear_on": "view",
        }
    ]


def test_two_active_extensions_stamped_separately(monkeypatch):
    active = {"ext-a", "ext-b"}
    monkeypatch.setattr(extension_store, "is_extension_active", lambda eid: eid in active)
    record_a = _record("ext-a", tag_rules=[_rule(tag="A")])
    record_b = _record("ext-b", tag_rules=[_rule(tag="B")])
    assert eac._tag_rules_from_record(record_a)[0]["_extension_id"] == "ext-a"
    assert eac._tag_rules_from_record(record_b)[0]["_extension_id"] == "ext-b"


# --------------------------------------------------------------------------- #
# _rebuild_registry
# --------------------------------------------------------------------------- #


def test_rebuild_merges_all_enabled_records(monkeypatch, captured_rules, active_ext):
    active_ext.update({"ext-2"})
    monkeypatch.setattr(
        extension_store,
        "_active_records",
        lambda: [_record("ext-1", tag_rules=[_rule(tag="ONE")]), _record("ext-2", tag_rules=[_rule(tag="TWO")])],
    )

    eac._rebuild_registry()

    assert captured_rules == [[
        {"tag": "ONE", "strip_wrapper": True, "_extension_id": "ext-1"},
        {"tag": "TWO", "strip_wrapper": True, "_extension_id": "ext-2"},
    ]]


def test_rebuild_skips_inactive_records(monkeypatch, captured_rules):
    monkeypatch.setattr(extension_store, "is_extension_active", lambda eid: eid == "ext-1")
    monkeypatch.setattr(
        extension_store,
        "_active_records",
        lambda: [_record("ext-1", tag_rules=[_rule(tag="KEEP")]), _record("ext-2", tag_rules=[_rule(tag="DROP")])],
    )

    eac._rebuild_registry()

    assert [r["tag"] for r in captured_rules[0]] == ["KEEP"]


# --------------------------------------------------------------------------- #
# reconcile
# --------------------------------------------------------------------------- #


def test_reconcile_enabled_record_does_not_purge(monkeypatch, captured_rules, active_ext):
    monkeypatch.setattr(extension_store, "_active_records", lambda: [])
    purged: list[str] = []
    monkeypatch.setattr(session_manager.manager, "clear_markers_for_extension", lambda eid: purged.append(eid))

    eac.reconcile(_record("ext-1", tag_rules=[_rule()]))  # enabled record

    assert purged == []


def test_reconcile_disabled_record_purges_markers(monkeypatch, captured_rules, active_ext):
    monkeypatch.setattr(extension_store, "_active_records", lambda: [])
    purged: list[str] = []
    monkeypatch.setattr(session_manager.manager, "clear_markers_for_extension", lambda eid: purged.append(eid))

    eac.reconcile({"manifest": {"id": "ext-1"}, "enabled": False})

    assert purged == ["ext-1"]


def test_reconcile_disabled_record_without_id_does_not_purge(monkeypatch, captured_rules, active_ext):
    monkeypatch.setattr(extension_store, "_active_records", lambda: [])
    purged: list[str] = []
    monkeypatch.setattr(session_manager.manager, "clear_markers_for_extension", lambda eid: purged.append(eid))

    eac.reconcile({"enabled": False})

    assert purged == []


# --------------------------------------------------------------------------- #
# reconcile_all
# --------------------------------------------------------------------------- #


def test_reconcile_all_purges_only_inactive(monkeypatch, captured_rules):
    monkeypatch.setattr(extension_store, "is_extension_active", lambda eid: eid == "ext-active")
    data = {"extensions": {
        "ext-active": _record("ext-active", tag_rules=[_rule()]),
        "ext-dead": _record("ext-dead", tag_rules=[_rule()]),
    }}
    monkeypatch.setattr(extension_store, "_load", lambda: data)
    monkeypatch.setattr(extension_store, "_active_records", lambda: [_record("ext-active", tag_rules=[_rule()])])
    purged: list[str] = []
    monkeypatch.setattr(session_manager.manager, "clear_markers_for_extension", lambda eid: purged.append(eid))

    eac.reconcile_all()

    assert purged == ["ext-dead"]


# --------------------------------------------------------------------------- #
# clear_for_uninstall
# --------------------------------------------------------------------------- #


def test_clear_for_uninstall_purges_record_id(monkeypatch, captured_rules, active_ext):
    monkeypatch.setattr(extension_store, "_active_records", lambda: [])
    purged: list[str] = []
    monkeypatch.setattr(session_manager.manager, "clear_markers_for_extension", lambda eid: purged.append(eid))

    eac.clear_for_uninstall(_record("ext-1"))

    assert purged == ["ext-1"]


def test_clear_for_uninstall_without_id_does_not_purge(monkeypatch, captured_rules, active_ext):
    monkeypatch.setattr(extension_store, "_active_records", lambda: [])
    purged: list[str] = []
    monkeypatch.setattr(session_manager.manager, "clear_markers_for_extension", lambda eid: purged.append(eid))

    eac.clear_for_uninstall({"manifest": {}})

    assert purged == []


# --------------------------------------------------------------------------- #
# tag_watch_rules
# --------------------------------------------------------------------------- #


def test_tag_watch_rules_only_includes_marked(monkeypatch, active_ext):
    monkeypatch.setattr(
        extension_store,
        "_active_records",
        lambda: [
            _record("ext-1", tag_rules=[
                _rule(tag="MARKED", marker="decision", clear_on="view"),
                _rule(tag="BARE"),  # no marker -> excluded
                _rule(tag="ALSO_MARKED", marker="alert"),
            ])
        ],
    )

    out = eac.tag_watch_rules()

    assert set(out.keys()) == {"MARKED", "ALSO_MARKED"}
    assert out["MARKED"] == {"extension_id": "ext-1", "marker": "decision", "clear_on": "view"}
    assert out["ALSO_MARKED"] == {"extension_id": "ext-1", "marker": "alert", "clear_on": None}


if __name__ == "__main__":
    raise SystemExit(os.system(f"{sys.executable} -m pytest {__file__} -v"))
