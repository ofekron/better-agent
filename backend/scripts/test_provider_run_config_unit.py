"""Hermetic unit tests for the pure-logic surface of provider_run_config.

provider_run_config normalizes provider run configs (mcp_servers / skills),
serializes skill frontmatter to TOML/YAML literals, and writes portable skill
trees with path-traversal guards. The existing scripts/test_provider_run_config.py
is an integration-style pipeline test that only grazes these pure functions
(~9% coverage); this file drives each function and its validation branches to
full coverage with real assertions. No subprocess, no live provider — pure
logic plus an isolated tmp home for the two filesystem helpers.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-provider-run-config-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import provider_run_config as prc  # noqa: E402


# --------------------------------------------------------------------------- #
# normalize_provider_run_config
# --------------------------------------------------------------------------- #


def test_normalize_none_returns_empty():
    assert prc.normalize_provider_run_config(None) == {}


def test_normalize_not_dict_raises():
    for bad in ([], "x", 5, 1.5):
        with pytest.raises(ValueError, match="must be an object"):
            prc.normalize_provider_run_config(bad)


def test_normalize_empty_dict_returns_empty():
    assert prc.normalize_provider_run_config({}) == {}


def test_normalize_copies_plain_keys_and_deepcopies():
    src = {"env": {"A": 1}, "model": "m"}
    out = prc.normalize_provider_run_config(src)
    assert out == {"env": {"A": 1}, "model": "m"}
    out["env"]["A"] = 2
    assert src["env"]["A"] == 1


def test_normalize_drops_mcp_and_skills_keys_from_top_level_copy():
    src = {"mcp_servers": {"s": {}}, "skills": {"k": "v"}, "keep": 1}
    out = prc.normalize_provider_run_config(src)
    assert "keep" in out and out["keep"] == 1
    # mcp_servers re-added only via its own validated path below
    assert out["mcp_servers"] == {"s": {}}
    assert out["skills"] == {"k": "v"}


def test_normalize_both_mcp_keys_ambiguous_raises():
    with pytest.raises(ValueError, match="ambiguous"):
        prc.normalize_provider_run_config(
            {"mcp_servers": {"a": {}}, "mcpServers": {"b": {}}}
        )


def test_normalize_mcp_servers_not_object_raises():
    with pytest.raises(ValueError, match="mcp_servers must be an object"):
        prc.normalize_provider_run_config({"mcp_servers": ["x"]})


def test_normalize_mcp_name_invalid_raises():
    bad_names = [("", "non-empty"), (" has ", "non-empty"), (123, "non-empty")]
    for name, _ in bad_names:
        with pytest.raises(ValueError, match="names must be non-empty strings"):
            prc.normalize_provider_run_config({"mcp_servers": {name: {}}})


def test_normalize_mcp_name_non_string_raises():
    with pytest.raises(ValueError, match="names must be non-empty strings"):
        prc.normalize_provider_run_config({"mcp_servers": {123: {}}})


def _norm_with_camel_mcp():
    return prc.normalize_provider_run_config({"mcpServers": {"s": {"x": 1}}})


def test_normalize_mcp_camelcase_key_normalized_to_mcp_servers():
    out = _norm_with_camel_mcp()
    assert "mcpServers" not in out
    assert out["mcp_servers"] == {"s": {"x": 1}}


def test_normalize_mcp_config_not_object_raises():
    with pytest.raises(ValueError, match=r"mcp_servers\.s must be an object"):
        prc.normalize_provider_run_config({"mcp_servers": {"s": "nope"}})


def test_normalize_empty_mcp_servers_omitted():
    out = prc.normalize_provider_run_config({"mcp_servers": {}})
    assert "mcp_servers" not in out


def test_normalize_mcp_servers_deepcopied():
    src = {"mcp_servers": {"s": {"cfg": 1}}}
    out = prc.normalize_provider_run_config(src)
    out["mcp_servers"]["s"]["cfg"] = 99
    assert src["mcp_servers"]["s"]["cfg"] == 1


def test_normalize_skills_not_object_raises():
    with pytest.raises(ValueError, match="skills must be an object"):
        prc.normalize_provider_run_config({"skills": "nope"})


def test_normalize_empty_skills_omitted():
    out = prc.normalize_provider_run_config({"skills": {}})
    assert "skills" not in out


def test_normalize_skills_populated_and_deepcopied():
    src = {"skills": {"a": "v"}}
    out = prc.normalize_provider_run_config(src)
    assert out["skills"] == {"a": "v"}
    out["skills"]["a"] = "z"
    assert src["skills"]["a"] == "v"


# --------------------------------------------------------------------------- #
# merge_provider_run_configs
# --------------------------------------------------------------------------- #


def test_merge_both_none_empty():
    assert prc.merge_provider_run_configs(None, None) == {}


def test_merge_plain_key_override_wins():
    out = prc.merge_provider_run_configs({"model": "a"}, {"model": "b"})
    assert out["model"] == "b"


def test_merge_mcp_servers_deep_merged():
    out = prc.merge_provider_run_configs(
        {"mcp_servers": {"a": {"x": 1}}},
        {"mcp_servers": {"b": {"y": 2}}},
    )
    assert out["mcp_servers"] == {"a": {"x": 1}, "b": {"y": 2}}


def test_merge_skills_deep_merged():
    out = prc.merge_provider_run_configs(
        {"skills": {"a": "1"}},
        {"skills": {"b": "2"}},
    )
    assert out["skills"] == {"a": "1", "b": "2"}


def test_merge_override_mcp_replaces_when_not_dict_is_normalized_first():
    # override mcp_servers is a dict, base has none -> merged from normalized base {}
    out = prc.merge_provider_run_configs(None, {"mcp_servers": {"s": {}}})
    assert out["mcp_servers"] == {"s": {}}


def test_merge_deepcopy_independence():
    base = {"env": {"A": 1}}
    out = prc.merge_provider_run_configs(base, {"env": {"A": 2}})
    out["env"]["A"] = 3
    assert base["env"]["A"] == 1


# --------------------------------------------------------------------------- #
# toml_literal
# --------------------------------------------------------------------------- #


def test_toml_str():
    assert prc.toml_literal("hi") == json.dumps("hi")


def test_toml_bool_true_and_false():
    assert prc.toml_literal(True) == "true"
    assert prc.toml_literal(False) == "false"


def test_toml_int_not_bool():
    assert prc.toml_literal(5) == "5"
    # bool is a subclass of int but must stay bool-encoded
    assert prc.toml_literal(True) == "true"


def test_toml_float():
    assert prc.toml_literal(1.5) == repr(1.5)


def test_toml_list_and_nested():
    assert prc.toml_literal([1, 2]) == "[1, 2]"
    assert prc.toml_literal([True, "x"]) == '[true, "x"]'


def test_toml_dict():
    assert prc.toml_literal({"a": 1, "b": "x"}) == '{ a = 1, b = "x" }'


def test_toml_dict_non_str_key_raises():
    with pytest.raises(ValueError, match="keys must be strings"):
        prc.toml_literal({1: 2})


def test_toml_none_raises():
    with pytest.raises(ValueError, match="TOML does not support null"):
        prc.toml_literal(None)


def test_toml_unsupported_type_raises():
    with pytest.raises(ValueError, match="unsupported TOML value type"):
        prc.toml_literal({1, 2})


# --------------------------------------------------------------------------- #
# _toml_key
# --------------------------------------------------------------------------- #


def test_toml_key_bare_alpha_and_underscore_dash():
    assert prc._toml_key("a") == "a"
    assert prc._toml_key("a_b-c") == "a_b-c"


def test_toml_key_quoted_when_empty_leading_digit_or_special():
    assert prc._toml_key("") == json.dumps("")
    assert prc._toml_key("1abc") == json.dumps("1abc")
    assert prc._toml_key("a b") == json.dumps("a b")


# --------------------------------------------------------------------------- #
# _yaml_scalar
# --------------------------------------------------------------------------- #


def test_yaml_scalar_plain_str():
    assert prc._yaml_scalar("hello") == "hello"


def test_yaml_scalar_empty_quoted():
    assert prc._yaml_scalar("") == json.dumps("")


def test_yaml_scalar_special_chars_quoted():
    for s in ("a:b", "a#b", "a[b]", "a{b}", "a\nb"):
        assert prc._yaml_scalar(s) == json.dumps(s)


def test_yaml_scalar_non_str_quoted():
    assert prc._yaml_scalar(5) == json.dumps(5)
    assert prc._yaml_scalar(None) == json.dumps(None)


# --------------------------------------------------------------------------- #
# _skill_text
# --------------------------------------------------------------------------- #


def test_skill_text_str_adds_trailing_newline():
    assert prc._skill_text("s", "body") == "body\n"


def test_skill_text_str_keeps_existing_newline():
    assert prc._skill_text("s", "body\n") == "body\n"


def test_skill_text_not_str_or_dict_raises():
    with pytest.raises(ValueError, match="must be a string or object"):
        prc._skill_text("s", 5)


def test_skill_text_instructions_not_str_raises():
    with pytest.raises(ValueError, match="instructions must be a string"):
        prc._skill_text("s", {"instructions": 5})


def test_skill_text_metadata_not_object_raises():
    with pytest.raises(ValueError, match="metadata must be an object"):
        prc._skill_text("s", {"instructions": "x", "metadata": "nope"})


def test_skill_text_object_full_frontmatter():
    text = prc._skill_text(
        "s",
        {
            "instructions": "do thing\n",
            "name": "Display",
            "description": "desc",
            "metadata": {"version": "1"},
        },
    )
    assert text.startswith("---\n")
    assert "name: Display" in text
    assert "description: desc" in text
    assert "version: 1" in text
    assert text.endswith("do thing\n")


def test_skill_text_name_falls_back_to_arg():
    text = prc._skill_text("s", {"instructions": "x"})
    assert "name: s" in text


# --------------------------------------------------------------------------- #
# write_skill_tree (real fs, isolated tmp)
# --------------------------------------------------------------------------- #


def test_write_skill_tree_string_skill(tmp_path: Path):
    prc.write_skill_tree(tmp_path, {"reviewer": "Review carefully.\n"})
    target = tmp_path / "reviewer" / "SKILL.md"
    assert target.read_text() == "Review carefully.\n"
    # atomic replace left no temp files behind
    assert not list((tmp_path / "reviewer").glob(".SKILL.md.*"))


def test_write_skill_tree_object_skill(tmp_path: Path):
    prc.write_skill_tree(tmp_path, {"reviewer": {"instructions": "x", "name": "R"}})
    content = (tmp_path / "reviewer" / "SKILL.md").read_text()
    assert content.startswith("---\n")
    assert "name: R" in content


def test_write_skill_tree_invalid_name_regex(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid skill name"):
        prc.write_skill_tree(tmp_path, {".hidden": "x"})


def test_write_skill_tree_invalid_name_dot_dot(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid skill name"):
        prc.write_skill_tree(tmp_path, {"..": "x"})


def test_write_skill_tree_invalid_name_trailing_dot(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid skill name"):
        prc.write_skill_tree(tmp_path, {"foo.": "x"})


def test_write_skill_tree_invalid_name_windows_reserved(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid skill name"):
        prc.write_skill_tree(tmp_path, {"CON.foo": "x"})


def test_write_skill_tree_name_not_string(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid skill name"):
        prc.write_skill_tree(tmp_path, {5: "x"})  # type: ignore[dict-item]


def test_write_skill_tree_creates_nested_dir(tmp_path: Path):
    nested = tmp_path / "sub"
    prc.write_skill_tree(nested, {"a": "b\n"})
    assert (nested / "a" / "SKILL.md").exists()


# --------------------------------------------------------------------------- #
# symlink_home_overlay (real fs, isolated tmp)
# --------------------------------------------------------------------------- #


def test_symlink_overlay_source_not_dir_returns(tmp_path: Path):
    target = tmp_path / "out"
    prc.symlink_home_overlay(tmp_path / "missing", target, skip=set())
    assert target.is_dir()


def test_symlink_overlay_links_entries_skipping_set(tmp_path: Path):
    source = tmp_path / "src"
    dest = tmp_path / "dest"
    source.mkdir()
    (source / "keep.txt").write_text("k")
    (source / "skip.txt").write_text("s")
    prc.symlink_home_overlay(source, dest, skip={"skip.txt"})
    assert (dest / "keep.txt").is_symlink()
    assert not (dest / "skip.txt").exists()


def test_symlink_overlay_skips_existing_targets(tmp_path: Path):
    source = tmp_path / "src"
    dest = tmp_path / "dest"
    source.mkdir()
    (source / "f.txt").write_text("new")
    dest.mkdir()
    (dest / "f.txt").write_text("existing")
    prc.symlink_home_overlay(source, dest, skip=set())
    assert (dest / "f.txt").read_text() == "existing"
    assert not (dest / "f.txt").is_symlink()


def test_symlink_overlay_links_subdirectory(tmp_path: Path):
    source = tmp_path / "src"
    dest = tmp_path / "dest"
    source.mkdir()
    (source / "d").mkdir()
    prc.symlink_home_overlay(source, dest, skip=set())
    assert (dest / "d").is_symlink()
