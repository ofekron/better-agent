"""Shared, dependency-light reader for the Claude CLI's subscription OAuth
token.

Extracted from `models.py` so both the FastAPI backend (model-list refresh)
and the in-process Better Agent runner (`runner_better_agent_claude_subscription.py`,
spawned as a bare subprocess) can read the same credential without the
runner importing the much heavier `models.py` module (which pulls in the
full provider/config-store surface — inappropriate for a lean per-turn
subprocess).

NOTE: only reads the single default OS-level Keychain entry. Per-record
`CLAUDE_CONFIG_DIR`-isolated multi-account credential stores are not
supported here (isolated accounts store a plain `.credentials.json` inside
their config dir instead of Keychain — see provider_claude.py for how that
file is captured, but nothing currently reads its contents for the
subscription-runner path). This is a known, accepted limitation.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Optional

logger = logging.getLogger("claude_subscription_credential")


def read_claude_subscription_token() -> Optional[str]:
    """Read the Claude CLI's OAuth access_token from macOS Keychain.

    Claude Code stores subscription creds in a Keychain item named
    `Claude Code-credentials` as JSON:
        {"claudeAiOauth": {"accessToken": "...", ...}}
    Returns the bearer token, or None if missing / malformed / not on
    macOS. The CLI refreshes the token internally; we just read whatever
    it most recently wrote.

    NOTE: macOS prompts (GUI dialog) the first time a non-owner process
    reads the item. uvicorn has no UI — the dialog blocks until the
    user (a) clicks "Always Allow" in the prompt OR (b) the 5s
    subprocess timeout fires (caught below). Workaround for a CI / dev
    box: `security add-generic-password -T <uvicorn_path>` to pre-add
    ACL access, OR open the Claude CLI once and click Allow on the
    first dialog. After that, the read is silent.
    """
    try:
        proc = subprocess.run(
            ["security", "find-generic-password",
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        # Likely a first-run Keychain dialog. Surface it so the user
        # knows why their subscription provider isn't refreshing.
        logger.warning(
            "keychain read for Claude subscription timed out (5s) — "
            "macOS may be showing a permission dialog; click Always "
            "Allow once to make subsequent reads silent",
        )
        return None
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.debug("keychain read unavailable (not macOS?): %s", e)
        return None
    if proc.returncode != 0:
        logger.debug(
            "Claude subscription keychain entry missing "
            "(security exit=%d)", proc.returncode,
        )
        return None
    try:
        data = json.loads(proc.stdout)
        return data["claudeAiOauth"]["accessToken"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("Claude keychain entry shape unexpected: %s", e)
        return None
