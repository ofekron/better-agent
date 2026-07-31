"""Unit-tier pytest coverage for ``extension_context_audit``.

The sibling ``test_extension_context_audit.py`` is a standalone ``__main__``
script (``t_*`` functions) and is therefore not collected by pytest, so its
coverage never reached the unit number. These tests re-exercise the pure
validation/projection/cache logic under pytest AND cover the strict
input-validation branches and cache edge paths the standalone does not reach.

Run with:
    cd backend && env -u PYTHONPATH .venv/bin/python -m pytest \
        scripts/test_extension_context_audit_unit.py --cov=extension_context_audit
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402

_test_home.isolate("ba-ext-ctx-audit-unit-")

import extension_context_audit as audit  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _reset_state() -> None:
    audit._IN_FLIGHT.clear()
    audit._FAILURES.clear()
    audit._CACHE_PROJECTION.clear()
    cache_dir = audit._cache_dir()
    if cache_dir.exists():
        for path in cache_dir.glob("*.json"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _build_projection(**overrides) -> dict:
    base = dict(
        provider_kind="codex",
        profile_id="default",
        profile_revision="7",
        runtime_skills=[{"name": "review"}],
        capability_contexts=[{"source_id": "src", "capability_id": "cap"}],
        extension_mcp_servers={"ext.tools": ["search"]},
        extension_skills={"ext.skills": ["verify"]},
        extension_instruction_names={"ext.rules": ["security"]},
    )
    base.update(overrides)
    return audit.build_projection(**base)


def _ref_for(kind: str, identity: dict) -> str:
    canonical = audit._canonical_json({"kind": kind, "identity": identity})
    return f"{kind}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _projection_with(entries: list, identities: dict, **meta) -> dict:
    proj = {
        "version": audit._AUDIT_VERSION,
        "provider_kind": meta.get("provider_kind", "codex"),
        "profile_id": meta.get("profile_id", "p"),
        "profile_revision": meta.get("profile_revision", "r"),
        "entries": entries,
        "identities": identities,
    }
    return proj


def _valid_projection_pair() -> dict:
    """A minimal projection with one consistent skill entry/identity."""
    identity = {"extension_id": "ext", "name": "n"}
    ref = _ref_for("skill", identity)
    entries = [{"ref": ref, "kind": "skill", "display": "n"}]
    identities = {ref: {"kind": "skill", "identity": identity}}
    return _projection_with(entries, identities)


def _finding_result(proj: dict) -> dict:
    first, second = proj["entries"][:2]
    return {
        "status": "findings",
        "findings": [
            {"code": "potential_overlap", "refs": [first["ref"], second["ref"]]}
        ],
    }


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_each():
    _reset_state()
    yield
    _reset_state()


# --------------------------------------------------------------------------- #
# build_projection / display sanitization
# --------------------------------------------------------------------------- #
def test_build_projection_happy_path_sorts_and_fingerprints():
    proj = _build_projection()
    refs = [e["ref"] for e in proj["entries"]]
    assert refs == sorted(refs)
    assert proj["version"] == audit._AUDIT_VERSION
    # display is included only when it matches the safe-display regex.
    for entry in proj["entries"]:
        assert "display" in entry


def test_build_projection_drops_unsafe_display():
    proj = _build_projection(runtime_skills=[{"name": "<bad>"}])
    # The unsafe runtime name is hashed into its ref but not echoed as display.
    unsafe = [
        e for e in proj["entries"]
        if e["kind"] == "skill"
        and proj["identities"][e["ref"]]["identity"].get("source") == "runtime"
    ]
    assert unsafe and "display" not in unsafe[0]


def test_build_projection_rejects_non_object_runtime_skill():
    with pytest.raises(ValueError, match="runtime skill identity must be an object"):
        audit.build_projection(
            provider_kind="codex",
            profile_id="p",
            profile_revision="r",
            runtime_skills=["not-a-dict"],
            capability_contexts=[],
            extension_mcp_servers={},
            extension_skills={},
            extension_instruction_names={},
        )


def test_build_projection_rejects_non_object_capability():
    with pytest.raises(ValueError, match="capability identity must be an object"):
        audit.build_projection(
            provider_kind="codex",
            profile_id="p",
            profile_revision="r",
            runtime_skills=[],
            capability_contexts=["nope"],
            extension_mcp_servers={},
            extension_skills={},
            extension_instruction_names={},
        )


def test_build_projection_optional_revision_can_be_dropped():
    proj = audit.build_projection(
        provider_kind="codex",
        profile_id="p",
        profile_revision="",  # empty -> optional, allowed through
        runtime_skills=[],
        capability_contexts=[],
        extension_mcp_servers={},
        extension_skills={},
        extension_instruction_names={},
    )
    assert proj["profile_revision"] == ""


# --------------------------------------------------------------------------- #
# _identity_string / _bounded_string / _add_extension_entries validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [None, 1, "", "x" * 1001])
def test_identity_string_rejects(bad):
    with pytest.raises(ValueError, match="must be a non-empty bounded string"):
        audit._identity_string(bad, "field")


def test_identity_string_accepts_boundary():
    assert audit._identity_string("x" * 1000, "field") == "x" * 1000


def test_bounded_string_required_rejects_empty_and_non_str():
    with pytest.raises(ValueError, match="must be a string"):
        audit._bounded_string(None, "f")
    with pytest.raises(ValueError, match="must be non-empty"):
        audit._bounded_string("", "f")
    with pytest.raises(ValueError, match="too large"):
        audit._bounded_string("y" * 1001, "f")


def test_bounded_string_optional_allows_empty():
    assert audit._bounded_string("", "f", required=False) == ""


def test_add_extension_entries_rejects_non_dict_values():
    def add(kind, identity, display=""):
        raise AssertionError("add must not be called for invalid input")

    with pytest.raises(ValueError, match="extension mcp identities must be an object"):
        audit._add_extension_entries(add, "mcp", ["not-a-dict"])


def test_add_extension_entries_rejects_non_list_names():
    captured = []

    def add(kind, identity, display=""):
        captured.append((kind, identity, display))

    with pytest.raises(ValueError, match="extension skill identities must be lists"):
        audit._add_extension_entries(add, "skill", {"ext": "not-a-list"})
    assert captured == []


def test_add_extension_entries_emits_owner_prefixed_display():
    captured = []

    def add(kind, identity, display=""):
        captured.append((kind, identity, display))

    audit._add_extension_entries(add, "instruction", {"ext.rules": ["sec"]})
    assert captured == [
        ("instruction", {"extension_id": "ext.rules", "name": "sec"}, "ext.rules:sec")
    ]


# --------------------------------------------------------------------------- #
# _validate_projection — every invalid branch
# --------------------------------------------------------------------------- #
def test_validate_projection_accepts_valid():
    assert audit._validate_projection(_valid_projection_pair())["version"] == audit._AUDIT_VERSION


def test_validate_projection_rejects_non_dict():
    with pytest.raises(ValueError, match="invalid audit projection"):
        audit._validate_projection(["nope"])


def test_validate_projection_rejects_wrong_key_set():
    proj = _valid_projection_pair()
    proj["extra"] = 1
    with pytest.raises(ValueError, match="invalid audit projection"):
        audit._validate_projection(proj)
    bad = _valid_projection_pair()
    del bad["entries"]
    with pytest.raises(ValueError, match="invalid audit projection"):
        audit._validate_projection(bad)


def test_validate_projection_rejects_wrong_version():
    proj = _valid_projection_pair()
    proj["version"] = 999
    with pytest.raises(ValueError, match="unsupported audit projection version"):
        audit._validate_projection(proj)


def test_validate_projection_rejects_entries_or_identities_wrong_type():
    proj = _valid_projection_pair()
    proj["entries"] = "x"
    with pytest.raises(ValueError, match="entries and identities are required"):
        audit._validate_projection(proj)
    proj2 = _valid_projection_pair()
    proj2["identities"] = "x"
    with pytest.raises(ValueError, match="entries and identities are required"):
        audit._validate_projection(proj2)


def test_validate_projection_rejects_entry_extra_key():
    proj = _valid_projection_pair()
    proj["entries"][0]["extra"] = 1
    with pytest.raises(ValueError, match="invalid audit projection entry"):
        audit._validate_projection(proj)


def test_validate_projection_rejects_bad_ref_format():
    proj = _valid_projection_pair()
    proj["entries"][0]["ref"] = "skill:tooshort"
    with pytest.raises(ValueError, match="invalid audit projection ref"):
        audit._validate_projection(proj)


def test_validate_projection_rejects_kind_ref_prefix_mismatch():
    identity = {"extension_id": "ext", "name": "n"}
    skill_ref = _ref_for("skill", identity)
    proj = _valid_projection_pair()
    # Swap kind so it disagrees with the ref prefix.
    proj["entries"][0] = {"ref": skill_ref, "kind": "mcp", "display": "n"}
    with pytest.raises(ValueError, match="inconsistent audit projection ref"):
        audit._validate_projection(proj)


def test_validate_projection_rejects_duplicate_ref():
    proj = _valid_projection_pair()
    dup = copy.deepcopy(proj["entries"][0])
    proj["entries"].append(dup)
    proj["identities"][dup["ref"]] = copy.deepcopy(proj["identities"][dup["ref"]])
    # identities must match the (deduplicated) seen set, so this also trips the
    # identities/entries mismatch — either ValueError is acceptable.
    with pytest.raises(ValueError):
        audit._validate_projection(proj)


def test_validate_projection_rejects_bad_display():
    proj = _valid_projection_pair()
    proj["entries"][0]["display"] = "<unsafe>"
    with pytest.raises(ValueError, match="invalid audit projection display"):
        audit._validate_projection(proj)


def test_validate_projection_rejects_identities_mismatch_seen():
    proj = _valid_projection_pair()
    # Drop the identity entry so identities set != seen entries set.
    proj["identities"] = {}
    with pytest.raises(ValueError, match="identities must match entries"):
        audit._validate_projection(proj)


def test_validate_projection_rejects_identity_wrong_keys():
    proj = _valid_projection_pair()
    ref = next(iter(proj["identities"]))
    proj["identities"][ref] = {"kind": "skill"}  # missing identity
    with pytest.raises(ValueError, match="invalid audit projection identity"):
        audit._validate_projection(proj)


def test_validate_projection_rejects_identity_ref_mismatch():
    proj = _valid_projection_pair()
    ref = next(iter(proj["identities"]))
    # Tamper with the identity so the recomputed ref no longer matches.
    proj["identities"][ref]["identity"]["name"] = "tampered"
    with pytest.raises(ValueError, match="identity does not match ref"):
        audit._validate_projection(proj)


# --------------------------------------------------------------------------- #
# _validate_original_identity — each kind + invalid shapes
# --------------------------------------------------------------------------- #
def test_validate_original_identity_capability_shape():
    out = audit._validate_original_identity(
        "capability", {"source_id": "s", "capability_id": "c"}
    )
    assert out == {"source_id": "s", "capability_id": "c"}


def test_validate_original_identity_runtime_skill_shape():
    out = audit._validate_original_identity(
        "skill", {"source": "runtime", "name": "n"}
    )
    assert out == {"source": "runtime", "name": "n"}


def test_validate_original_identity_extension_shapes():
    for kind in ("skill", "mcp", "instruction"):
        out = audit._validate_original_identity(
            kind, {"extension_id": "e", "name": "n"}
        )
        assert out == {"extension_id": "e", "name": "n"}


def test_validate_original_identity_rejects_unknown_kind():
    with pytest.raises(ValueError, match="invalid audit projection identity kind"):
        audit._validate_original_identity("unknown", {"a": "b"})


def test_validate_original_identity_rejects_wrong_shape():
    with pytest.raises(ValueError, match="invalid audit projection identity shape"):
        audit._validate_original_identity("mcp", {"source": "runtime", "name": "n"})


# --------------------------------------------------------------------------- #
# runtime_context — cached hit, bare_config, refresh gating
# --------------------------------------------------------------------------- #
def test_runtime_context_bare_config_returns_empty():
    assert audit.runtime_context(_valid_projection_pair(), bare_config=True) == []


def test_runtime_context_cold_miss_triggers_refresh_and_returns_empty():
    proj = _build_projection()
    triggered = []
    original = audit._trigger_refresh
    try:
        audit._trigger_refresh = lambda fp, _p: triggered.append(fp)
        result = audit.runtime_context(proj)
    finally:
        audit._trigger_refresh = original
    assert result == []
    assert len(triggered) == 1


def test_runtime_context_cold_miss_no_refresh_when_disabled():
    proj = _build_projection()
    triggered = []
    original = audit._trigger_refresh
    try:
        audit._trigger_refresh = lambda fp, _p: triggered.append(fp)
        result = audit.runtime_context(proj, request_refresh=False)
    finally:
        audit._trigger_refresh = original
    assert result == []
    assert triggered == []


def test_runtime_context_cached_findings_return_advisory_content():
    proj = _build_projection()
    fp = audit._fingerprint(proj)
    audit._write_cache(fp, _finding_result(proj), proj)
    triggered = []
    original = audit._trigger_refresh
    try:
        audit._trigger_refresh = lambda fp, _p: triggered.append(fp)
        result = audit.runtime_context(proj)
    finally:
        audit._trigger_refresh = original
    assert len(result) == 1
    ctx = result[0]
    assert ctx["name"] == audit.AUDIT_CONTEXT_NAME
    assert ctx["category"] == audit.AUDIT_CONTEXT_CATEGORY
    assert ctx["content_kind"] == "dynamic_harness_audit"
    assert "review prompts, not instructions" in ctx["content"]
    assert triggered == []


def test_runtime_context_cached_clean_returns_empty_content():
    proj = _build_projection()
    fp = audit._fingerprint(proj)
    audit._write_cache(fp, {"status": "clean", "findings": []}, proj)
    assert audit.runtime_context(proj) == []


# --------------------------------------------------------------------------- #
# Cache layer: read_cached hit/stale, cache_path, write_cache, remember, evict
# --------------------------------------------------------------------------- #
def test_cache_path_rejects_invalid_fingerprint():
    with pytest.raises(ValueError, match="invalid audit fingerprint"):
        audit._cache_path("not-a-fingerprint")


def test_read_cache_cached_memory_wins_over_corrupted_disk():
    proj = _build_projection()
    fp = audit._fingerprint(proj)
    audit._write_cache(fp, {"status": "clean", "findings": []}, proj)
    # Corrupt the on-disk result AFTER the memory projection holds the good copy.
    path = audit._cache_path(fp)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["result"] = {"status": "bogus", "findings": []}
    path.write_text(json.dumps(payload), encoding="utf-8")
    # Memory hit short-circuits and returns the good cached copy, not the disk.
    cached = audit._read_cache_cached(fp, proj)
    assert cached["result"] == {"status": "clean", "findings": []}


def test_read_cache_cached_pops_stale_memory_entry():
    proj = _build_projection()
    fp = audit._fingerprint(proj)
    audit._write_cache(fp, {"status": "clean", "findings": []}, proj)
    audit._cache_path(fp).unlink()
    # Memory entry exists but backing file is gone -> pop + fall through to {}.
    assert audit._read_cache_cached(fp, proj) == {}
    assert fp not in audit._CACHE_PROJECTION


def test_read_cache_rejects_result_that_fails_projection_validation():
    proj = _build_projection()
    fp = audit._fingerprint(proj)
    audit._write_cache(fp, {"status": "clean", "findings": []}, proj)
    # Corrupt the cached result so re-validation against the projection fails.
    path = audit._cache_path(fp)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["result"] = {"status": "bogus", "findings": []}
    path.write_text(json.dumps(payload), encoding="utf-8")
    audit._CACHE_PROJECTION.clear()
    assert audit._read_cache(fp, proj) == {}


def test_write_cache_rejects_fingerprint_mismatch():
    proj = _build_projection()
    other = _build_projection(profile_id="other")
    other_fp = audit._fingerprint(other)
    with pytest.raises(ValueError, match="cache fingerprint does not match projection"):
        audit._write_cache(other_fp, {"status": "clean", "findings": []}, proj)


def test_remember_cache_evicts_to_max_entries():
    original_max = audit._MAX_CACHE_ENTRIES
    audit._MAX_CACHE_ENTRIES = 3
    try:
        for i in range(5):
            fp = hashlib.sha256(str(i).encode()).hexdigest()
            audit._remember_cache(fp, {"i": i})
        assert len(audit._CACHE_PROJECTION) == 3
    finally:
        audit._MAX_CACHE_ENTRIES = original_max


def test_remember_cache_prunes_by_live_fingerprints():
    original_max = audit._MAX_CACHE_ENTRIES
    audit._MAX_CACHE_ENTRIES = 10
    # Seed two stale memory entries.
    audit._CACHE_PROJECTION["stale1"] = {"x": 1}
    audit._CACHE_PROJECTION["stale2"] = {"x": 2}
    live_fp = hashlib.sha256(b"live").hexdigest()
    audit._remember_cache(live_fp, {"x": 3}, live_fingerprints={live_fp})
    # Stale entries not in the live set are pruned.
    assert "stale1" not in audit._CACHE_PROJECTION
    assert "stale2" not in audit._CACHE_PROJECTION
    assert live_fp in audit._CACHE_PROJECTION
    audit._MAX_CACHE_ENTRIES = original_max


def test_evict_cache_entries_skips_non_fingerprint_files():
    directory = audit._cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "not-a-fingerprint.json").write_text("{}", encoding="utf-8")
    keep = directory / f'{hashlib.sha256(b"k").hexdigest()}.json'
    keep.write_text("{}", encoding="utf-8")
    live = audit._evict_cache_entries(directory, keep=keep)
    # Non-fingerprint file is neither counted nor removed.
    assert (directory / "not-a-fingerprint.json").exists()
    assert keep.stem in live


def test_evict_cache_entries_removes_oldest_excess():
    directory = audit._cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    original_max = audit._MAX_CACHE_ENTRIES
    audit._MAX_CACHE_ENTRIES = 2
    try:
        paths = []
        for i in range(4):
            p = directory / f"{hashlib.sha256(str(i).encode()).hexdigest()}.json"
            p.write_text("{}", encoding="utf-8")
            os.utime(p, ns=(i * 1_000_000, i * 1_000_000))
            paths.append(p)
        keep = paths[-1]
        live = audit._evict_cache_entries(directory, keep=keep)
        remaining = {p.stem for p in directory.glob("*.json")}
        # Two excess beyond the cap are removed; keep survives.
        assert keep.stem in live
        assert len(remaining) <= 2
    finally:
        audit._MAX_CACHE_ENTRIES = original_max


def test_atomic_write_json_cleans_up_tmp_on_failure(monkeypatch):
    target = audit._cache_dir() / "target.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    real_replace = os.replace

    def fail(tmp, dst):
        raise OSError("boom")

    monkeypatch.setattr(audit.os, "replace", fail)
    before = set(audit._cache_dir().glob(".*.tmp"))
    with pytest.raises(OSError, match="boom"):
        audit._atomic_write_json(target, {"a": 1})
    monkeypatch.setattr(audit.os, "replace", real_replace)
    after = set(audit._cache_dir().glob(".*.tmp"))
    # The tmp file created during the failed write was unlinked.
    assert len(after) == len(before)
    assert not target.exists()


# --------------------------------------------------------------------------- #
# _trigger_refresh in-flight dedup + backoff
# --------------------------------------------------------------------------- #
def test_trigger_refresh_dedups_in_flight():
    proj = _build_projection()
    fp = audit._fingerprint(proj)
    started = []
    original_thread = audit.threading.Thread

    class _NoThread:
        def __init__(self, **kwargs):
            started.append(kwargs)

        def start(self):
            pass

    try:
        audit.threading.Thread = _NoThread
        audit._IN_FLIGHT.add(fp)
        audit._trigger_refresh(fp, proj)
    finally:
        audit.threading.Thread = original_thread
        audit._IN_FLIGHT.discard(fp)
    assert started == []


def test_trigger_refresh_suppressed_by_backoff():
    proj = _build_projection()
    fp = audit._fingerprint(proj)
    started = []
    original_thread = audit.threading.Thread

    class _NoThread:
        def __init__(self, **kwargs):
            started.append(kwargs)

        def start(self):
            pass

    try:
        audit.threading.Thread = _NoThread
        # A backoff failure window still in the future suppresses the refresh.
        audit._FAILURES[fp] = (2, float("inf"))
        audit._trigger_refresh(fp, proj)
    finally:
        audit.threading.Thread = original_thread
        audit._FAILURES.pop(fp, None)
    assert started == []


def test_refresh_cache_success_clears_failure_and_caches(monkeypatch):
    proj = _build_projection()
    fp = audit._fingerprint(proj)
    monkeypatch.setattr(
        audit.provisioning,
        "run_sync",
        lambda *a, **k: SimpleNamespace(value={"status": "clean", "findings": []}),
    )
    audit._FAILURES[fp] = (3, 0.0)
    audit._refresh_cache(fp, proj)
    assert fp not in audit._FAILURES
    assert audit._cache_path(fp).exists()
    assert fp not in audit._IN_FLIGHT


def test_refresh_cache_failure_enters_backoff_no_cache(monkeypatch):
    proj = _build_projection()
    fp = audit._fingerprint(proj)

    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(audit.provisioning, "run_sync", boom)
    audit._refresh_cache(fp, proj)
    assert fp in audit._FAILURES
    assert not audit._cache_path(fp).exists()
    assert fp not in audit._IN_FLIGHT


# --------------------------------------------------------------------------- #
# _parse_json_object / _validate_audit_result / _render_context / _llm_projection
# --------------------------------------------------------------------------- #
def test_parse_json_object_rejects_non_string():
    with pytest.raises(ValueError, match="must be one JSON object"):
        audit._parse_json_object(None)  # type: ignore[arg-type]


def test_parse_json_object_rejects_non_object_json():
    with pytest.raises(ValueError, match="must be one JSON object"):
        audit._parse_json_object("[1, 2, 3]")


def test_validate_audit_result_rejects_too_many_findings():
    proj = _build_projection()
    ref = proj["entries"][0]["ref"]
    findings = [
        {"code": "identifier_clarity", "refs": [ref]} for _ in range(audit._MAX_FINDINGS + 1)
    ]
    with pytest.raises(ValueError, match="too many audit findings"):
        audit._validate_audit_result({"status": "findings", "findings": findings}, proj)


def test_validate_audit_result_rejects_duplicate_finding():
    proj = _build_projection()
    ref = proj["entries"][0]["ref"]
    finding = {"code": "identifier_clarity", "refs": [ref]}
    with pytest.raises(ValueError, match="duplicate audit finding"):
        audit._validate_audit_result({"status": "findings", "findings": [finding, finding]}, proj)


def test_validate_audit_result_rejects_wrong_arity():
    proj = _build_projection()
    first, second = proj["entries"][:2]
    # potential_overlap requires exactly 2 refs.
    with pytest.raises(ValueError, match="invalid audit finding rule"):
        audit._validate_audit_result(
            {"status": "findings", "findings": [{"code": "potential_overlap", "refs": [first["ref"]]}]},
            proj,
        )


def test_validate_audit_result_rejects_duplicate_refs_in_one_finding():
    proj = _build_projection()
    ref = proj["entries"][0]["ref"]
    # potential_overlap has arity 2, so the arity gate passes and the
    # distinctness check is what rejects the repeated ref.
    with pytest.raises(ValueError, match="refs must be distinct"):
        audit._validate_audit_result(
            {"status": "findings", "findings": [{"code": "potential_overlap", "refs": [ref, ref]}]},
            proj,
        )


def test_validate_audit_result_rejects_non_string_ref():
    proj = _build_projection()
    with pytest.raises(ValueError, match="invalid audit finding refs"):
        audit._validate_audit_result(
            {"status": "findings", "findings": [{"code": "identifier_clarity", "refs": [5]}]},
            proj,
        )


def test_validate_audit_result_rejects_unavailable_ref_kind():
    proj = _build_projection()
    # Fabricate a ref that is well-formed but absent from the projection.
    ghost = _ref_for("skill", {"extension_id": "ghost", "name": "g"})
    with pytest.raises(ValueError, match="ref is unavailable"):
        audit._validate_audit_result(
            {"status": "findings", "findings": [{"code": "identifier_clarity", "refs": [ghost]}]},
            proj,
        )


def test_validate_audit_result_rejects_extra_finding_key():
    proj = _build_projection()
    ref = proj["entries"][0]["ref"]
    with pytest.raises(ValueError, match="invalid audit finding"):
        audit._validate_audit_result(
            {"status": "findings", "findings": [{"code": "identifier_clarity", "refs": [ref], "text": "x"}]},
            proj,
        )


def test_validate_audit_result_rejects_status_mismatch_findings():
    with pytest.raises(ValueError, match="findings audit must contain findings"):
        audit._validate_audit_result({"status": "findings", "findings": []}, _build_projection())
    with pytest.raises(ValueError, match="clean audit cannot contain findings"):
        audit._validate_audit_result(
            {"status": "clean", "findings": [{"code": "x", "refs": []}]}, _build_projection()
        )


def test_render_context_clean_is_empty():
    proj = _build_projection()
    assert audit._render_context({"status": "clean", "findings": []}, proj) == ""


def test_render_context_identifier_clarity_uses_single_ref():
    proj = _build_projection()
    ref = proj["entries"][0]["ref"]
    result = {"status": "findings", "findings": [{"code": "identifier_clarity", "refs": [ref]}]}
    content = audit._render_context(result, proj)
    assert "clear enough to distinguish its purpose" in content
    assert content.startswith("Advisory harness review")


def test_render_context_overlapping_pair_message():
    proj = _build_projection()
    content = audit._render_context(_finding_result(proj), proj)
    assert "overlap" in content


def test_llm_projection_strips_unsafe_display_fields():
    proj = _build_projection(provider_kind="<unsafe>", profile_id="ok")
    payload = audit._llm_projection(proj)
    assert payload["provider_kind"] == ""  # unsafe display dropped for the model
    assert payload["profile"] == "ok"


def test_llm_projection_includes_entries_and_version():
    proj = _build_projection()
    payload = audit._llm_projection(proj)
    assert payload["version"] == proj["version"]
    assert payload["entries"] == proj["entries"]


# --------------------------------------------------------------------------- #
# ExtensionContextAuditSpec.parse_result
# --------------------------------------------------------------------------- #
def test_spec_parse_result_requires_projection():
    with pytest.raises(ValueError, match="audit projection is required"):
        audit.AUDIT_SPEC.parse_result("{}", {})


def test_spec_parse_result_rejects_missing_projection_key():
    with pytest.raises(ValueError, match="audit projection is required"):
        audit.AUDIT_SPEC.parse_result("{}", {"not_projection": {}})


def test_spec_build_instructions_wraps_query():
    text = audit.AUDIT_SPEC.build_instructions("hello", {})
    assert "<active-contextual-harness-mix>" in text
    assert "hello" in text


def test_spec_parse_result_happy_path_validates():
    proj = _build_projection()
    parsed = audit.AUDIT_SPEC.parse_result(
        '{"status": "clean", "findings": []}', {"projection": proj}
    )
    assert parsed == {"status": "clean", "findings": []}


# --------------------------------------------------------------------------- #
# _parse_json_object direct + _validate_audit_result top-level + read paths
# --------------------------------------------------------------------------- #
def test_parse_json_object_plain_object():
    assert audit._parse_json_object('{"a": 1}') == {"a": 1}


def test_parse_json_object_strips_code_fence():
    assert audit._parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_object_rejects_invalid_json():
    with pytest.raises(ValueError, match="must be one JSON object"):
        audit._parse_json_object("not json at all")


def test_validate_audit_result_rejects_non_dict():
    with pytest.raises(ValueError, match="invalid audit result"):
        audit._validate_audit_result("nope", _build_projection())


def test_validate_audit_result_rejects_wrong_keys():
    with pytest.raises(ValueError, match="invalid audit result"):
        audit._validate_audit_result({"status": "clean"}, _build_projection())


def test_read_cache_remembers_after_memory_miss():
    proj = _build_projection()
    fp = audit._fingerprint(proj)
    audit._write_cache(fp, {"status": "clean", "findings": []}, proj)
    audit._CACHE_PROJECTION.clear()
    cached = audit._read_cache_cached(fp, proj)
    assert cached["fingerprint"] == fp
    # A memory miss that hit disk re-populates the projection cache.
    assert fp in audit._CACHE_PROJECTION


def test_read_cache_rejects_wrong_top_level_shape():
    proj = _build_projection()
    fp = audit._fingerprint(proj)
    path = audit._cache_path(fp)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"unexpected": 1}), encoding="utf-8")
    assert audit._read_cache(fp) == {}


def test_read_cache_without_projection_returns_raw_data():
    proj = _build_projection()
    fp = audit._fingerprint(proj)
    audit._write_cache(fp, {"status": "clean", "findings": []}, proj)
    audit._CACHE_PROJECTION.clear()
    data = audit._read_cache(fp)
    assert data["fingerprint"] == fp
    assert data["schema_version"] == audit._CACHE_SCHEMA_VERSION


def test_trigger_refresh_starts_real_refresh_thread():
    proj = _build_projection()
    fp = audit._fingerprint(proj)
    original = audit.provisioning.run_sync

    def _ok(*args, **kwargs):
        return SimpleNamespace(value={"status": "clean", "findings": []})

    audit.provisioning.run_sync = _ok
    try:
        audit._trigger_refresh(fp, proj)
        # The daemon thread runs _refresh_cache end-to-end; wait for it to clear
        # the in-flight marker and publish the cache file.
        deadline_loop(audit, fp)
    finally:
        audit.provisioning.run_sync = original
    assert audit._cache_path(fp).exists()
    assert fp not in audit._IN_FLIGHT


def deadline_loop(audit, fp, iterations: int = 200, delay: float = 0.01) -> None:
    import time as _time

    for _ in range(iterations):
        if fp not in audit._IN_FLIGHT:
            return
        _time.sleep(delay)
    raise AssertionError("refresh thread did not drain in-flight marker in time")


def test_atomic_write_json_swallows_unlink_failure(monkeypatch):
    target = audit._cache_dir() / "target2.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    real_replace = os.replace
    real_unlink = os.unlink

    def fail_replace(tmp, dst):
        raise OSError("replace boom")

    def fail_unlink(path):
        raise OSError("unlink boom")

    monkeypatch.setattr(audit.os, "replace", fail_replace)
    monkeypatch.setattr(audit.os, "unlink", fail_unlink)
    with pytest.raises(OSError, match="replace boom"):
        audit._atomic_write_json(target, {"a": 1})
    monkeypatch.setattr(audit.os, "replace", real_replace)
    monkeypatch.setattr(audit.os, "unlink", real_unlink)
    assert not target.exists()


def test_evict_cache_entries_skips_stat_failure(monkeypatch):
    directory = audit._cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    keep = directory / f'{hashlib.sha256(b"k").hexdigest()}.json'
    keep.write_text("{}", encoding="utf-8")
    bad = directory / f'{hashlib.sha256(b"b").hexdigest()}.json'
    bad.write_text("{}", encoding="utf-8")
    real_stat = Path.stat

    def flaky_stat(self, *a, **kw):
        if self == bad:
            raise OSError("stat boom")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    live = audit._evict_cache_entries(directory, keep=keep)
    # The stat-failed file is skipped (not counted), keep survives.
    assert keep.stem in live
    assert bad.stem not in live
