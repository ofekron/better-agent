"""Regression: the extension projection must stay robust to a single bad record.

A record carrying a null manifest id used to crash `list_extensions` (it sorted
by `manifest["id"]`), which bricked every consumer of the projection — most
notably `config_store.default_session_model`, i.e. all session creation. The
projection now sorts by the canonical dict key, so one corrupt record cannot
take down the global extension read model.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import _test_home

_test_home.isolate("bc-test-extension-list-projection-")
os.environ["BETTER_AGENT_API_ONLY"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extension_store  # noqa: E402


def test_list_extensions_survives_null_manifest_id_record() -> None:
    data = {
        "extensions": {
            "ofek-dev.alpha": {
                "manifest": {"id": None, "core_roles": []},
                "enabled": True,
                "source": {"type": "test"},
                "entitlement": {"status": "not_required"},
            },
            "ofek-dev.beta": {
                "manifest": {"id": "ofek-dev.beta", "core_roles": []},
                "enabled": True,
                "source": {"type": "test"},
                "entitlement": {"status": "not_required"},
            },
        },
    }
    # Must not raise TypeError comparing None to str. Ordering follows the
    # canonical dict key, so alpha precedes beta regardless of manifest ids.
    records = extension_store._list_extensions_from_data(data, include_hidden=True)  # type: ignore[attr-defined]
    assert [record["manifest"]["id"] for record in records] == [None, "ofek-dev.beta"]


def test_list_extensions_orders_by_canonical_key_not_manifest_id() -> None:
    # A record whose manifest id differs from its dict key still sorts by the
    # authoritative dict key, never crashing and never depending on manifest id.
    data = {
        "extensions": {
            "zzz-key": {
                "manifest": {"id": "aaa-manifest"},
                "enabled": True,
                "source": {"type": "test"},
                "entitlement": {"status": "not_required"},
            },
            "aaa-key": {
                "manifest": {"id": "zzz-manifest"},
                "enabled": True,
                "source": {"type": "test"},
                "entitlement": {"status": "not_required"},
            },
        },
    }
    records = extension_store._list_extensions_from_data(data, include_hidden=True)  # type: ignore[attr-defined]
    assert [record["manifest"]["id"] for record in records] == ["zzz-manifest", "aaa-manifest"]
