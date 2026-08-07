"""Pytest owner for ``permission.py`` — the pure-logic permission selector.

The sibling ``test_permission.py`` is a standalone script (ignored by pytest
collection). This module is the pytest-counted owner so the selector's
vocabulary / normalize / resolve surface is covered in the unit tier.

All provider-record lookups in ``resolve_for_run`` are monkeypatched, so the
tests never depend on config_store's seeded state.
"""
from __future__ import annotations

import pytest

import config_store
import permission


# ── vocabularies ────────────────────────────────────────────────────────
def test_permission_constants_match_axes():
    assert permission.CLAUDE_PERMISSION_MODES == (
        "default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto")
    assert permission.CODEX_APPROVAL_POLICIES == (
        "untrusted", "on-request", "on-failure", "never")
    assert permission.CODEX_SANDBOX_MODES == (
        "read-only", "workspace-write", "danger-full-access")
    assert permission.SESSION_EVENTS_APPROVAL_MODES == ("auto_edit", "yolo", "plan")
    assert permission.OPENAI_PERMISSION_MODES == ("default", "bypassPermissions")
    # Every axis value set is exactly the kind's declared vocabulary.
    assert permission._AXES["claude"]["mode"] is permission.CLAUDE_PERMISSION_MODES
    assert permission._AXES["codex"]["approval"] is permission.CODEX_APPROVAL_POLICIES
    assert permission._AXES["codex"]["sandbox"] is permission.CODEX_SANDBOX_MODES
    # fugu is codex by reference — same axes and same default object identity.
    assert permission._AXES["fugu"] is permission._AXES["codex"]


def test_permission_axes_for_kind():
    assert permission.permission_axes_for_kind("claude") == {"mode": permission.CLAUDE_PERMISSION_MODES}
    # Returned dict is a copy — mutating it does not corrupt the registry.
    axes = permission.permission_axes_for_kind("codex")
    axes["approval"] = ()
    assert permission._AXES["codex"]["approval"] == permission.CODEX_APPROVAL_POLICIES
    # Unknown kind surfaces no axes.
    assert permission.permission_axes_for_kind("nope") == {}


def test_default_permission_for_kind():
    assert permission.default_permission_for_kind("claude") == {"mode": "bypassPermissions"}
    assert permission.default_permission_for_kind("codex") == {
        "approval": "never", "sandbox": "danger-full-access"}
    assert permission.default_permission_for_kind("qwen") == {"mode": "yolo"}
    assert permission.default_permission_for_kind("openai") == {"mode": "bypassPermissions"}
    # fugu inherits codex's default by reference.
    assert permission.default_permission_for_kind("fugu") == permission.default_permission_for_kind("codex")
    # Returned dict is a copy.
    d = permission.default_permission_for_kind("claude")
    d["mode"] = "x"
    assert permission.DEFAULT_PERMISSION["claude"] == {"mode": "bypassPermissions"}
    # Unknown kind → empty.
    assert permission.default_permission_for_kind("nope") == {}


# ── normalize_permission ────────────────────────────────────────────────
def test_normalize_permission_inherits_on_blank_or_non_dict():
    assert permission.normalize_permission("claude", None) is None
    assert permission.normalize_permission("claude", "") is None
    assert permission.normalize_permission("claude", {}) is None
    assert permission.normalize_permission("claude", "plan") is None
    assert permission.normalize_permission("claude", 123) is None
    # Unknown kind has no axes → None regardless of input.
    assert permission.normalize_permission("nope", {"mode": "plan"}) is None


def test_normalize_permission_keeps_valid_and_falls_back_on_invalid():
    assert permission.normalize_permission("claude", {"mode": "plan"}) == {"mode": "plan"}
    # Unknown axis value falls back to the kind default for that axis.
    assert permission.normalize_permission(
        "codex", {"approval": "bogus", "sandbox": "read-only"}) == {
        "approval": "never", "sandbox": "read-only"}
    # Missing axis → kind default; non-string raw → kind default.
    assert permission.normalize_permission("codex", {"approval": 7}) == {
        "approval": "never", "sandbox": "danger-full-access"}
    assert permission.normalize_permission("openai", {"mode": "bypassPermissions"}) == {
        "mode": "bypassPermissions"}
    # Extra unknown axes are dropped (not in the kind's axes).
    assert permission.normalize_permission(
        "claude", {"mode": "auto", "extra": "x"}) == {"mode": "auto"}


# ── clean_default_permission ────────────────────────────────────────────
def test_clean_default_permission_never_empty():
    # None/blank input → kind default (never empty).
    assert permission.clean_default_permission("claude", None) == {"mode": "bypassPermissions"}
    assert permission.clean_default_permission("claude", {}) == {"mode": "bypassPermissions"}
    # Valid value passes through normalized.
    assert permission.clean_default_permission("claude", {"mode": "plan"}) == {"mode": "plan"}
    # Unknown kind → normalize is None → default is also empty.
    assert permission.clean_default_permission("nope", {"mode": "plan"}) == {}


# ── resolve_permission ──────────────────────────────────────────────────
def test_resolve_permission_precedence():
    # Session override wins.
    assert permission.resolve_permission("qwen", {"mode": "plan"}, {"mode": "yolo"}) == {"mode": "plan"}
    # No session override → provider default.
    assert permission.resolve_permission("qwen", None, {"mode": "plan"}) == {"mode": "plan"}
    assert permission.resolve_permission("qwen", {}, {"mode": "plan"}) == {"mode": "plan"}
    # Neither → kind default.
    assert permission.resolve_permission("qwen", None, None) == {"mode": "yolo"}
    assert permission.resolve_permission("openai", None, None) == {"mode": "bypassPermissions"}
    # Invalid session override (bogus axis) normalizes, then still wins over provider default.
    assert permission.resolve_permission(
        "codex", {"approval": "bogus"}, {"approval": "on-request", "sandbox": "read-only"}) == {
        "approval": "never", "sandbox": "danger-full-access"}


# ── resolve_for_run ─────────────────────────────────────────────────────
def test_resolve_for_run_non_worker_uses_session_owner(monkeypatch):
    record = {"kind": "claude", "default_permission": {"mode": "plan"}, "runner": "claude_cli"}
    monkeypatch.setattr(config_store, "get_provider", lambda pid: record)
    sess = {"provider_id": "p1", "permission": {"mode": "acceptEdits"}}
    assert permission.resolve_for_run(
        sess_rec=sess, worker_sess_rec=None, is_worker=False) == {"mode": "acceptEdits"}


def test_resolve_for_run_worker_owns_worker_session(monkeypatch):
    record = {"kind": "codex", "default_permission": {"approval": "never", "sandbox": "read-only"},
              "runner": "codex_cli"}
    monkeypatch.setattr(config_store, "get_provider", lambda pid: record)
    sess = {"provider_id": "p1", "permission": {"mode": "default"}}
    worker = {"provider_id": "p2", "permission": {"approval": "on-request", "sandbox": "read-only"}}
    # Worker turn → owner is the worker session, not the app session.
    assert permission.resolve_for_run(
        sess_rec=sess, worker_sess_rec=worker, is_worker=True) == {
        "approval": "on-request", "sandbox": "read-only"}


def test_resolve_for_run_worker_without_worker_record_falls_back_to_session(monkeypatch):
    record = {"kind": "qwen", "default_permission": {"mode": "plan"}, "runner": "qwen_cli"}
    monkeypatch.setattr(config_store, "get_provider", lambda pid: record)
    sess = {"provider_id": "p1", "permission": {"mode": "yolo"}}
    # is_worker but no worker_sess_rec → owner is the app session.
    assert permission.resolve_for_run(
        sess_rec=sess, worker_sess_rec=None, is_worker=True) == {"mode": "yolo"}


def test_resolve_for_run_better_agent_runner_collapses_non_claude_to_openai(monkeypatch):
    # A non-claude kind on the better_agent_runner backend speaks OpenAI wire
    # format, so it must resolve against openai's defaults — not its own.
    record = {"kind": "codex", "default_permission": {"approval": "never", "sandbox": "read-only"},
              "runner": "better_agent_runner"}
    monkeypatch.setattr(config_store, "get_provider", lambda pid: record)
    sess = {"provider_id": "p1", "permission": {}}
    assert permission.resolve_for_run(
        sess_rec=sess, worker_sess_rec=None, is_worker=False) == {"mode": "bypassPermissions"}


def test_resolve_for_run_claude_on_better_agent_runner_stays_claude(monkeypatch):
    record = {"kind": "claude", "default_permission": {"mode": "plan"},
              "runner": "better_agent_runner"}
    monkeypatch.setattr(config_store, "get_provider", lambda pid: record)
    sess = {"provider_id": "p1", "permission": {}}
    assert permission.resolve_for_run(
        sess_rec=sess, worker_sess_rec=None, is_worker=False) == {"mode": "plan"}


def test_resolve_for_run_provider_lookup_failure_uses_fallback_kind(monkeypatch):
    def _boom(pid):
        raise RuntimeError("store unavailable")
    monkeypatch.setattr(config_store, "get_provider", _boom)
    sess = {"provider_id": "p1", "permission": {"mode": "acceptEdits"}}
    # Lookup raised → record None → kind from fallback → session override still wins.
    assert permission.resolve_for_run(
        sess_rec=sess, worker_sess_rec=None, is_worker=False, fallback_kind="claude") == {
        "mode": "acceptEdits"}
    # And with no override, the fallback kind's default is used.
    sess2 = {"provider_id": "p1", "permission": {}}
    assert permission.resolve_for_run(
        sess_rec=sess2, worker_sess_rec=None, is_worker=False, fallback_kind="codex") == {
        "approval": "never", "sandbox": "danger-full-access"}


def test_resolve_for_run_no_provider_id_uses_fallback_kind():
    # No provider_id on the owner → no lookup → kind from fallback.
    sess = {"permission": {"mode": "plan"}}
    assert permission.resolve_for_run(
        sess_rec=sess, worker_sess_rec=None, is_worker=False, fallback_kind="claude") == {
        "mode": "plan"}
    sess2 = {"permission": {}}
    assert permission.resolve_for_run(
        sess_rec=sess2, worker_sess_rec=None, is_worker=False, fallback_kind="qwen") == {
        "mode": "yolo"}


def test_resolve_for_run_non_dict_owner_uses_fallback_kind(monkeypatch):
    # Non-dict owner → no provider_id → fallback kind; empty owner permission → default.
    assert permission.resolve_for_run(
        sess_rec=None, worker_sess_rec=None, is_worker=False, fallback_kind="openai") == {
        "mode": "bypassPermissions"}
    assert permission.resolve_for_run(
        sess_rec=None, worker_sess_rec=None, is_worker=True, fallback_kind="claude") == {
        "mode": "bypassPermissions"}
