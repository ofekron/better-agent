from __future__ import annotations

import os
import sys

import pytest

import _test_home

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_test_home.isolate("bc-test-harness-field-writer-unit-")

import extension_store  # noqa: E402
import harness_field_writer as mod  # noqa: E402
import harness_fields  # noqa: E402
from harness_fields import HarnessFieldError  # noqa: E402

DISABLED_GROUPS = (
    harness_fields.GROUP_DISABLED_BUILTIN_TOOLS,
    harness_fields.GROUP_DISABLED_BUILTIN_EXTENSIONS,
    harness_fields.GROUP_DISABLED_RUNTIME_SKILLS,
)
META = harness_fields.GROUP_PROFILE_META
SETTINGS = harness_fields.GROUP_SETTINGS
USER_INSTR = harness_fields.GROUP_USER_INSTRUCTIONS
SKILLS = harness_fields.GROUP_SKILLS
NATIVE = harness_fields.GROUP_NATIVE_EXPOSURE
DEFAULT_PID = "default"
NAMED_PID = "work"


def _disabled_resolved(head: str, items: list[str]) -> dict:
    return {head: {"resolved": list(items)}}


def _ext_resolved(ext: str, group: str, items: list[str]) -> dict:
    return {"extension_instances": {ext: {group: {"resolved": list(items)}}}}


def _settings_record(key: str) -> dict:
    return {"manifest": {"id": "ext", "entrypoints": {"settings": [{"key": key, "type": "string"}]}}}


@pytest.fixture
def install(monkeypatch):
    """Patch the four I/O collaborators; keep harness_fields pure helpers real."""
    state: dict = {
        "default": {},
        "resolved": {},
        "extensions": {},
        "applied": [],
        "meta": [],
        "gets": [],
        "written": [],
        "apply_return": {"revision": "r2", "id": NAMED_PID},
        "meta_return": {"revision": "r3", "id": NAMED_PID},
        "get_return": {"revision": "r0", "id": NAMED_PID},
    }

    monkeypatch.setattr(
        mod.harness_profile_resolver, "compute_default_profile", lambda: state["default"]
    )
    monkeypatch.setattr(
        mod.harness_profile_resolver,
        "resolve_profile",
        lambda profile_id, revision, default=None: state["resolved"],
    )
    monkeypatch.setattr(
        mod.harness_profile_store,
        "apply_override_patch",
        lambda profile_id, ops, revision=None: state["applied"].append((profile_id, ops, revision))
        or state["apply_return"],
    )
    monkeypatch.setattr(
        mod.harness_profile_store,
        "set_profile_meta",
        lambda profile_id, patch, revision=None: state["meta"].append((profile_id, patch, revision))
        or state["meta_return"],
    )
    monkeypatch.setattr(
        mod.harness_profile_store,
        "get_profile",
        lambda profile_id, revision=None: state["gets"].append((profile_id, revision))
        or state["get_return"],
    )
    monkeypatch.setattr(
        harness_fields, "write_default", lambda path, value: state["written"].append((path, value))
    )
    monkeypatch.setattr(
        extension_store, "get_extension", lambda ext_id: state["extensions"].get(ext_id)
    )
    return state


def apply(profile_id, writes, revision="r1"):
    return mod.apply_field_writes(profile_id, revision, list(writes))


# --- pure helpers -----------------------------------------------------------


def test_toggled_adds_when_present():
    assert mod._toggled(["a", "b"], "c", True) == ["a", "b", "c"]


def test_toggled_filters_when_absent():
    assert mod._toggled(["a", "b"], "a", False) == ["b"]
    assert mod._toggled(["a", "b"], "a", True) == ["b", "a"]


def test_override_path_non_extension_leaf():
    assert mod._override_path([DISABLED_GROUPS[0]]) == [DISABLED_GROUPS[0]]


def test_override_path_user_instructions():
    name = harness_fields.user_instruction_source_name("ext")
    assert mod._override_path(["extension_instances", "ext", USER_INSTR]) == [
        "instruction_sources",
        name,
    ]


def test_override_path_settings():
    assert mod._override_path(["extension_instances", "ext", SETTINGS, "key"]) == [
        "extension_instances",
        "ext",
        SETTINGS,
        "key",
    ]


def test_override_path_generic_group():
    assert mod._override_path(["extension_instances", "ext", SKILLS]) == [
        "extension_instances",
        "ext",
        SKILLS,
    ]


# --- profile-meta writes ----------------------------------------------------


def test_meta_set_routes_to_set_profile_meta(install):
    res = apply(NAMED_PID, [{"path": [META, "base_profile_id"], "value": "base"}])
    assert res is install["meta_return"]
    assert install["meta"] == [(NAMED_PID, {"base_profile_id": "base"}, "r1")]
    assert install["applied"] == []


def test_meta_clear_stores_none(install):
    apply(NAMED_PID, [{"path": [META, "default_model"], "clear": True}])
    assert install["meta"] == [(NAMED_PID, {"default_model": None}, "r1")]


def test_meta_on_default_profile_raises(install):
    with pytest.raises(HarnessFieldError, match="Default profile"):
        apply(DEFAULT_PID, [{"path": [META, "base_profile_id"], "value": "b"}])


@pytest.mark.parametrize("path", [[META], [META, "bogus"], [META, "base_profile_id", "extra"]])
def test_meta_invalid_path_raises(install, path):
    with pytest.raises(HarnessFieldError):
        apply(NAMED_PID, [{"path": path, "value": "x"}])


# --- clear writes -----------------------------------------------------------


def test_clear_on_default_profile_raises(install):
    with pytest.raises(HarnessFieldError, match="Only a named profile"):
        apply(DEFAULT_PID, [{"path": [DISABLED_GROUPS[0], "t"], "clear": True}])


def test_clear_global_scope_raises(install):
    with pytest.raises(HarnessFieldError, match="Only a named profile"):
        apply(NAMED_PID, [{"path": ["extension_instances", "ext", NATIVE, "k"], "clear": True}])


def test_clear_disabled_non_extension(install):
    res = apply(NAMED_PID, [{"path": [DISABLED_GROUPS[0]], "clear": True}])
    assert res is install["apply_return"]
    assert install["applied"] == [
        (NAMED_PID, [{"path": [DISABLED_GROUPS[0]], "op": "clear"}], "r1")
    ]


def test_clear_generic_extension_group(install):
    install["resolved"] = _ext_resolved("ext", SKILLS, ["s"])
    apply(NAMED_PID, [{"path": ["extension_instances", "ext", SKILLS], "clear": True}])
    assert install["applied"][-1][1] == [
        {"path": ["extension_instances", "ext", SKILLS], "op": "clear"}
    ]


def test_clear_user_instructions(install):
    install["resolved"] = _ext_resolved("ext", USER_INSTR, [])
    name = harness_fields.user_instruction_source_name("ext")
    apply(NAMED_PID, [{"path": ["extension_instances", "ext", USER_INSTR], "clear": True}])
    assert install["applied"][-1][1] == [{"path": ["instruction_sources", name], "op": "clear"}]


def test_clear_settings(install):
    install["resolved"] = _ext_resolved("ext", SETTINGS, {})
    apply(
        NAMED_PID,
        [{"path": ["extension_instances", "ext", SETTINGS, "key"], "clear": True}],
    )
    assert install["applied"][-1][1] == [
        {"path": ["extension_instances", "ext", SETTINGS, "key"], "op": "clear"}
    ]


# --- write_default passthrough ---------------------------------------------


def test_default_profile_write_passes_through_and_returns_none(install):
    res = apply(DEFAULT_PID, [{"path": [DISABLED_GROUPS[0], "t"], "value": True}])
    assert res is None
    assert install["written"] == [([DISABLED_GROUPS[0], "t"], True)]
    assert install["applied"] == [] and install["gets"] == []


def test_named_global_scope_write_passes_through(install):
    res = apply(
        NAMED_PID, [{"path": ["extension_instances", "ext", NATIVE, "k"], "value": True}]
    )
    assert res is install["get_return"]
    assert install["written"] == [(["extension_instances", "ext", NATIVE, "k"], True)]
    assert install["applied"] == []
    # The no-ops/no-meta fall-through reads the current record without a revision.
    assert install["gets"] == [(NAMED_PID, None)]


# --- disabled-group toggles -------------------------------------------------


def test_disabled_toggle_add(install):
    head = DISABLED_GROUPS[0]
    install["resolved"] = _disabled_resolved(head, ["other"])
    install["default"] = {head: ["other"]}
    apply(NAMED_PID, [{"path": [head, "t"], "value": False}])
    ops = install["applied"][-1][1]
    assert ops == [{"path": [head], "op": "set", "value": {"add": ["t"], "remove": []}}]


def test_disabled_toggle_remove(install):
    head = DISABLED_GROUPS[1]
    install["resolved"] = _disabled_resolved(head, ["t"])
    install["default"] = {head: ["t"]}
    apply(NAMED_PID, [{"path": [head, "t"], "value": True}])
    ops = install["applied"][-1][1]
    assert ops == [{"path": [head], "op": "set", "value": {"add": [], "remove": ["t"]}}]


def test_disabled_two_writes_compose_on_one_leaf(install):
    head = DISABLED_GROUPS[2]
    install["resolved"] = _disabled_resolved(head, [])
    install["default"] = {head: []}
    apply(
        NAMED_PID,
        [{"path": [head, "a"], "value": False}, {"path": [head, "b"], "value": False}],
    )
    ops = install["applied"][-1][1]
    # Each toggle emits its own delta op; the shared working list composes them,
    # so the second op carries the cumulative add against Default.
    assert len(ops) == 2
    assert ops[-1] == {"path": [head], "op": "set", "value": {"add": ["a", "b"], "remove": []}}


# --- extension instances ----------------------------------------------------


def test_extension_not_active_raises(install):
    install["resolved"] = {"extension_instances": {"other": {}}}
    with pytest.raises(HarnessFieldError, match="not active"):
        apply(NAMED_PID, [{"path": ["extension_instances", "ghost", SKILLS, "s"], "value": True}])


def test_user_instructions_set(install):
    install["resolved"] = _ext_resolved("ext", USER_INSTR, [])
    name = harness_fields.user_instruction_source_name("ext")
    apply(NAMED_PID, [{"path": ["extension_instances", "ext", USER_INSTR], "value": " hi "}])
    assert install["applied"][-1][1] == [
        {"path": ["instruction_sources", name], "op": "set", "value": {"kind": "inline", "content": "hi"}}
    ]


def test_user_instructions_empty_clears(install):
    install["resolved"] = _ext_resolved("ext", USER_INSTR, [])
    name = harness_fields.user_instruction_source_name("ext")
    apply(NAMED_PID, [{"path": ["extension_instances", "ext", USER_INSTR], "value": "  "}])
    assert install["applied"][-1][1] == [
        {"path": ["instruction_sources", name], "op": "clear"}
    ]


def test_settings_set(install):
    install["resolved"] = _ext_resolved("ext", SETTINGS, {})
    install["extensions"] = {"ext": _settings_record("key")}
    apply(NAMED_PID, [{"path": ["extension_instances", "ext", SETTINGS, "key"], "value": 123}])
    op = install["applied"][-1][1][0]
    assert op["path"] == ["extension_instances", "ext", SETTINGS, "key"]
    assert op["op"] == "set"
    assert op["value"]["value"] == 123
    assert op["value"]["schema_hash"] == harness_fields.setting_schema_hash(
        _settings_record("key"), "key"
    )


def test_settings_not_installed_raises(install):
    install["resolved"] = _ext_resolved("ext", SETTINGS, {})
    install["extensions"] = {}
    with pytest.raises(HarnessFieldError, match="not installed"):
        apply(NAMED_PID, [{"path": ["extension_instances", "ext", SETTINGS, "key"], "value": 1}])


def test_generic_extension_toggle_add(install):
    install["resolved"] = _ext_resolved("ext", SKILLS, [])
    install["default"] = {"extension_instances": {"ext": {SKILLS: []}}}
    apply(NAMED_PID, [{"path": ["extension_instances", "ext", SKILLS, "s"], "value": True}])
    assert install["applied"][-1][1] == [
        {"path": ["extension_instances", "ext", SKILLS], "op": "set", "value": {"add": ["s"], "remove": []}}
    ]


def test_generic_extension_toggle_remove(install):
    install["resolved"] = _ext_resolved("ext", SKILLS, ["s"])
    install["default"] = {"extension_instances": {"ext": {SKILLS: ["s"]}}}
    apply(NAMED_PID, [{"path": ["extension_instances", "ext", SKILLS, "s"], "value": False}])
    assert install["applied"][-1][1] == [
        {"path": ["extension_instances", "ext", SKILLS], "op": "set", "value": {"add": [], "remove": ["s"]}}
    ]


# --- final assembly ---------------------------------------------------------


def test_final_ops_plus_meta_chains_revisions(install):
    install["resolved"] = _ext_resolved("ext", SKILLS, [])
    install["default"] = {"extension_instances": {"ext": {SKILLS: []}}}
    apply(
        NAMED_PID,
        [
            {"path": ["extension_instances", "ext", SKILLS, "s"], "value": True},
            {"path": [META, "base_profile_id"], "value": "base"},
        ],
    )
    assert install["applied"] == [(NAMED_PID, install["applied"][0][1], "r1")]
    # meta chains off the apply_override_patch result revision ("r2").
    assert install["meta"] == [(NAMED_PID, {"base_profile_id": "base"}, "r2")]


def test_final_meta_only_uses_request_revision(install):
    res = apply(NAMED_PID, [{"path": [META, "base_profile_id"], "value": "base"}])
    assert res is install["meta_return"]
    assert install["applied"] == []
    assert install["meta"] == [(NAMED_PID, {"base_profile_id": "base"}, "r1")]
