#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import installation_profile
import dependency_plan
import provider_setup


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Configure Better Agent installation")
    parser.add_argument("--mode", choices=sorted(installation_profile.MODES))
    parser.add_argument("--provider", choices=provider_setup.supported_provider_kinds())
    parser.add_argument("--yes", action="store_true")
    return parser


def _choose(title: str, options: list[tuple[str, str]]) -> str:
    print(title)
    for index, (_, label) in enumerate(options, start=1):
        print(f"  {index}. {label}")
    while True:
        answer = input("Choose: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        print(f"Enter a number from 1 to {len(options)}.")


def _resolve_args(args: argparse.Namespace) -> tuple[str, str]:
    if args.yes and (not args.mode or not args.provider):
        raise SystemExit("--yes requires both --mode and --provider")
    mode = args.mode or _choose(
        "How deeply should Better Agent integrate?",
        [
            (
                installation_profile.DESKTOP_UI_ONLY,
                "Desktop UI only — no mobile app or Better Agent integrations",
            ),
            (
                installation_profile.MOBILE_DESKTOP_UI_ONLY,
                "Mobile + Desktop UI only — no Better Agent extensions, skills, MCPs, or agent additions",
            ),
            (
                installation_profile.DEFAULT,
                "Default — Better Agent's standard integrations plus mobile and desktop UI",
            ),
        ],
    )
    providers = [
        (kind, provider_setup.installer_for(kind).label)
        for kind in provider_setup.supported_provider_kinds()
    ]
    provider = args.provider or _choose("Which provider do you want to use?", providers)
    return mode, provider


async def _configure(mode: str, provider: str) -> None:
    async def report(event: str, payload: dict) -> None:
        if event == "provider_install_progress" and payload.get("text"):
            print(payload["text"])

    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required before Better Agent setup can activate")

    installer = provider_setup.installer_for(provider)
    with dependency_plan.activation_lock():
        print(f"Checking {installer.label}...")
        initial_identity = await provider_setup.verified_provider_identity(provider)
        if initial_identity is None:
            result = await provider_setup.install_if_missing(provider, report)
            if result["state"] != "succeeded":
                raise RuntimeError(
                    result.get("message") or f"Failed to install {installer.label}"
                )
            print(f"Installed {installer.label}.")
        else:
            print(f"{installer.label} is already installed.")

        provider_setup.clear_status_cache(provider)
        verified_identity = await provider_setup.verified_provider_identity(provider)
        if verified_identity is None:
            raise RuntimeError(f"{installer.label} failed final verification")
        if initial_identity is not None and initial_identity != verified_identity:
            raise RuntimeError(
                f"{installer.label} executable changed between verification stages"
            )

        profile = installation_profile.new_active_profile(
            mode=mode,
            provider=provider,
            provider_identity=verified_identity,
        )
        environment = dependency_plan.prepare_installation(uv, profile)
        dependency_plan.activate_prepared_installation(environment, profile)

    print(f"Better Agent installation mode: {mode}")
    print("Restart Better Agent if it is currently running so all integration projections reconcile.")


def main() -> int:
    mode, provider = _resolve_args(_parser().parse_args())
    try:
        asyncio.run(_configure(mode, provider))
    except (installation_profile.InstallationProfileError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
