from __future__ import annotations

import json
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-harness-fields-api-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import extension_store  # noqa: E402
import harness_fields  # noqa: E402
import harness_profile_resolver  # noqa: E402
import installation_profile  # noqa: E402
import main  # noqa: E402

# A bare test home has no installation.json, so the admission middleware
# would 503 every route and _record_active() would treat every extension as
# inactive. Both gates are about installation setup, not about what this
# test exercises.
installation_profile.integrations_enabled = lambda: True
installation_profile.allows = lambda capability: True

FIXTURE_ID = "fixture.apicheck"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def _install_fixture() -> None:
    package = Path(_TMP_HOME) / "apicheck-extension"
    package.mkdir(parents=True, exist_ok=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": FIXTURE_ID,
        "name": "API Check Fixture",
        "version": "1.0.0",
        "description": "API Check Fixture",
        "surfaces": ["backend_feature"],
        "entrypoints": {
            "backend": "",
            "frontend": "",
            "mcp": [{"name": "api_server", "label": "API Server", "command": "true"}],
            "skills": [{"name": "api-skill", "path": "skills/api-skill", "description": "skill"}],
            "provider_capabilities": [],
            "frontend_modules": [],
            "settings": [
                {"key": "endpoint", "label": "Endpoint", "type": "string", "default": "https://a.test"},
            ],
        },
        "permissions": {},
        "marketplace": {},
    }
    manifest.setdefault("protocol", {
        "version": 1,
        "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
    })
    validated = extension_store.validate_manifest(manifest)
    (package / "better-agent-extension.json").write_text(json.dumps(validated), encoding="utf-8")
    data = extension_store._load()  # type: ignore[attr-defined]
    data["extensions"][FIXTURE_ID] = {
        "manifest": validated,
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {
            "type": "git",
            "repo_url": "https://example.test/private.git",
            "extension_path": "extensions/apicheck",
            "ref": "",
            "commit_sha": "apicheck-fixture",
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


def main_test() -> None:
    _install_fixture()
    client = TestClient(main.app)
    client.headers.update({"Authorization": f"Bearer {auth.create_token('test')}"})

    # The descriptor route must not be swallowed by /{profile_id}.
    descriptor = client.get("/api/harness-profiles/descriptor")
    check(descriptor.status_code == 200, "descriptor route resolves ahead of the profile-id route")
    body = descriptor.json()
    entry = next((e for e in body["extensions"] if e["id"] == FIXTURE_ID), None)
    check(entry is not None, "descriptor lists the installed extension")
    manifest_path = str((Path(_TMP_HOME) / "apicheck-extension" / "better-agent-extension.json").resolve())
    check(entry["description_path"] == manifest_path, "extension description links to its manifest")
    group_ids = {g["id"] for g in entry["groups"]}
    check(harness_fields.GROUP_MCP in group_ids, "descriptor exposes the MCP group")
    check(harness_fields.GROUP_SETTINGS in group_ids, "descriptor exposes the settings group")
    skills = next(group for group in entry["groups"] if group["id"] == harness_fields.GROUP_SKILLS)
    check(skills["items"][0]["description_path"] == manifest_path, "declared skill description links to its manifest")

    original_resolve_profile = harness_profile_resolver.resolve_profile
    resolve_calls = 0

    def counted_resolve_profile(*args, **kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        return original_resolve_profile(*args, **kwargs)

    harness_profile_resolver.resolve_profile = counted_resolve_profile
    try:
        profiles = client.get("/api/harness-profiles")
        check(profiles.status_code == 200, "profile summary list succeeds")
        listed = profiles.json()["profiles"]
        check(listed[0]["id"] == "default", "profile summary list includes Default")
        check("fields" not in listed[0], "profile summary list omits resolved fields")
        check(resolve_calls == 0, "profile summary list does not resolve every profile")
    finally:
        harness_profile_resolver.resolve_profile = original_resolve_profile

    # Default writes pass through to the owning store.
    default_write = client.patch(
        "/api/harness-profiles/default/fields",
        json={"revision": "", "writes": [
            {"path": ["extension_instances", FIXTURE_ID, harness_fields.GROUP_SETTINGS, "endpoint"],
             "value": "https://api-written.test"},
        ]},
    )
    check(default_write.status_code == 200, "Default field write succeeds")
    check(
        extension_store.get_extension_settings(FIXTURE_ID)["values"]["endpoint"] == "https://api-written.test",
        "Default write reaches the owning extension store",
    )
    check(default_write.json()["id"] == "default", "Default write returns the re-read Default profile")

    created = client.post("/api/harness-profiles", json={"name": "Api Profile"})
    check(created.status_code == 200, "profile creation succeeds")
    profile = created.json()
    resolve_calls = 0
    harness_profile_resolver.resolve_profile = counted_resolve_profile
    try:
        prompt_write = client.patch(
            f"/api/harness-profiles/{profile['id']}/fields",
            json={"revision": profile["revision"], "writes": [
                {"path": ["profile_meta", "provisioning_prompt"],
                 "value": "Provision this profile's workers carefully."},
            ]},
        )
    finally:
        harness_profile_resolver.resolve_profile = original_resolve_profile
    check(prompt_write.status_code == 200, "profile provisioning prompt write succeeds")
    check(resolve_calls <= 2, "profile field write resolves only for write validation and response")
    check(
        prompt_write.json()["provisioning_prompt"] == "Provision this profile's workers carefully.",
        "profile response includes own provisioning prompt",
    )
    profile = prompt_write.json()

    # A named profile narrows without touching global state.
    toggle = client.patch(
        f"/api/harness-profiles/{profile['id']}/fields",
        json={"revision": profile["revision"], "writes": [
            {"path": ["extension_instances", FIXTURE_ID, harness_fields.GROUP_SKILLS, "api-skill"],
             "value": False},
        ]},
    )
    check(toggle.status_code == 200, "named-profile item toggle succeeds")
    fields = toggle.json()["fields"]["extension_instances"][FIXTURE_ID]
    check(fields["skills"]["resolved"] == [], "named profile resolves the skill off")
    check(fields["skills"]["override"] == {"add": [], "remove": ["api-skill"]}, "stored override is a minimal delta")
    check(
        extension_store.extension_runtime_skills(FIXTURE_ID)[0]["enabled"] is True,
        "named-profile narrowing leaves the global store untouched",
    )

    # A stale revision must not silently overwrite a concurrent writer.
    # Revisions are content-addressed, so this has to be checked while the
    # profile genuinely differs from the revision being replayed — once the
    # override is cleared the profile hashes back to its created revision.
    stale = client.patch(
        f"/api/harness-profiles/{profile['id']}/fields",
        json={"revision": profile["revision"], "writes": [
            {"path": ["extension_instances", FIXTURE_ID, harness_fields.GROUP_MCP, "api_server"],
             "value": False},
        ]},
    )
    check(stale.status_code in (400, 409), "stale revision is rejected")

    # Clearing reverts to inheriting Default.
    latest = toggle.json()
    cleared = client.patch(
        f"/api/harness-profiles/{profile['id']}/fields",
        json={"revision": latest["revision"], "writes": [
            {"path": ["extension_instances", FIXTURE_ID, harness_fields.GROUP_SKILLS, "api-skill"],
             "clear": True},
        ]},
    )
    check(cleared.status_code == 200, "clearing an override succeeds")
    cleared_fields = cleared.json()["fields"]["extension_instances"][FIXTURE_ID]
    check(cleared_fields["skills"]["override"] is None, "cleared field inherits Default again")
    check(cleared_fields["skills"]["resolved"] == ["api-skill"], "cleared field resolves to Default's value")

    # Clearing is meaningless on Default and must be refused rather than silently ignored.
    bad_clear = client.patch(
        "/api/harness-profiles/default/fields",
        json={"revision": "", "writes": [
            {"path": ["extension_instances", FIXTURE_ID, harness_fields.GROUP_SKILLS, "api-skill"],
             "clear": True},
        ]},
    )
    check(bad_clear.status_code == 400, "clearing on Default is rejected")

    empty = client.patch(
        f"/api/harness-profiles/{profile['id']}/fields", json={"revision": "", "writes": []},
    )
    check(empty.status_code == 400, "empty write list is rejected")


if __name__ == "__main__":
    main_test()
    print("PASS harness profile fields API")
