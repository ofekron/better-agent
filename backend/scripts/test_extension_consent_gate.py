"""FIX2 (Model A, trusted-by-install): a non-builtin extension must not be
enabled until the user has consented to its declared permission set, and must
re-consent when those declared permissions change.
"""
from __future__ import annotations

import os
import sys
import tempfile

import _test_home
_test_home.isolate("ba-consent-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extension_store as es  # noqa: E402


def _rec(perms: dict, *, builtin: bool, consent_fp: str | None = None) -> dict:
    rec = {
        "manifest": {"id": "ext.demo", "permissions": perms},
        "source": {"type": "builtin" if builtin else "repo"},
    }
    if consent_fp is not None:
        rec["consent"] = {"fingerprint": consent_fp, "at": "now"}
    return rec


def main() -> int:
    perms_v1 = {"internal_loopback": True, "network": True, "secrets": "optional"}
    perms_v2 = {"internal_loopback": True, "network": True, "filesystem": True}

    # Builtins never require consent.
    assert es.consent_required(_rec(perms_v1, builtin=True)) is False

    # Non-builtin, no consent recorded → required (fail-closed).
    assert es.consent_required(_rec(perms_v1, builtin=False)) is True

    # Consent recorded for the CURRENT permission set → not required.
    fp1 = es.permission_consent_fingerprint(_rec(perms_v1, builtin=False))
    assert es.consent_required(_rec(perms_v1, builtin=False, consent_fp=fp1)) is False

    # Permission set changes (an update asks for filesystem) → re-consent required
    # even though an old consent exists.
    assert es.consent_required(_rec(perms_v2, builtin=False, consent_fp=fp1)) is True

    # Fingerprint is order-independent and stable.
    a = es.permission_consent_fingerprint(_rec({"a": True, "b": "optional"}, builtin=False))
    b = es.permission_consent_fingerprint(_rec({"b": "optional", "a": True}, builtin=False))
    assert a == b, "fingerprint must be independent of declaration order"
    assert fp1 != es.permission_consent_fingerprint(_rec(perms_v2, builtin=False))

    # --- grandfather migration: legacy enabled non-builtin gets consent stamped ---
    data = es._load()
    data["extensions"]["legacy.ext"] = {
        "manifest": {"id": "legacy.ext", "permissions": {"network": True}},
        "source": {"type": "repo"},
        "enabled": True,  # enabled before the consent feature, no consent record
    }
    data["extensions"]["legacy.builtin"] = {
        "manifest": {"id": "legacy.builtin", "permissions": {"network": True}},
        "source": {"type": "builtin"},
        "enabled": True,
    }
    data["extensions"]["legacy.disabled"] = {
        "manifest": {"id": "legacy.disabled", "permissions": {"network": True}},
        "source": {"type": "repo"},
        "enabled": False,
    }
    es._save(data)

    changed = es.reconcile_extension_consent()
    assert changed >= 1, f"the enabled non-builtin should be grandfathered, got {changed}"
    after = es._load()["extensions"]
    assert not es.consent_required(after["legacy.ext"]), "legacy enabled ext must be grandfathered"
    assert "consent" not in after["legacy.builtin"], "builtins are never stamped"
    assert "consent" not in after["legacy.disabled"], "disabled extensions are not grandfathered"
    # Idempotent: a second pass changes nothing.
    assert es.reconcile_extension_consent() == 0, "grandfather must be idempotent"

    print("OK: non-builtin extensions require permission consent; re-consent on change; grandfather works")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
