from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home
_TMP_HOME = _test_home.isolate("bc-harness-instruction-selection-")

import extension_store
import harness_profile_resolver
import harness_profile_store
import installation_profile

installation_profile.integrations_enabled = lambda: True

_ON_ID = "fixture.instructions-on"
_OFF_ID = "fixture.instructions-off"


def _install(extension_id: str, sections: list[str], *, instructions_enabled: bool) -> None:
    package = Path(_TMP_HOME) / extension_id
    package.mkdir(parents=True, exist_ok=True)
    for name in sections:
        (package / f"{name}.md").write_text(f"CONTENT OF {name}", encoding="utf-8")
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": extension_id,
        "surfaces": ["instructions"],
        "entrypoints": {
            "backend": "",
            "frontend": "",
            "mcp": [],
            "frontend_modules": [],
            "instructions": [
                {"name": name, "path": f"{name}.md", "level": "global"} for name in sections
            ],
        },
        "permissions": {},
        "marketplace": {},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
        },
    }
    validated = extension_store.validate_manifest(manifest)
    (package / "better-agent-extension.json").write_text(json.dumps(validated), encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][extension_id] = {
        "manifest": validated,
        "enabled": True,
        "instructions_enabled": {"global": instructions_enabled, "projects": {}},
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": f"extensions/{extension_id}",
            "ref": "",
            "commit_sha": f"{extension_id}-fixture",
            "install_path": str(package),
        },
        "entitlement": {
            "status": "not_required",
            "product_id": "",
            "token_present": False,
            "last_checked_at": "",
            "expires_at": "",
        },
    }
    extension_store._save(data)  # type: ignore[attr-defined]


def _block_names(profile_id: str) -> list[str]:
    harness_profile_resolver.invalidate_cache()
    snapshot = harness_profile_resolver.resolve_for_session({}, profile_id=profile_id)
    return [block["name"] for block in snapshot["instruction_blocks"]]


def _select(profile_id: str, extension_id: str, *, add: list[str], remove: list[str]) -> None:
    harness_profile_store.apply_override_patch(
        profile_id,
        [{
            "path": ["extension_instances", extension_id, "instruction_names"],
            "op": "set",
            "value": {"add": add, "remove": remove},
        }],
    )


def test_default_injects_every_globally_enabled_section() -> None:
    names = _block_names(harness_profile_store.DEFAULT_PROFILE_ID)
    assert names == ["alpha", "beta"], names


def test_deselected_section_is_not_injected() -> None:
    harness_profile_store.create_profile({"id": "drop.one", "name": "Drop One"})
    _select("drop.one", _ON_ID, add=[], remove=["beta"])
    names = _block_names("drop.one")
    # Before the fix the inherited instruction_sources entry kept injecting
    # "beta" even though the profile deselected it.
    assert names == ["alpha"], names


def test_deselecting_every_section_does_not_fail_the_run() -> None:
    harness_profile_store.create_profile({"id": "drop.all", "name": "Drop All"})
    _select("drop.all", _ON_ID, add=[], remove=["alpha", "beta"])
    # The extension leaves the run entirely; its orphaned instruction sources
    # must be dropped rather than raising "not selected".
    assert _block_names("drop.all") == []


def test_selected_section_of_globally_disabled_extension_is_injected() -> None:
    harness_profile_store.create_profile({"id": "add.off", "name": "Add Off"})
    _select("add.off", _OFF_ID, add=["gamma"], remove=[])
    names = _block_names("add.off")
    assert names == ["alpha", "beta", "gamma"], names


def main() -> int:
    _install(_ON_ID, ["alpha", "beta"], instructions_enabled=True)
    _install(_OFF_ID, ["gamma"], instructions_enabled=False)
    test_default_injects_every_globally_enabled_section()
    test_deselected_section_is_not_injected()
    test_deselecting_every_section_does_not_fail_the_run()
    test_selected_section_of_globally_disabled_extension_is_injected()
    print("PASS harness profile instruction selection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
