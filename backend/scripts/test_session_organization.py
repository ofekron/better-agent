from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-session-org-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
_SDK = os.path.join(os.path.dirname(_BACKEND), "sdk")
if _SDK not in sys.path:
    sys.path.insert(0, _SDK)

from fastapi.testclient import TestClient  # noqa: E402
from httpx import Response  # noqa: E402

from better_agent_sdk.client import Client  # noqa: E402
from scripts.auth_test_helpers import authenticate_client  # noqa: E402
import extension_token_registry  # noqa: E402
import main  # noqa: E402
import session_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
_AUTO_TAGGING_GRANTS = [
    "auto-tagging.current-task",
    "auto-tagging.snapshot",
    "auto-tagging.select-tags",
    "auto-tagging.ensure-tag",
    "auto-tagging.update-tag",
    "auto-tagging.delete-tag",
    "auto-tagging.sync-session-tags",
    "auto-tagging.tags-sql",
]
_ORIGINAL_GET_EXTENSION = main.extension_store.get_extension
_ORIGINAL_IS_EXTENSION_ACTIVE = main.extension_store.is_extension_active
_ORIGINAL_EXTENSION_ID_FOR_ROLE = main.extension_store.extension_id_for_role


def _session(
    name: str,
    cwd: str = "/tmp/project",
    model: str = "claude-sonnet-4-6",
    provider_id: str | None = None,
    orchestration_mode: str = "native",
) -> str:
    return session_store.create_session(
        name=name,
        model=model,
        cwd=cwd,
        orchestration_mode=orchestration_mode,
        provider_id=provider_id,
    )["id"]


def _auto_tagging_headers() -> dict[str, str]:
    record = {
        "enabled": True,
        "manifest": {
            "id": "ofek-dev.auto-tagging",
            "permissions": {"backend_routes": True, "capabilities": _AUTO_TAGGING_GRANTS},
        },
        "entitlement": {"status": "not_required"},
    }
    main.extension_store.get_extension = lambda extension_id: (
        record if extension_id == "ofek-dev.auto-tagging" else _ORIGINAL_GET_EXTENSION(extension_id)
    )
    main.extension_store.is_extension_active = lambda extension_id: (
        True if extension_id == "ofek-dev.auto-tagging" else _ORIGINAL_IS_EXTENSION_ACTIVE(extension_id)
    )
    main.extension_store.extension_id_for_role = lambda role: (
        "ofek-dev.auto-tagging" if role == "auto-tagging" else _ORIGINAL_EXTENSION_ID_FOR_ROLE(role)
    )
    main.extension_store.runtime_not_ready_message = lambda _extension_id: None
    return {"X-Internal-Token": extension_token_registry.mint("ofek-dev.auto-tagging")}


def _invoke_auto_tagging(client: TestClient, action: str, payload: dict) -> Response:
    return client.post(
        "/api/internal/capabilities/invoke",
        headers=_auto_tagging_headers(),
        json={"capability": "auto-tagging", "action": action, "payload": payload},
    )


def test_folder_tag_assignment_and_query(client: TestClient) -> bool:
    sid_a = _session("api bug")
    sid_b = _session("ui polish")
    folder = client.post(
        "/api/session-folders",
        json={"project_id": "/tmp/project", "name": "Release"},
    ).json()["folder"]
    tag_bug = client.post(
        "/api/session-tags",
        json={"project_id": "/tmp/project", "name": "bug"},
    ).json()["tag"]
    tag_ui = client.post(
        "/api/session-tags",
        json={"project_id": "/tmp/project", "name": "ui"},
    ).json()["tag"]
    r = client.patch(
        f"/api/sessions/{sid_a}/organization",
        json={"folder_id": folder["id"], "tag_ids": [tag_bug["id"], tag_ui["id"]]},
    )
    if r.status_code != 200:
        print(f"  assignment failed: {r.status_code} {r.text}")
        return False
    client.patch(
        f"/api/sessions/{sid_b}/organization",
        json={"tag_ids": [tag_ui["id"]]},
    )
    sessions = client.get("/api/sessions").json()["sessions"]
    rec = next(s for s in sessions if s["id"] == sid_a)
    if rec.get("folder_id") != folder["id"]:
        print(f"  folder missing from summary: {rec}")
        return False
    if {t["id"] for t in rec.get("session_tags") or []} != {tag_bug["id"], tag_ui["id"]}:
        print(f"  tags missing from summary: {rec}")
        return False
    qr = client.post(
        "/api/session-organization/query",
        json={"tag_ids": [tag_bug["id"], tag_ui["id"]], "folder_ids": [folder["id"]]},
    )
    ids = {s["id"] for s in qr.json()["sessions"]}
    if ids != {sid_a}:
        print(f"  query mismatch: {ids}")
        return False
    return True


def test_delete_folder_requires_mode_when_sessions_inside(client: TestClient) -> bool:
    sid = _session("folder delete")
    folder = client.post(
        "/api/session-folders",
        json={"project_id": "/tmp/project", "name": "Temp"},
    ).json()["folder"]
    client.patch(
        f"/api/sessions/{sid}/organization",
        json={"folder_id": folder["id"]},
    )
    r = client.delete(f"/api/session-folders/{folder['id']}")
    if r.status_code != 409:
        print(f"  expected conflict, got: {r.status_code} {r.text}")
        return False
    detail = r.json().get("detail") or {}
    if detail.get("reason") != "folder_contains_sessions" or detail.get("session_ids") != [sid]:
        print(f"  conflict detail mismatch: {detail}")
        return False
    return True


def test_delete_folder_unfiles_sessions(client: TestClient) -> bool:
    sid = _session("folder unassign")
    folder = client.post(
        "/api/session-folders",
        json={"project_id": "/tmp/project", "name": "Temp Unassign"},
    ).json()["folder"]
    client.patch(
        f"/api/sessions/{sid}/organization",
        json={"folder_id": folder["id"]},
    )
    r = client.delete(f"/api/session-folders/{folder['id']}?mode=unassign")
    if r.status_code != 200:
        print(f"  delete failed: {r.status_code} {r.text}")
        return False
    rec = next(s for s in client.get("/api/sessions").json()["sessions"] if s["id"] == sid)
    if rec.get("folder_id") is not None:
        print(f"  folder assignment survived delete: {rec}")
        return False
    return True


def test_delete_folder_can_delete_sessions(client: TestClient) -> bool:
    sid = _session("folder destructive")
    folder = client.post(
        "/api/session-folders",
        json={"project_id": "/tmp/project", "name": "Temp Destructive"},
    ).json()["folder"]
    client.patch(
        f"/api/sessions/{sid}/organization",
        json={"folder_id": folder["id"]},
    )
    r = client.delete(f"/api/session-folders/{folder['id']}?mode=delete_sessions")
    if r.status_code != 200:
        print(f"  destructive delete failed: {r.status_code} {r.text}")
        return False
    ids = {s["id"] for s in client.get("/api/sessions").json()["sessions"]}
    if sid in ids:
        print(f"  session survived destructive folder delete: {ids}")
        return False
    return True


def test_rejects_unknown_query_shape(client: TestClient) -> bool:
    r = client.post("/api/session-organization/query", json={"tag_ids": "bug"})
    if r.status_code != 400:
        print(f"  expected 400 for invalid tag_ids, got {r.status_code}: {r.text}")
        return False
    r = client.post("/api/session-organization/query", json={"models": "opus"})
    if r.status_code != 400:
        print(f"  expected 400 for invalid models, got {r.status_code}: {r.text}")
        return False
    return True


def test_query_filters_provider_model_mode_and_tags(client: TestClient) -> bool:
    sid_a = _session(
        "codex tagged",
        model="gpt-5-codex",
        provider_id="codex",
        orchestration_mode="native",
    )
    sid_b = _session(
        "claude tagged",
        model="claude-sonnet-4-6",
        provider_id="claude",
        orchestration_mode="team",
    )
    sid_c = _session(
        "codex other model",
        model="gpt-5",
        provider_id="codex",
        orchestration_mode="native",
    )
    tag = client.post(
        "/api/session-tags",
        json={"project_id": "/tmp/project", "name": "searchable"},
    ).json()["tag"]
    for sid in (sid_a, sid_b, sid_c):
        client.patch(
            f"/api/sessions/{sid}/organization",
            json={"tag_ids": [tag["id"]]},
        )
    qr = client.post(
        "/api/session-organization/query",
        json={
            "providers": ["codex"],
            "models": ["gpt-5-codex"],
            "modes": ["native"],
            "tag_ids": [tag["id"]],
        },
    )
    ids = [s["id"] for s in qr.json()["sessions"]]
    if ids != [sid_a]:
        print(f"  advanced query mismatch: {ids}")
        return False
    return True


def test_internal_session_organization_routes(client: TestClient) -> bool:
    sid = _session("sdk arranged")
    headers = {"X-Internal-Token": main.coordinator.internal_token}
    folder_resp = client.post(
        "/api/internal/session-organization/create-folder",
        headers=headers,
        json={"project_id": "/tmp/project", "name": "SDK"},
    )
    if folder_resp.status_code != 200:
        print(f"  internal folder create failed: {folder_resp.status_code} {folder_resp.text}")
        return False
    folder = folder_resp.json()["folder"]
    tag_resp = client.post(
        "/api/internal/session-organization/create-tag",
        headers=headers,
        json={"project_id": "/tmp/project", "name": "auto", "color": "blue"},
    )
    if tag_resp.status_code != 200:
        print(f"  internal tag create failed: {tag_resp.status_code} {tag_resp.text}")
        return False
    tag = tag_resp.json()["tag"]
    assign_resp = client.post(
        "/api/internal/session-organization/update-session",
        headers=headers,
        json={"session_id": sid, "folder_id": folder["id"], "tag_ids": [tag["id"]]},
    )
    if assign_resp.status_code != 200:
        print(f"  internal assignment failed: {assign_resp.status_code} {assign_resp.text}")
        return False
    query_resp = client.post(
        "/api/internal/session-organization/query",
        headers=headers,
        json={"folder_ids": [folder["id"]], "tag_ids": [tag["id"]]},
    )
    if query_resp.status_code != 200:
        print(f"  internal query failed: {query_resp.status_code} {query_resp.text}")
        return False
    ids = [s["id"] for s in query_resp.json()["sessions"]]
    if ids != [sid]:
        print(f"  internal query mismatch: {ids}")
        return False
    clear_resp = client.post(
        "/api/internal/session-organization/update-session",
        headers=headers,
        json={"session_id": sid, "folder_id": None, "remove_tag_ids": [tag["id"]]},
    )
    if clear_resp.status_code != 200:
        print(f"  internal clear failed: {clear_resp.status_code} {clear_resp.text}")
        return False
    org = clear_resp.json()["organization"]
    if org.get("folder_id") is not None or org.get("tag_ids") != []:
        print(f"  internal clear mismatch: {org}")
        return False
    return True


def test_sdk_session_organization_methods_build_internal_requests(client: TestClient) -> bool:
    class RecordingClient(Client):
        def __init__(self) -> None:
            super().__init__(backend_url="http://example.invalid", internal_token="token")
            self.calls: list[tuple[str, dict]] = []

        def _post(self, path: str, payload: dict, *, timeout: float = 60.0) -> dict:
            self.calls.append((path, payload))
            return {"ok": True}

    sdk = RecordingClient()
    sdk.get_session_organization("/tmp/project")
    sdk.query_sessions_by_organization({"tag_ids": ["tag"]})
    sdk.create_session_folder("/tmp/project", "Folder", parent_folder_id="parent")
    sdk.update_session_folder("folder", {"name": "Renamed"})
    sdk.delete_session_folder("folder")
    sdk.create_session_tag("Tag", project_id="/tmp/project", color="red")
    sdk.update_session_tag("tag", {"color": "blue"})
    sdk.delete_session_tag("tag")
    sdk.update_session_organization(
        "session",
        folder_id=None,
        tag_ids=[],
        add_tag_ids=["tag"],
        remove_tag_ids=["old"],
        tag_source="auto_tagging",
        sync_tag_source="auto_tagging",
    )
    expected = [
        ("/api/internal/session-organization/snapshot", {"project_id": "/tmp/project"}),
        ("/api/internal/session-organization/query", {"tag_ids": ["tag"]}),
        (
            "/api/internal/session-organization/create-folder",
            {
                "project_id": "/tmp/project",
                "name": "Folder",
                "parent_folder_id": "parent",
            },
        ),
        (
            "/api/internal/session-organization/update-folder",
            {"folder_id": "folder", "patch": {"name": "Renamed"}},
        ),
        ("/api/internal/session-organization/delete-folder", {"folder_id": "folder", "mode": None}),
        (
            "/api/internal/session-organization/create-tag",
            {"name": "Tag", "project_id": "/tmp/project", "color": "red"},
        ),
        (
            "/api/internal/session-organization/update-tag",
            {"tag_id": "tag", "patch": {"color": "blue"}},
        ),
        ("/api/internal/session-organization/delete-tag", {"tag_id": "tag"}),
        (
            "/api/internal/session-organization/update-session",
            {
                "session_id": "session",
                "folder_id": None,
                "tag_ids": [],
                "add_tag_ids": ["tag"],
                "remove_tag_ids": ["old"],
                "tag_source": "auto_tagging",
                "sync_tag_source": "auto_tagging",
            },
        ),
    ]
    if sdk.calls != expected:
        print(f"  sdk calls mismatch: {sdk.calls}")
        return False
    return True


def test_source_sync_preserves_manual_tags(client: TestClient) -> bool:
    sid = _session("source sync")
    manual = client.post(
        "/api/session-tags",
        json={"project_id": "/tmp/project", "name": "manual"},
    ).json()["tag"]
    auto_a = client.post(
        "/api/session-tags",
        json={"project_id": "/tmp/project", "name": "auto a"},
    ).json()["tag"]
    auto_b = client.post(
        "/api/session-tags",
        json={"project_id": "/tmp/project", "name": "auto b"},
    ).json()["tag"]
    first = client.patch(
        f"/api/sessions/{sid}/organization",
        json={"tag_ids": [manual["id"]]},
    )
    if first.status_code != 200:
        print(f"  manual assign failed: {first.status_code} {first.text}")
        return False
    sync = _invoke_auto_tagging(
        client,
        "sync-session-tags",
        {"session_id": sid, "tag_ids": [auto_a["id"]], "source": "auto_tagging"},
    )
    if sync.status_code != 200:
        print(f"  auto sync failed: {sync.status_code} {sync.text}")
        return False
    sync = _invoke_auto_tagging(
        client,
        "sync-session-tags",
        {"session_id": sid, "tag_ids": [auto_b["id"]], "source": "auto_tagging"},
    )
    org = sync.json()["organization"]
    tag_ids = set(org.get("tag_ids") or [])
    sources = org.get("tag_sources") or {}
    if tag_ids != {manual["id"], auto_b["id"]}:
        print(f"  source sync tag mismatch: {org}")
        return False
    if sources.get(manual["id"]) != "manual" or sources.get(auto_b["id"]) != "auto_tagging":
        print(f"  source sync source mismatch: {org}")
        return False
    return True


def test_source_sync_deletes_orphaned_dropped_tags(client: TestClient) -> bool:
    sid = _session("orphan gc")
    other_sid = _session("orphan gc keeper")
    stale = client.post(
        "/api/session-tags",
        json={"project_id": "/tmp/project", "name": "stale auto"},
    ).json()["tag"]
    shared = client.post(
        "/api/session-tags",
        json={"project_id": "/tmp/project", "name": "shared auto"},
    ).json()["tag"]
    fresh = client.post(
        "/api/session-tags",
        json={"project_id": "/tmp/project", "name": "fresh auto"},
    ).json()["tag"]
    kept_manual = client.patch(
        f"/api/sessions/{other_sid}/organization",
        json={"tag_ids": [shared["id"]]},
    )
    if kept_manual.status_code != 200:
        print(f"  keeper assign failed: {kept_manual.status_code} {kept_manual.text}")
        return False
    for tag_ids in ([stale["id"], shared["id"]], [fresh["id"]]):
        sync = _invoke_auto_tagging(
            client,
            "sync-session-tags",
            {"session_id": sid, "tag_ids": tag_ids, "source": "auto_tagging"},
        )
        if sync.status_code != 200:
            print(f"  auto sync failed: {sync.status_code} {sync.text}")
            return False
    names = {t["name"] for t in client.get("/api/session-organization").json()["tags"]}
    if "stale auto" in names:
        print(f"  orphaned dropped tag not deleted: {sorted(names)}")
        return False
    if "shared auto" not in names or "fresh auto" not in names:
        print(f"  referenced tags were deleted: {sorted(names)}")
        return False
    return True


def test_auto_tagging_merge_sync_accumulates_deduped(client: TestClient) -> bool:
    sid = _session("merge accumulate")
    tag_a = client.post(
        "/api/session-tags",
        json={"project_id": "/tmp/project", "name": "merge a"},
    ).json()["tag"]
    tag_b = client.post(
        "/api/session-tags",
        json={"project_id": "/tmp/project", "name": "merge b"},
    ).json()["tag"]
    for tag_ids in ([tag_a["id"]], [tag_b["id"]], [tag_b["id"], tag_a["id"]]):
        sync = _invoke_auto_tagging(
            client,
            "sync-session-tags",
            {
                "session_id": sid,
                "tag_ids": tag_ids,
                "source": "auto_tagging",
                "merge": True,
            },
        )
        if sync.status_code != 200:
            print(f"  merge sync failed: {sync.status_code} {sync.text}")
            return False
    org = sync.json()["organization"]
    if org.get("tag_ids") != [tag_a["id"], tag_b["id"]]:
        print(f"  merge did not accumulate deduped: {org}")
        return False
    names = {t["name"] for t in client.get("/api/session-organization").json()["tags"]}
    if "merge a" not in names or "merge b" not in names:
        print(f"  merged tags missing from vocabulary: {sorted(names)}")
        return False
    return True


def test_tags_get_distinct_palette_colors(client: TestClient) -> bool:
    import session_organization_store

    created = [
        client.post(
            "/api/session-tags",
            json={"project_id": "/tmp/color-proj", "name": f"color tag {index}"},
        ).json()["tag"]
        for index in range(4)
    ]
    colors = [tag["color"] for tag in created]
    if any(color not in session_organization_store.TAG_COLOR_PALETTE for color in colors):
        print(f"  colors not from palette: {colors}")
        return False
    if len(set(colors)) != len(colors):
        print(f"  colors not distinct: {colors}")
        return False
    explicit = client.post(
        "/api/session-tags",
        json={"project_id": "/tmp/color-proj", "name": "explicit color", "color": "#123456"},
    ).json()["tag"]
    if explicit["color"] != "#123456":
        print(f"  explicit color overridden: {explicit}")
        return False
    return True


def test_public_session_organization_rejects_tag_source(client: TestClient) -> bool:
    sid = _session("public source rejected")
    r = client.patch(
        f"/api/sessions/{sid}/organization",
        json={"tag_ids": [], "sync_tag_source": "auto_tagging"},
    )
    if r.status_code != 400:
        print(f"  expected public tag source rejection, got {r.status_code}: {r.text}")
        return False
    return True


def test_internal_session_organization_requires_auth_and_source_owner(client: TestClient) -> bool:
    sid = _session("internal source owner")
    tag = client.post(
        "/api/session-tags",
        json={"project_id": "/tmp/project", "name": "owned-source"},
    ).json()["tag"]
    missing = client.post(
        "/api/internal/session-organization/update-session",
        json={"session_id": sid, "tag_ids": [tag["id"]], "sync_tag_source": "auto_tagging"},
    )
    if missing.status_code != 403:
        print(f"  expected missing token rejection, got {missing.status_code}: {missing.text}")
        return False
    wrong_owner = client.post(
        "/api/internal/session-organization/update-session",
        headers={"X-Internal-Token": extension_token_registry.mint("ofek-dev.ask")},
        json={"session_id": sid, "tag_ids": [tag["id"]], "sync_tag_source": "auto_tagging"},
    )
    if wrong_owner.status_code != 403:
        print(f"  expected wrong source owner rejection, got {wrong_owner.status_code}: {wrong_owner.text}")
        return False
    return True


def test_internal_auto_tagging_tags_sql_is_read_only(client: TestClient) -> bool:
    tag = _invoke_auto_tagging(
        client,
        "ensure-tag",
        {"name": "sql tag", "project_id": "/tmp/project"},
    )
    if tag.status_code != 200:
        print(f"  ensure tag failed: {tag.status_code} {tag.text}")
        return False
    selected = _invoke_auto_tagging(
        client,
        "tags-sql",
        {"sql": "SELECT name FROM tags WHERE name = 'sql tag'"},
    )
    if selected.status_code != 200 or selected.json().get("rows") != [["sql tag"]]:
        print(f"  tags sql select mismatch: {selected.status_code} {selected.text}")
        return False
    denied = _invoke_auto_tagging(
        client,
        "tags-sql",
        {"sql": "DELETE FROM tags"},
    )
    if denied.status_code != 200 or denied.json().get("success") is not False:
        print(f"  tags sql write not denied: {denied.status_code} {denied.text}")
        return False
    sid = _session("auto tagging source pin")
    source_attempt = _invoke_auto_tagging(
        client,
        "sync-session-tags",
        {
            "session_id": sid,
            "tag_ids": [tag.json()["tag"]["id"]],
            "source": "requirement_analysis",
        },
    )
    if source_attempt.status_code != 422:
        print(f"  expected auto-tagging source pin rejection, got {source_attempt.status_code}: {source_attempt.text}")
        return False
    return True


def run_test(name: str, fn, client: TestClient) -> bool:
    try:
        ok = fn(client)
    except Exception as exc:
        print(f"{FAIL} {name}: {exc}")
        return False
    print(f"{PASS if ok else FAIL} {name}")
    return ok


def main_test() -> int:
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    authenticate_client(client)
    tests = [
        ("folder/tag assignment and query", test_folder_tag_assignment_and_query),
        ("query filters provider/model/mode/tags", test_query_filters_provider_model_mode_and_tags),
        ("internal session organization routes", test_internal_session_organization_routes),
        ("source sync preserves manual tags", test_source_sync_preserves_manual_tags),
        ("source sync deletes orphaned dropped tags", test_source_sync_deletes_orphaned_dropped_tags),
        ("tags get distinct palette colors", test_tags_get_distinct_palette_colors),
        ("auto tagging merge sync accumulates deduped", test_auto_tagging_merge_sync_accumulates_deduped),
        ("public session organization rejects tag source", test_public_session_organization_rejects_tag_source),
        ("internal session organization requires auth and source owner", test_internal_session_organization_requires_auth_and_source_owner),
        ("internal auto-tagging tags sql is read only", test_internal_auto_tagging_tags_sql_is_read_only),
        ("sdk session organization request shapes", test_sdk_session_organization_methods_build_internal_requests),
        ("delete folder requires mode when sessions inside", test_delete_folder_requires_mode_when_sessions_inside),
        ("delete folder unfiles sessions", test_delete_folder_unfiles_sessions),
        ("delete folder can delete sessions", test_delete_folder_can_delete_sessions),
        ("rejects unknown query shape", test_rejects_unknown_query_shape),
    ]
    try:
        return 0 if all(run_test(name, fn, client) for name, fn in tests) else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main_test())
