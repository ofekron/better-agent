"""FuguProvider — Sakana Fugu via the `codex-fugu` launcher.

Fugu (https://sakana.ai/fugu/) is a multi-agent system exposed as an
OpenAI-compatible API that plugs into the Codex CLI. The one-line installer
deploys a `fugu` provider profile into `~/.codex/config.toml` and ships a
`codex-fugu` launcher that runs `codex -p fugu`, forwarding every argument
to codex unchanged. So Fugu is Codex with a different binary and profile —
it reuses `CodexProvider` and `runner_codex` wholesale; only the binary
selection and model catalog differ.

Setup is manual (the installer is a `git clone HEAD | bash` bootstrap that
is not hash-pinnable, so it is intentionally NOT wired into the setup
wizard). Users run the installer themselves, then add a Fugu provider.
"""

from __future__ import annotations

from typing import ClassVar

from provider_codex import CodexProvider


FUGU_MODELS = [
    "fugu",
    "fugu-ultra",
]


def fetch_fugu_models() -> list[str]:
    """Best-effort model list from `codex-fugu debug models`.

    Returns the static `FUGU_MODELS` list on any failure (binary missing,
    non-zero exit, parse error) so the dropdown always has something. Fugu
    exposes exactly two models — Fugu and Fugu Ultra — so the static list
    is authoritative in practice.
    """
    import json as _json
    import subprocess as _sp

    from cli_paths import resolve_cli_binary

    fugu_bin = resolve_cli_binary("codex-fugu")
    if not fugu_bin:
        return list(FUGU_MODELS)

    try:
        proc = _sp.run(
            [fugu_bin, "debug", "models"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, _sp.TimeoutExpired):
        return list(FUGU_MODELS)

    if proc.returncode != 0:
        return list(FUGU_MODELS)

    try:
        data = _json.loads(proc.stdout)
        models = [
            m["slug"]
            for m in data.get("models", [])
            if m.get("visibility") != "hide" and m.get("slug")
        ]
        return models if len(models) >= 1 else list(FUGU_MODELS)
    except (ValueError, KeyError, TypeError):
        return list(FUGU_MODELS)


class FuguProvider(CodexProvider):
    """Sakana Fugu — drives the `codex-fugu` launcher, which is `codex
    -p fugu` under the hood. Inherits all Codex app-server behavior
    (fork, steering, subagents); only the binary and model catalog
    differ."""

    KIND: ClassVar[str] = "fugu"
    RUNNER_KIND: ClassVar[str] = "fugu"
    CODEX_BINARY: ClassVar[str] = "codex-fugu"

    # Sakana's `codex-fugu debug models` catalog advertises exactly two
    # reasoning levels for both Fugu and Fugu Ultra — `high` and `xhigh`.
    # The launcher forwards args to codex unchanged, so codex's
    # `model_reasoning_effort` config reaches the model; expose the dial.
    supports_reasoning_effort: ClassVar[bool] = True
    reasoning_effort_options: ClassVar[tuple[str, ...]] = ("high", "xhigh")
    default_reasoning_effort: ClassVar[str] = "high"
