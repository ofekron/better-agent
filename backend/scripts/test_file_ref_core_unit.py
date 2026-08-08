#!/usr/bin/env python3
"""Dedicated unit coverage for the CORE (non-migration) surface of
backend/file_ref_resolver.py (lines ~76-434): the existence / cwd caches,
the extension tag-rule registry + style sentinels, the session-name tag,
marker detection, the worker-node "assume exists" policy, the bcfile link
builder, `rewrite_text`, and the event-shape-aware rewriters
(`_rewrite_content_blocks` / `rewrite_event_data`).

This is pure logic over a few process-global singletons
(`_cache`, `_cwd_path_cache`, `_tag_rules`, `_tag_scan_re`); the autouse
fixture below resets them between tests so order never matters. Real file
existence is exercised against pytest's `tmp_path`, never real state.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import file_ref_resolver as frr  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_resolver_state():
    """Reset the resolver's process-global singletons between tests so no
    fixture state (tag rules, caches, cache capacity) bleeds across."""
    frr.set_tag_rules([])
    saved_cache = frr._cache
    saved_cwd_cache = frr._cwd_path_cache
    frr._cache = frr._ExistsCache()
    frr._cwd_path_cache = frr._CwdPathCache()
    try:
        yield
    finally:
        frr._cache = saved_cache
        frr._cwd_path_cache = saved_cwd_cache
        frr.set_tag_rules([])


# ─── _ExistsCache ────────────────────────────────────────────────────────


def test_exists_cache_caches_and_invalidates(tmp_path):
    target = tmp_path / "real.py"
    target.write_text("x")
    c = frr._ExistsCache()
    assert c.exists(str(target)) is True
    # Second hit is served from the cache (no re-stat needed).
    assert c.exists(str(target)) is True
    c.invalidate_path(str(target))
    assert str(target) not in c._d


def test_exists_cache_missing_is_false_and_cached(tmp_path):
    missing = str(tmp_path / "nope.py")
    c = frr._ExistsCache()
    assert c.exists(missing) is False
    assert c._d[missing] is False
    # Cached False is returned without re-stat.
    assert c.exists(missing) is False


def test_exists_cache_oserror_treated_as_missing(monkeypatch):
    c = frr._ExistsCache()

    def boom(_path):
        raise OSError("denied")

    monkeypatch.setattr(frr.os.path, "isfile", boom)
    assert c.exists("/totally/fine.py") is False
    # The OSError branch still records the False verdict.
    assert c._d["/totally/fine.py"] is False


def test_exists_cache_evicts_oldest_quarter_when_full():
    c = frr._ExistsCache()
    c._MAX_ENTRIES = 4  # eviction drops oldest 4 // 4 == 1 entry
    for i in range(4):
        c._d[f"old{i}"] = True
    assert len(c._d) == 4
    c.exists("trigger.py")  # len >= cap → drop oldest 1, then insert
    assert "old0" not in c._d
    assert "old1" in c._d
    assert "trigger.py" in c._d
    assert len(c._d) == 4


# ─── _CwdPathCache ───────────────────────────────────────────────────────


def test_cwd_path_cache_resolves_and_caches(tmp_path):
    c = frr._CwdPathCache()
    resolved = c.resolve(str(tmp_path))
    assert resolved == tmp_path.resolve()
    # Served from cache on the second call.
    assert c._d[str(tmp_path)] == resolved
    assert c.resolve(str(tmp_path)) == resolved


def test_cwd_path_cache_evicts_oldest_quarter_when_full(tmp_path):
    c = frr._CwdPathCache()
    c._MAX_ENTRIES = 4
    for i in range(4):
        c._d[f"old{i}"] = Path(f"old{i}")
    c.resolve(str(tmp_path))  # len >= cap → drop oldest 1, then insert
    assert "old0" not in c._d
    assert str(tmp_path) in c._d
    assert len(c._d) == 4


# ─── _style_attrs ────────────────────────────────────────────────────────


def test_style_attrs_empty_rule_is_empty_string():
    assert frr._style_attrs({}) == ""


def test_style_attrs_bold_and_scale():
    rule = {"bold": True, "font_scale": 2}
    out = frr._style_attrs(rule)
    assert "b=1" in out
    assert "s=2" in out


def test_style_attrs_scale_omitted_when_one_or_falsy():
    assert "s=" not in frr._style_attrs({"font_scale": 1})
    assert "s=" not in frr._style_attrs({"font_scale": 0})
    assert "s=" not in frr._style_attrs({"font_scale": None})


def test_style_attrs_highlight_color_and_alpha():
    rule = {"highlight": {"color": "#ff8c00", "alpha": 0.18}}
    out = frr._style_attrs(rule)
    assert "bg=#ff8c00" in out
    assert "a=0.18" in out


def test_style_attrs_highlight_partial_and_invalid():
    # color present, no alpha
    assert "bg=#fff" in frr._style_attrs({"highlight": {"color": "#fff"}})
    assert "a=" not in frr._style_attrs({"highlight": {"color": "#fff"}})
    # empty / non-string color omitted
    assert "bg=" not in frr._style_attrs({"highlight": {"color": ""}})
    assert "bg=" not in frr._style_attrs({"highlight": {"color": None}})
    # alpha without color still emitted
    assert "a=0.5" in frr._style_attrs({"highlight": {"alpha": 0.5}})
    # non-numeric alpha omitted
    assert "a=" not in frr._style_attrs({"highlight": {"alpha": "0.5"}})


def test_style_attrs_combines_all_three():
    out = frr._style_attrs(
        {"bold": True, "font_scale": 1.3, "highlight": {"color": "#000", "alpha": 0.1}})
    assert out == "b=1;s=1.3;bg=#000;a=0.1"


# ─── set_tag_rules / tag_names / _apply_tag_rules ────────────────────────


def test_set_tag_rules_empty_disables_scan():
    frr.set_tag_rules([])
    assert frr._tag_rules == {}
    assert frr._tag_scan_re is None
    assert frr.tag_names() == frozenset()


def test_set_tag_rules_skips_non_string_and_empty_tags():
    frr.set_tag_rules([
        {"tag": "NEEDS_USER_DECISION", "bold": True},
        {"tag": "", "bold": True},
        {"tag": None, "bold": True},
        {"tag": 123, "bold": True},
        {"other": "no-tag"},
    ])
    assert frr.tag_names() == frozenset({"NEEDS_USER_DECISION"})
    assert frr._tag_scan_re is not None


def test_apply_tag_rules_no_angle_bracket_fast_path():
    assert frr._apply_tag_rules("plain text no tag") == "plain text no tag"


def test_apply_tag_rules_angle_present_but_no_rules_only_strips_session_name():
    frr.set_tag_rules([])
    out = frr._apply_tag_rules("see <SESSION_NAME>my name</SESSION_NAME> body")
    assert "<SESSION_NAME>" not in out
    assert "my name" not in out  # session-name inner text is metadata, stripped
    assert "body" in out


def test_apply_tag_rules_strips_wrapper_and_applies_sentinel():
    frr.set_tag_rules([{"tag": "NEEDS_USER_DECISION", "bold": True}])
    out = frr._apply_tag_rules("pre <NEEDS_USER_DECISION> act now </NEEDS_USER_DECISION> post")
    assert "<NEEDS_USER_DECISION>" not in out
    assert frr._STYLE_SENTINEL_OPEN.format(attrs="b=1") in out
    assert frr._STYLE_SENTINEL_CLOSE in out
    assert "act now" in out  # inner stripped of surrounding whitespace


def test_apply_tag_rules_no_attrs_keeps_plain_inner():
    frr.set_tag_rules([{"tag": "NOTE"}])  # no styling attrs
    out = frr._apply_tag_rules("<NOTE> hello </NOTE>")
    assert "<NOTE>" not in out
    assert frr._STYLE_SENTINEL_OPEN not in out
    assert "hello" in out


def test_apply_tag_rules_strip_wrapper_false_keeps_tag_verbatim():
    frr.set_tag_rules([{"tag": "KEEP", "strip_wrapper": False}])
    out = frr._apply_tag_rules("x <KEEP>inner</KEEP> y")
    assert out == "x <KEEP>inner</KEEP> y"


# ─── session-name tag (extract / strip) ──────────────────────────────────


def test_extract_session_name_none_for_empty_or_missing_marker():
    assert frr.extract_session_name("") is None
    assert frr.extract_session_name("plain prose") is None


def test_extract_session_name_returns_inner():
    assert frr.extract_session_name("hi <SESSION_NAME>My Sess</SESSION_NAME>") == "My Sess"


def test_extract_session_name_marker_present_but_no_close_is_none():
    assert frr.extract_session_name("hi <SESSION_NAME>no close") is None


def test_extract_session_name_whitespace_only_is_none():
    assert frr.extract_session_name("<SESSION_NAME>   </SESSION_NAME>") is None


def test_strip_session_name_tag_no_marker_unchanged():
    assert frr.strip_session_name_tag("nothing here") == "nothing here"


def test_strip_session_name_tag_removes_tag():
    out = frr.strip_session_name_tag("a <SESSION_NAME>x</SESSION_NAME> b")
    assert out == "a  b"


# ─── detect_markers ──────────────────────────────────────────────────────


def test_detect_markers_no_rules_or_no_angle_bracket():
    frr.set_tag_rules([])
    assert frr.detect_markers("text <X>") == []
    frr.set_tag_rules([{"tag": "X", "marker": {"color": "red"}}])
    assert frr.detect_markers("text without the opening tag") == []


def test_detect_markers_emits_for_marker_rules_with_tag_in_text():
    frr.set_tag_rules([
        {"tag": "NEEDS_USER_DECISION", "marker": {"color": "orange"},
         "_extension_id": "user-attention"},
        {"tag": "SILENT", "marker": {"color": "gray"}},  # tag not in text
        {"tag": "NOMARK"},  # no marker key
    ])
    out = frr.detect_markers("see <NEEDS_USER_DECISION>do thing</NEEDS_USER_DECISION>")
    assert len(out) == 1
    ext_id, marker = out[0]
    assert ext_id == "user-attention"
    # The emitted marker is a fresh dict carrying the tag and the rule's attrs.
    assert marker["tag"] == "NEEDS_USER_DECISION"
    assert marker["color"] == "orange"
    # It must NOT be the shared rule["marker"] object (projection safety).
    rule_marker = next(r for r in frr._tag_rules.values()
                       if r["tag"] == "NEEDS_USER_DECISION")["marker"]
    assert marker is not rule_marker


def test_detect_markers_defaults_extension_id_to_empty():
    frr.set_tag_rules([{"tag": "X", "marker": {"k": 1}}])  # no _extension_id
    out = frr.detect_markers("<X>y</X>")
    assert out == [("", {"k": 1, "tag": "X"})]


# ─── invalidate_path / assume_exists_* ───────────────────────────────────


def test_invalidate_path_delegates_to_cache(tmp_path):
    target = tmp_path / "f.py"
    target.write_text("x")
    assert frr._cache.exists(str(target)) is True
    frr.invalidate_path(str(target))
    assert str(target) not in frr._cache._d


def test_assume_exists_for_session_primary_is_false():
    assert frr.assume_exists_for_session(None) is False
    assert frr.assume_exists_for_session({}) is False
    assert frr.assume_exists_for_session({"node_id": "primary"}) is False


def _install_fake_topology(monkeypatch, local_id):
    """Inject a fake `topology` module exposing `local_node_id()`; restored
    automatically by pytest's monkeypatch at test teardown."""
    mod = types.ModuleType("topology")
    mod.local_node_id = lambda: local_id
    monkeypatch.setitem(sys.modules, "topology", mod)


def test_assume_exists_for_node_non_primary_remote_node(monkeypatch):
    _install_fake_topology(monkeypatch, "primary")
    # Worker node id != local node id → its files live on the node.
    assert frr.assume_exists_for_node("worker-1") is True


def test_assume_exists_for_node_non_primary_self_node(monkeypatch):
    _install_fake_topology(monkeypatch, "worker-1")
    # node_id == local → files are local, do the real disk check.
    assert frr.assume_exists_for_node("worker-1") is False


def test_assume_exists_for_node_topology_import_failure_assumes_exists(monkeypatch):
    # A topology module whose `local_node_id` attr access raises makes the
    # deferred import fail → caller assumes files exist (fail-open for the
    # existence check, since the node may simply be unreachable).
    mod = types.ModuleType("topology")

    class _Raise:
        def __getattr__(self, _name):
            raise ImportError("simulated topology breakage")

    monkeypatch.setitem(sys.modules, "topology", _Raise())
    assert frr.assume_exists_for_node("worker-9") is True


# ─── _build_link ─────────────────────────────────────────────────────────


def test_build_link_with_and_without_lines():
    assert frr._build_link("a.py", "/p/a.py", None) == f"[a.py](bcfile:{quote('/p/a.py', safe='/:')})"
    assert frr._build_link("a.py", "/p/a.py", "10-20") == \
        f"[a.py](bcfile:{quote('/p/a.py', safe='/:')}?L=10-20)"


def test_build_link_escapes_bracket_in_label():
    assert frr._build_link("a]b.py", "/p/a.py", None) == \
        f"[a\\]b.py](bcfile:{quote('/p/a.py', safe='/:')})"


# ─── rewrite_text ────────────────────────────────────────────────────────


def test_rewrite_text_empty_or_non_string_returned_unchanged():
    assert frr.rewrite_text("", "/tmp") == ""
    assert frr.rewrite_text(None, "/tmp") is None  # type: ignore[arg-type]
    assert frr.rewrite_text(123, "/tmp") == 123  # type: ignore[arg-type]


def test_rewrite_text_no_dot_early_out():
    assert frr.rewrite_text("no extension here", "/tmp") == "no extension here"


def test_rewrite_text_missing_relative_with_cwd_left_verbatim(tmp_path):
    assert frr.rewrite_text("see missing.py", str(tmp_path)) == "see missing.py"


def _link(label: str, abs_path: str, lines: str | None = None) -> str:
    """Expected bcfile link string for a given label/abs_path/lines."""
    href = f"bcfile:{quote(abs_path, safe='/:')}"
    if lines:
        href = f"{href}?L={lines}"
    return f"[{label}]({href})"


def test_rewrite_text_existing_relative_becomes_link(tmp_path):
    (tmp_path / "real.py").write_text("x")
    out = frr.rewrite_text("see real.py", str(tmp_path))
    assert out == "see " + _link("real.py", str(tmp_path / "real.py"))


def test_rewrite_text_existing_with_lines(tmp_path):
    (tmp_path / "real.py").write_text("x")
    out = frr.rewrite_text("see real.py:42", str(tmp_path))
    assert out == "see " + _link("real.py:42", str(tmp_path / "real.py"), "42")


def test_rewrite_text_existing_range(tmp_path):
    (tmp_path / "real.py").write_text("x")
    out = frr.rewrite_text("see real.py:10-20", str(tmp_path))
    assert out == "see " + _link("real.py:10-20", str(tmp_path / "real.py"), "10-20")


def test_rewrite_text_disallowed_extension_left_verbatim(tmp_path):
    # .bin is not in the allow-list → token untouched even if it existed.
    (tmp_path / "weird.bin").write_text("x")
    assert frr.rewrite_text("see weird.bin", str(tmp_path)) == "see weird.bin"


def test_rewrite_text_relative_with_no_cwd_left_verbatim(tmp_path):
    (tmp_path / "real.py").write_text("x")
    # cwd=None → relative refs can't be resolved → left verbatim.
    assert frr.rewrite_text("see real.py", None) == "see real.py"


def test_rewrite_text_absolute_existing_becomes_link(tmp_path):
    target = tmp_path / "abs.py"
    target.write_text("x")
    abs_path = str(target)
    out = frr.rewrite_text(f"see {abs_path}", None)
    assert out == "see " + _link(abs_path, abs_path)


def test_rewrite_text_existing_backticked_path_strips_backticks(tmp_path):
    (tmp_path / "real.py").write_text("x")
    out = frr.rewrite_text("see `real.py`", str(tmp_path))
    assert out == "see " + _link("real.py", str(tmp_path / "real.py"))


def test_rewrite_text_backticked_existing_link_strips_backticks():
    # A `[label](bcfile:href)` already wrapped in backticks: backticks
    # suppress link rendering, so they are stripped and the bare link kept.
    inner = "[real.py](bcfile:/p/real.py)"
    out = frr.rewrite_text(f"see `{inner}` rest", "/anything")
    assert out == f"see {inner} rest"


def test_rewrite_text_already_rewritten_link_idempotent(tmp_path):
    (tmp_path / "real.py").write_text("x")
    once = frr.rewrite_text("see real.py", str(tmp_path))
    twice = frr.rewrite_text(once, str(tmp_path))
    assert once == twice


def test_rewrite_text_already_rewritten_backticked_link_idempotent(tmp_path):
    (tmp_path / "real.py").write_text("x")
    once = frr.rewrite_text("see `real.py`", str(tmp_path))
    # The result is a link wrapped in backticks-less form; re-running leaves
    # it unchanged.
    assert frr.rewrite_text(once, str(tmp_path)) == once


def test_rewrite_text_assume_exists_skips_disk_check():
    # assume_exists=True emits the link even though no file exists.
    out = frr.rewrite_text("see /nonexistent/ghost.py", None, assume_exists=True)
    assert out == "see " + _link("/nonexistent/ghost.py", "/nonexistent/ghost.py")


def test_rewrite_text_assume_exists_relative_uses_cwd():
    out = frr.rewrite_text("see ghost.py", "/proj", assume_exists=True)
    assert out == "see " + _link("ghost.py", "/proj/ghost.py")


# ─── _rewrite_content_blocks ─────────────────────────────────────────────


def test_rewrite_content_blocks_non_list_is_noop():
    frr._rewrite_content_blocks("not-a-list", "/tmp")  # must not raise
    frr._rewrite_content_blocks(None, "/tmp")


def test_rewrite_content_blocks_skips_non_dict_blocks(tmp_path):
    (tmp_path / "real.py").write_text("x")
    blocks = ["str-block", 42, None, {"type": "text", "text": "see real.py"}]
    frr._rewrite_content_blocks(blocks, str(tmp_path))
    # Non-dict entries untouched; the text block rewritten.
    assert blocks[0] == "str-block"
    assert _link("real.py", str(tmp_path / "real.py")) in blocks[3]["text"]


def test_rewrite_content_blocks_text_field_non_string_skipped():
    blocks = [{"type": "text", "text": 123}]
    frr._rewrite_content_blocks(blocks, "/tmp")
    assert blocks[0]["text"] == 123


def test_rewrite_content_blocks_text_block_applies_tag_rules(tmp_path):
    (tmp_path / "real.py").write_text("x")
    frr.set_tag_rules([{"tag": "NEEDS_USER_DECISION", "bold": True}])
    blocks = [{"type": "text",
               "text": "see real.py <NEEDS_USER_DECISION> now </NEEDS_USER_DECISION>"}]
    frr._rewrite_content_blocks(blocks, str(tmp_path))
    text = blocks[0]["text"]
    assert "real.py" in text and "bcfile:" in text
    assert "<NEEDS_USER_DECISION>" not in text
    assert frr._STYLE_SENTINEL_OPEN.format(attrs="b=1") in text


def test_rewrite_content_blocks_thinking_field(tmp_path):
    (tmp_path / "real.py").write_text("x")
    blocks = [{"type": "thinking", "thinking": "ponder real.py"}]
    frr._rewrite_content_blocks(blocks, str(tmp_path))
    assert _link("real.py", str(tmp_path / "real.py")) in blocks[0]["thinking"]


def test_rewrite_content_blocks_tool_result_string_content(tmp_path):
    (tmp_path / "real.py").write_text("x")
    blocks = [{"type": "tool_result", "content": "output real.py"}]
    frr._rewrite_content_blocks(blocks, str(tmp_path))
    assert _link("real.py", str(tmp_path / "real.py")) in blocks[0]["content"]


def test_rewrite_content_blocks_tool_result_list_content_recurses(tmp_path):
    (tmp_path / "real.py").write_text("x")
    blocks = [{"type": "tool_result", "content": [
        {"type": "text", "text": "see real.py"},
    ]}]
    frr._rewrite_content_blocks(blocks, str(tmp_path))
    assert _link("real.py", str(tmp_path / "real.py")) in blocks[0]["content"][0]["text"]


def test_rewrite_content_blocks_tool_result_non_str_non_list_content_skipped():
    blocks = [{"type": "tool_result", "content": 99}]
    frr._rewrite_content_blocks(blocks, "/tmp")
    assert blocks[0]["content"] == 99


# ─── rewrite_event_data ──────────────────────────────────────────────────


def test_rewrite_event_data_non_dict_returned_unchanged():
    assert frr.rewrite_event_data("text", ["not", "dict"], "/tmp") == ["not", "dict"]
    assert frr.rewrite_event_data("text", None, "/tmp") is None


def test_rewrite_event_data_manager_event_recurses_into_inner_data(tmp_path):
    (tmp_path / "real.py").write_text("x")
    data = {"event": {"type": "text", "data": {"text": "see real.py"}}}
    out = frr.rewrite_event_data("manager_event", data, str(tmp_path))
    assert _link("real.py", str(tmp_path / "real.py")) in out["event"]["data"]["text"]


def test_rewrite_event_data_manager_event_inner_not_dict_skipped():
    data = {"event": "not-a-dict"}
    out = frr.rewrite_event_data("manager_event", data, "/tmp")
    assert out is data
    assert out["event"] == "not-a-dict"


def test_rewrite_event_data_manager_event_no_inner_data_key():
    data = {"event": {"type": "text"}}  # no "data"
    out = frr.rewrite_event_data("manager_event", data, "/tmp")
    assert out is data


def test_rewrite_event_data_manager_event_inner_data_not_dict():
    data = {"event": {"type": "text", "data": "flat"}}
    out = frr.rewrite_event_data("manager_event", data, "/tmp")
    assert out["event"]["data"] == "flat"


def test_rewrite_event_data_agent_message_rewrites_content(tmp_path):
    (tmp_path / "real.py").write_text("x")
    data = {"message": {"content": [{"type": "text", "text": "see real.py"}]}}
    out = frr.rewrite_event_data("agent_message", data, str(tmp_path))
    assert _link("real.py", str(tmp_path / "real.py")) in out["message"]["content"][0]["text"]


def test_rewrite_event_data_agent_message_message_not_dict():
    data = {"message": "flat"}
    out = frr.rewrite_event_data("agent_message", data, "/tmp")
    assert out is data


def test_rewrite_event_data_legacy_text_output_thought_error_content(tmp_path):
    (tmp_path / "real.py").write_text("x")
    data = {"text": "a real.py", "output": "b real.py", "thought": "c real.py",
            "error": "d real.py", "content": "e real.py", "untouched": 5}
    out = frr.rewrite_event_data("legacy_frame", data, str(tmp_path))
    expected_link = _link("real.py", str(tmp_path / "real.py"))
    for key in ("text", "output", "thought", "error", "content"):
        assert expected_link in out[key], key
    assert out["untouched"] == 5


def test_rewrite_event_data_legacy_skips_non_string_values():
    data = {"text": 123, "output": ["x"], "content": None}
    out = frr.rewrite_event_data("legacy_frame", data, "/tmp")
    assert out["text"] == 123
    assert out["output"] == ["x"]
    assert out["content"] is None
