"""openai-kind providers (sakana, z.ai, custom) must list models via the
OpenAI-compatible GET {base_url}/models endpoint.

Regression: before this fix _resolve_refresh_fetch returned None for openai,
so the catalog only ever held the single configured default_model (e.g.
"fugu" with no "fugu-ultra"). Locks (a) the wiring (openai now yields a
fetcher) and (b) the parser against the OpenAI /models response shape.

Uses a temp BETTER_AGENT_HOME so no real session state is touched.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="openai_models_test_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import models  # noqa: E402


def test_openai_yields_a_fetcher():
    rec = {"kind": "openai", "base_url": "https://api.sakana.ai/v1", "api_key": "sk-x"}
    assert models._resolve_refresh_fetch(rec) is not None


def test_openai_without_key_or_url_is_not_refreshable():
    assert models._resolve_refresh_fetch({"kind": "openai", "base_url": "x"}) is None
    assert models._resolve_refresh_fetch({"kind": "openai", "api_key": "y"}) is None


def test_fetch_parses_models_endpoint(monkeypatch=None):
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"object": "list", "data": [
                {"id": "fugu"}, {"id": "fugu-ultra"}, {"id": "fugu-ultra-20260615"},
            ]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            captured["url"] = url
            captured["auth"] = (headers or {}).get("Authorization")
            return _Resp()

    orig = models.httpx.Client
    models.httpx.Client = _Client
    try:
        out = models.fetch_openai_models("https://api.sakana.ai/v1", "sk-key")
    finally:
        models.httpx.Client = orig

    # base_url is used verbatim — endpoint is {base_url}/models, not /v1/v1
    assert captured["url"] == "https://api.sakana.ai/v1/models", captured["url"]
    assert captured["auth"] == "Bearer sk-key"
    assert out == ["fugu", "fugu-ultra", "fugu-ultra-20260615"], out


def test_fetch_returns_empty_on_failure():
    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            raise OSError("network down")

    orig = models.httpx.Client
    models.httpx.Client = _BoomClient
    try:
        assert models.fetch_openai_models("https://api.sakana.ai/v1", "k") == []
    finally:
        models.httpx.Client = orig


def test_cold_start_catalog_includes_default_model():
    # Fresh openai provider, no cache, no custom_models, first /models fetch
    # not yet run: the selector must still offer the configured default rather
    # than being empty.
    prov = {
        "id": "no-such-cache-provider-xyz", "kind": "openai", "mode": "api_key",
        "base_url": "https://api.sakana.ai/v1", "default_model": "fugu",
    }
    out = models._models_for(prov)
    assert out == ["fugu"], out


if __name__ == "__main__":
    test_openai_yields_a_fetcher()
    test_openai_without_key_or_url_is_not_refreshable()
    test_fetch_parses_models_endpoint()
    test_fetch_returns_empty_on_failure()
    test_cold_start_catalog_includes_default_model()
    print("ok")
