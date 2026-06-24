#!/usr/bin/env python3
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
tmp_home = _test_home.isolate("bc-gemini-subscription-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config_store  # noqa: E402
from provider_gemini import GeminiProvider  # noqa: E402


def check(label: str, condition: bool) -> bool:
    print(("PASS " if condition else "FAIL ") + label)
    return condition


def raises_unsupported(fn) -> bool:
    try:
        fn()
    except ValueError as exc:
        return "Gemini CLI subscription auth is no longer supported" in str(exc)
    return False


def main() -> int:
    failures: list[str] = []

    state = config_store.list_providers()
    kinds = [p["kind"] for p in state["providers"]]
    if not check("fresh defaults omit Gemini subscription provider", "gemini" not in kinds):
        failures.append("fresh defaults")

    if not check(
        "add_provider rejects Gemini subscription",
        raises_unsupported(lambda: config_store.add_provider({
            "name": "Gemini",
            "kind": "gemini",
            "mode": "subscription",
            "default_model": "gemini-2.5-pro",
        })),
    ):
        failures.append("add reject")

    claude = config_store.add_provider({
        "name": "Temp",
        "kind": "claude",
        "mode": "subscription",
        "default_model": "claude-opus-4-7[1m]",
    })
    if not check(
        "update_provider rejects conversion to Gemini subscription",
        raises_unsupported(lambda: config_store.update_provider(claude["id"], {"kind": "gemini"})),
    ):
        failures.append("update reject")

    provider = GeminiProvider({
        "id": "legacy-gemini",
        "kind": "gemini",
        "mode": "subscription",
        "default_model": "gemini-2.5-pro",
    })
    loop = asyncio.new_event_loop()
    try:
        provider.start_run(
            run_id="r1",
            prompt="hello",
            cwd=tmp_home,
            loop=loop,
            queue=asyncio.Queue(),
            model="gemini-2.5-pro",
            reasoning_effort=None,
            session_id=None,
            mode="native",
            app_session_id="s1",
        )
        blocked = False
    except RuntimeError as exc:
        blocked = "Gemini CLI subscription auth is no longer supported" in str(exc)
    finally:
        loop.close()
    if not check("legacy Gemini subscription provider fails before spawn", blocked):
        failures.append("start_run reject")

    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)
