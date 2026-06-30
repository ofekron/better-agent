from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import provider_codex  # noqa: E402


def test_codex_cold_start_models_include_current_cli_visible_models():
    assert provider_codex.CODEX_MODELS == [
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
    ]


def test_fetch_codex_models_parses_visible_cli_catalog():
    payload = {
        "models": [
            {"slug": "gpt-5.5", "visibility": "list"},
            {"slug": "gpt-5.4", "visibility": "show"},
            {"slug": "hidden", "visibility": "hide"},
            {"slug": "gpt-5.3-codex-spark", "visibility": "list"},
            {"visibility": "list"},
        ],
    }
    proc = mock.Mock(returncode=0, stdout=json.dumps(payload))

    with (
        mock.patch("cli_paths.resolve_cli_binary", return_value="/bin/codex"),
        mock.patch("subprocess.run", return_value=proc) as run,
    ):
        models = provider_codex.fetch_codex_models()

    run.assert_called_once_with(
        ["/bin/codex", "debug", "models"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert models == ["gpt-5.5", "gpt-5.4", "gpt-5.3-codex-spark"]


if __name__ == "__main__":
    test_codex_cold_start_models_include_current_cli_visible_models()
    test_fetch_codex_models_parses_visible_cli_catalog()
    print("ok")
