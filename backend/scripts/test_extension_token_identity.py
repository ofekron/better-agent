"""FIX1 regression: extension identity on the /api/internal/* boundary is
derived from a per-extension token, NOT a self-asserted X-Extension-Id header.

Before this fix, one global internal token authenticated every extension and
the caller asserted its own id via header — so any internal_loopback extension
could impersonate any other extension (or a builtin). These assertions prove:
  * each extension gets its own distinct token,
  * a token reverse-maps only to its owner,
  * the core token is NOT an extension principal,
  * resolve_principal/principal_extension_id classify purely by token,
  * a forged/unknown token authenticates as nobody.
"""
from __future__ import annotations

import os
import sys
import tempfile
from types import SimpleNamespace

import _test_home
_test_home.isolate("ba-tok-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extension_token_registry as reg  # noqa: E402
from orchestrator import Coordinator  # noqa: E402


def main() -> int:
    CORE = "core-global-token-xyz"

    # --- registry: distinct, stable, owner-scoped ---
    tok_a = reg.mint("ext.alpha")
    tok_b = reg.mint("ext.beta")
    assert tok_a and tok_b and tok_a != tok_b, "extensions must get distinct tokens"
    assert reg.mint("ext.alpha") == tok_a, "mint must be idempotent (stable token)"
    assert reg.resolve(tok_a) == "ext.alpha"
    assert reg.resolve(tok_b) == "ext.beta"
    assert reg.resolve(CORE) is None, "core token is not in the extension registry"
    assert reg.resolve("forged") is None and reg.resolve(None) is None

    # --- resolve_principal / principal_extension_id classify by TOKEN ONLY ---
    # Build a minimal stand-in whose verify_internal_token recognizes only the
    # core token, then exercise the real Coordinator methods unbound.
    fake = SimpleNamespace(
        verify_internal_token=lambda tok: tok == CORE,
    )
    rp = Coordinator.resolve_principal.__get__(fake)
    fake.resolve_principal = rp  # principal_extension_id delegates to it
    pid = Coordinator.principal_extension_id.__get__(fake)

    assert rp(CORE) == ("core", None), "core token classifies as core"
    assert rp(tok_a) == ("extension", "ext.alpha")
    assert rp(tok_b) == ("extension", "ext.beta")
    assert rp("forged") is None, "unknown token authenticates as nobody"
    assert rp(None) is None

    # The methods take NO extension-id argument — identity cannot be asserted by
    # the caller. A 'beta' caller holding alpha's token is alpha, full stop.
    assert pid(tok_a) == "ext.alpha"
    assert pid(tok_b) == "ext.beta"
    assert pid(CORE) is None, "core token has no extension identity"

    # --- revoke cuts off authentication ---
    reg.revoke("ext.alpha")
    assert reg.resolve(tok_a) is None, "revoked token must stop authenticating"
    assert reg.resolve(tok_b) == "ext.beta", "revoke is scoped to one extension"

    print("OK: extension identity is token-derived; X-Extension-Id spoofing is impossible")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
