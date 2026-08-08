#!/usr/bin/env python3
"""Covers the codex leg of `provider_execution_contract._decode_codex`.

`_decode_codex` is the codec registered for the `"codex"` contract type in
`_CODECS`. Its happy path wraps `CodexExecutionContract.from_dict` and
re-canonicalizes the result; the non-object rejection lives in the family
owner. This file deliberately does NOT engage `_test_home.isolate`, mirroring
`test_codex_execution_contract.py`, because the codex builder captures
launcher/authority file identity (sha256, device, inode, symlink chain) that
must round-trip through `from_dict` — isolating the home breaks that capture.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_execution import build_codex_execution_contract  # noqa: E402
from codex_execution_test_support import provider, write_executable  # noqa: E402
from provider_execution_contract import _decode_codex  # noqa: E402


def test_decode_codex_round_trips_real_contract() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text(
            'model_provider = "openai"\n', encoding="utf-8"
        )
        executable = root / "codex"
        write_executable(executable, b"native")
        encoded = build_codex_execution_contract(
            provider(config_dir),
            launcher_path=str(executable),
            profile="work",
            catalog_args=("-c", 'model_provider="openai"'),
            runtime_args=("-c", "features.shell_snapshot=false"),
        ).to_dict()

    # In production the codec only ever sees JSON-deserialized payloads, so
    # feed it the JSON shape (lists, never tuples) exactly as it arrives.
    frozen = _decode_codex(json.loads(json.dumps(encoded)))
    assert type(frozen.value) is dict
    assert frozen.value["provider_kind"] == "codex"


if __name__ == "__main__":
    test_decode_codex_round_trips_real_contract()
    print("ok codex decode round-trip")
    print("all provider execution contract codex decode tests passed")
