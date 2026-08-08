#!/usr/bin/env python3
"""Unit coverage for runtime_skills.py — pure-logic surface, discovery cache,
filesystem discovery, fingerprinting, and materialization. Independent of the
script-style test_runtime_skills.py (whose `t_*` helpers run only via main())."""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

import _test_home  # noqa: E402  (sets BETTER_AGENT_HOME + deletion guards)

_TEST_HOME = _test_home.isolate("bc-test-runtime-skills-unit-")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import runtime_skills  # noqa: E402


@pytest.fixture(autouse=True)
def _enable_integrations(monkeypatch):
    """runtime_skills gates on integrations_enabled(); enable it and start each
    test with a cold discovery cache so fs fixtures never bleed across tests."""
    monkeypatch.setattr(
        runtime_skills.installation_profile, "integrations_enabled", lambda: True
    )
    runtime_skills._DISCOVERY_CACHE.clear()
    yield
    runtime_skills._DISCOVERY_CACHE.clear()


def _write_skill(skill_dir: Path, name: str, description: str = "desc") -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n"
        f"# {name}\n",
        encoding="utf-8",
    )
    return skill_md


def _isolate_discovery(monkeypatch, roots):
    """Point _skill_roots at controlled dirs and stub extension skills out."""
    monkeypatch.setattr(runtime_skills, "_skill_roots", lambda _cwd: list(roots))
    monkeypatch.setattr(runtime_skills, "_extension_runtime_skills", lambda: [])


# --------------------------------------------------------------------------- #
# _filter_disabled
# --------------------------------------------------------------------------- #
def test_filter_disabled_none_returns_all():
    skills = [{"name": "a"}, {"name": "b"}]
    assert runtime_skills._filter_disabled(skills, None) == skills
    assert runtime_skills._filter_disabled(skills, []) == skills


def test_filter_disabled_wildcard_drops_everything():
    assert runtime_skills._filter_disabled([{"name": "a"}], ["*"]) == []


def test_filter_disabled_named_set_filters_matches():
    skills = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    assert runtime_skills._filter_disabled(skills, ["a", "c"]) == [{"name": "b"}]


def test_filter_disabled_strips_whitespace_and_drops_falsy():
    skills = [{"name": "a"}, {"name": "b"}]
    # " a " stripped -> "a"; "" and "   " dropped; None dropped.
    assert runtime_skills._filter_disabled(skills, [" a ", "", "   ", None]) == [
        {"name": "b"}
    ]


# --------------------------------------------------------------------------- #
# runtime_skill_projection / runtime_skill_contexts / has_runtime_skills
# --------------------------------------------------------------------------- #
def test_projection_bare_config_returns_empty():
    assert runtime_skills.runtime_skill_projection(_TEST_HOME, bare_config=True) == (
        [],
        [],
    )


def test_projection_integrations_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(
        runtime_skills.installation_profile, "integrations_enabled", lambda: False
    )
    assert runtime_skills.runtime_skill_projection(_TEST_HOME) == ([], [])


def test_projection_no_skills_returns_empty(monkeypatch, tmp_path):
    _isolate_discovery(monkeypatch, [tmp_path])
    assert runtime_skills.runtime_skill_projection(str(tmp_path)) == ([], [])


def test_contexts_returns_projection_contexts(monkeypatch, tmp_path):
    _write_skill(tmp_path / "alpha", "alpha", "Alpha skill.")
    _isolate_discovery(monkeypatch, [tmp_path])
    contexts = runtime_skills.runtime_skill_contexts(str(tmp_path))
    assert [c["name"] for c in contexts] == ["Runtime Skills"]
    assert "alpha" in contexts[0]["content"]


def test_contexts_disabled_skill_absent(monkeypatch, tmp_path):
    _write_skill(tmp_path / "alpha", "alpha", "Alpha skill.")
    _write_skill(tmp_path / "beta", "beta", "Beta skill.")
    _isolate_discovery(monkeypatch, [tmp_path])
    content = runtime_skills.runtime_skill_contexts(
        str(tmp_path), disabled=["alpha"]
    )[0]["content"]
    assert "beta" in content and "alpha" not in content


def test_has_runtime_skills_truthy_and_falsy(monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    full = tmp_path / "full"
    full.mkdir()
    _write_skill(full / "alpha", "alpha")

    _isolate_discovery(monkeypatch, [empty])
    assert runtime_skills.has_runtime_skills(str(empty)) is False

    _isolate_discovery(monkeypatch, [full])
    assert runtime_skills.has_runtime_skills(str(full)) is True


def test_has_runtime_skills_bare_config_false(monkeypatch, tmp_path):
    _write_skill(tmp_path / "alpha", "alpha")
    _isolate_discovery(monkeypatch, [tmp_path])
    assert runtime_skills.has_runtime_skills(str(tmp_path), bare_config=True) is False


def test_projection_specializes_machine_template(monkeypatch, tmp_path):
    skill_md = _write_skill(
        tmp_path / "machine", "machine", "Operate on {{better_agent.machine_id}}."
    )
    monkeypatch.setattr(runtime_skills, "_skill_roots", lambda _cwd: [tmp_path])
    monkeypatch.setattr(
        runtime_skills,
        "_extension_runtime_skills",
        lambda: [
            {
                "name": "machine",
                "dir": str(skill_md.parent),
                "path": str(skill_md),
                "template_variables": ["machine_id"],
            }
        ],
    )
    content = runtime_skills.runtime_skill_contexts(
        str(tmp_path), machine_id="primary"
    )[0]["content"]
    assert "primary" in content
    assert "{{better_agent.machine_id}}" not in content


# --------------------------------------------------------------------------- #
# _frontmatter / _read_description / read_skill_description
# --------------------------------------------------------------------------- #
def test_frontmatter_empty_or_unmarked_returns_none():
    assert runtime_skills._frontmatter("") is None
    assert runtime_skills._frontmatter("no frontmatter here") is None
    assert runtime_skills._frontmatter("-- not triple\nx: 1\n---\n") is None


def test_frontmatter_parses_simple_and_quoted_values():
    fm = runtime_skills._frontmatter('---\nname: alpha\ndescription: "hi there"\n---\n')
    assert fm == {"name": "alpha", "description": "hi there"}


def test_frontmatter_continuation_lines_append_to_current_key():
    text = (
        "---\n"
        "description: line one\n"
        "  line two\n"
        "  line three\n"
        "---\n"
    )
    assert runtime_skills._frontmatter(text) == {
        "description": "line one line two line three"
    }


def test_frontmatter_line_without_colon_resets_current_key():
    # A bare line (no colon) inside the block resets current_key so a later
    # indented line is not mis-attributed; the block still closes at ---.
    text = "---\nname: alpha\nbogus no colon line\n  orphan\nfoo: bar\n---\n"
    fm = runtime_skills._frontmatter(text)
    assert fm["name"] == "alpha"
    assert fm["foo"] == "bar"
    assert "orphan" not in fm.get("name", "")


def test_frontmatter_unclosed_block_returns_none():
    assert runtime_skills._frontmatter("---\nname: alpha\n") is None


def test_frontmatter_blank_continuation_line_skipped():
    # An indented line that is only whitespace has an empty continuation; it is
    # skipped (no attribute appended) but still consumes the line.
    text = (
        "---\n"
        "description: keep\n"
        "   \n"            # blank indented continuation
        "real: value\n"
        "---\n"
    )
    fm = runtime_skills._frontmatter(text)
    assert fm == {"description": "keep", "real": "value"}


def test_read_description_missing_file_returns_empty():
    assert runtime_skills._read_description(Path("/nonexistent/skill.md")) == ""


def test_read_description_no_frontmatter_or_no_key(tmp_path):
    md = tmp_path / "SKILL.md"
    md.write_text("# just a body, no frontmatter\n", encoding="utf-8")
    assert runtime_skills._read_description(md) == ""
    md.write_text("---\nname: x\n---\nbody\n", encoding="utf-8")
    assert runtime_skills._read_description(md) == ""


def test_read_skill_description_reads_frontmatter(tmp_path):
    md = _write_skill(tmp_path / "s", "s", "Real description.")
    assert runtime_skills.read_skill_description(md) == "Real description."


# --------------------------------------------------------------------------- #
# _display_skill_path
# --------------------------------------------------------------------------- #
def test_display_skill_path_with_root_and_name():
    skill = {"name": "alpha", "path": "/abs/SKILL.md"}
    assert (
        runtime_skills._display_skill_path(skill, "/run/home")
        == "/run/home/alpha/SKILL.md"
    )


def test_display_skill_path_falls_back_to_path():
    skill = {"name": "alpha", "path": "/abs/SKILL.md"}
    assert runtime_skills._display_skill_path(skill, "") == "/abs/SKILL.md"
    # Trailing slash on root is stripped; empty name falls back too.
    assert (
        runtime_skills._display_skill_path({"name": "", "path": "/abs/SKILL.md"}, "/r/")
        == "/abs/SKILL.md"
    )


# --------------------------------------------------------------------------- #
# fingerprints
# --------------------------------------------------------------------------- #
def test_file_and_path_fingerprint_existing_and_missing(tmp_path):
    f = tmp_path / "f"
    f.write_text("x", encoding="utf-8")
    existing = runtime_skills._file_fingerprint(f)
    assert existing == (f.stat().st_mtime_ns, f.stat().st_size)
    assert runtime_skills._file_fingerprint(tmp_path / "missing") == (0, 0)
    # _path_fingerprint shares the same shape.
    assert runtime_skills._path_fingerprint(f) == existing
    assert runtime_skills._path_fingerprint(tmp_path / "missing") == (0, 0)


def test_directory_fingerprint_missing_root_returns_zeros(tmp_path):
    assert runtime_skills._directory_fingerprint(tmp_path / "absent") == (0, 0, 0)


def test_directory_fingerprint_walks_entries(tmp_path):
    root = tmp_path / "skills"
    _write_skill(root / "alpha", "alpha")
    (root / "no-skill-md").mkdir()
    fp = runtime_skills._directory_fingerprint(root)
    assert fp[0] == root.stat().st_mtime_ns
    assert fp[1] >= fp[0]
    assert fp[2] > 1


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_directory_fingerprint_broken_entry_stats_swallowed(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root / "alpha", "alpha")
    # A broken symlink: iterdir yields it, .stat() (follows link) raises OSError.
    (root / "dangling").symlink_to(root / "does-not-exist")
    fp = runtime_skills._directory_fingerprint(root)
    assert fp[2] > 1  # did not abort the walk


class _BrokenIterdirDir:
    """Path-like root whose .stat() succeeds but .iterdir() raises OSError,
    exercising the defensive `except OSError` around the walk loop."""

    def __init__(self, real_dir: Path) -> None:
        self._real = real_dir

    def stat(self):
        return self._real.stat()

    def iterdir(self):
        raise OSError("iterdir denied")


def test_directory_fingerprint_iterdir_oserror_swallowed(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    fp = runtime_skills._directory_fingerprint(_BrokenIterdirDir(real))
    assert fp == (real.stat().st_mtime_ns, real.stat().st_mtime_ns, 1)


# --------------------------------------------------------------------------- #
# extension fingerprint / extension runtime skills — exception branches
# --------------------------------------------------------------------------- #
def _inject_extension_module(monkeypatch, store_fp=None, settings_fp=None,
                             entries=None, store_exc=None, settings_exc=None,
                             entries_exc=None):
    fake = types.ModuleType("extension_store")
    if store_exc is not None:
        fake.store_fingerprint = mock_raiser(store_exc)
    else:
        fake.store_fingerprint = lambda: store_fp
    if settings_exc is not None:
        fake.extension_settings_fingerprint = mock_raiser(settings_exc)
    else:
        fake.extension_settings_fingerprint = lambda: settings_fp
    if entries_exc is not None:
        fake.runtime_skill_entries = mock_raiser(entries_exc)
    else:
        fake.runtime_skill_entries = lambda: entries if entries is not None else []
    monkeypatch.setitem(sys.modules, "extension_store", fake)
    return fake


def mock_raiser(exc):
    def _raise(*_a, **_k):
        raise exc
    return _raise


def test_extension_fingerprint_unimportable_returns_empty(monkeypatch):
    # Force `import extension_store` to fail by poisoning sys.modules with None.
    monkeypatch.setitem(sys.modules, "extension_store", None)
    assert runtime_skills._extension_runtime_skills_fingerprint() == ()


def test_extension_fingerprint_store_and_settings_exceptions(monkeypatch):
    _inject_extension_module(monkeypatch, store_exc=RuntimeError("s"))
    # store_fingerprint raised -> store_fp=()
    assert runtime_skills._extension_runtime_skills_fingerprint()[0] == ()
    _inject_extension_module(
        monkeypatch, store_fp=("s",), settings_exc=RuntimeError("set")
    )
    assert runtime_skills._extension_runtime_skills_fingerprint() == (("s",), ())


def test_extension_fingerprint_happy(monkeypatch):
    _inject_extension_module(monkeypatch, store_fp=("s",), settings_fp=("g",))
    assert runtime_skills._extension_runtime_skills_fingerprint() == (("s",), ("g",))


def test_extension_runtime_skills_unimportable(monkeypatch):
    monkeypatch.setitem(sys.modules, "extension_store", None)
    assert runtime_skills._extension_runtime_skills() == []


def test_extension_runtime_skills_entries_exception(monkeypatch):
    _inject_extension_module(monkeypatch, entries_exc=RuntimeError("boom"))
    assert runtime_skills._extension_runtime_skills() == []


def test_extension_runtime_skills_happy(monkeypatch):
    entries = [{"name": "x", "dir": "/d", "path": "/d/SKILL.md"}]
    _inject_extension_module(monkeypatch, entries=entries)
    assert runtime_skills._extension_runtime_skills() == entries


# --------------------------------------------------------------------------- #
# _skill_roots / _trusted_project_root / _cwd_chain
# --------------------------------------------------------------------------- #
def test_trusted_project_root_empty_and_missing():
    assert runtime_skills._trusted_project_root("") is None
    assert runtime_skills._trusted_project_root("/no/such/dir/anywhere") is None


def test_skill_roots_empty_cwd_is_just_home():
    roots = runtime_skills._skill_roots("")
    assert roots == [Path.home() / ".agents" / "skills"]


def test_skill_roots_includes_project_chain(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    sub = project / "deep" / "nested"
    sub.mkdir(parents=True)
    roots = runtime_skills._skill_roots(str(sub))
    # Home root + every ancestor from the project root up to (not past) home.
    assert roots[0] == Path.home() / ".agents" / "skills"
    assert (project / ".agents" / "skills") in roots
    assert (sub / ".agents" / "skills") in roots


def test_cwd_chain_terminates_at_home():
    home = Path.home().resolve()
    chain = runtime_skills._cwd_chain(home)
    assert chain == [home]


# --------------------------------------------------------------------------- #
# _discover_skills — filesystem + extension paths
# --------------------------------------------------------------------------- #
def test_discover_filesystem_sorted_and_filters(monkeypatch, tmp_path):
    root = tmp_path / "skills"
    _write_skill(root / "bravo", "bravo")
    _write_skill(root / "alpha", "alpha")
    (root / ".hidden").mkdir()  # dot-dir skipped
    (root / "not-a-dir").write_text("x", encoding="utf-8")  # non-dir skipped
    (root / "empty").mkdir()  # dir without SKILL.md skipped
    _isolate_discovery(monkeypatch, [root])
    names = [s["name"] for s in runtime_skills._discover_skills(str(tmp_path))]
    assert names == ["alpha", "bravo"]


def test_discover_extension_path_filters(monkeypatch, tmp_path):
    good = _write_skill(tmp_path / "good", "good")
    monkeypatch.setattr(runtime_skills, "_skill_roots", lambda _cwd: [])
    monkeypatch.setattr(
        runtime_skills,
        "_extension_runtime_skills",
        lambda: [
            {"name": "good", "dir": str(good.parent), "path": str(good),
             "template_variables": []},
            {"name": "", "dir": "/x", "path": "/x/SKILL.md"},  # empty name skipped
            {"name": "good", "dir": "/x", "path": "/x/SKILL.md"},  # dup skipped
            {"name": "nofile", "dir": "/x", "path": "/x/missing.md"},  # not a file
        ],
    )
    found = runtime_skills._discover_skills(str(tmp_path))
    assert [s["name"] for s in found] == ["good"]


def test_discover_extension_max_skills_cap(monkeypatch, tmp_path):
    """Extension loop returns early once MAX_SKILLS is reached (line 160 path)."""
    entries = []
    for i in range(runtime_skills.MAX_SKILLS + 5):
        md = _write_skill(tmp_path / f"s{i}", f"s{i}")
        entries.append(
            {"name": f"s{i}", "dir": str(md.parent), "path": str(md),
             "template_variables": []}
        )
    monkeypatch.setattr(runtime_skills, "_skill_roots", lambda _cwd: [])
    monkeypatch.setattr(
        runtime_skills, "_extension_runtime_skills", lambda: entries
    )
    found = runtime_skills._discover_skills(str(tmp_path))
    assert len(found) == runtime_skills.MAX_SKILLS


def test_discover_filesystem_max_skills_cap(monkeypatch, tmp_path):
    root = tmp_path / "skills"
    for i in range(runtime_skills.MAX_SKILLS + 5):
        _write_skill(root / f"s{i:03d}", f"s{i}")
    _isolate_discovery(monkeypatch, [root])
    found = runtime_skills._discover_skills(str(tmp_path))
    assert len(found) == runtime_skills.MAX_SKILLS


def test_discover_cache_hit_skips_rediscovery(monkeypatch, tmp_path):
    root = tmp_path / "skills"
    _write_skill(root / "alpha", "alpha")
    calls = {"n": 0}

    def counting_extension():
        calls["n"] += 1
        return []

    monkeypatch.setattr(runtime_skills, "_skill_roots", lambda _cwd: [root])
    monkeypatch.setattr(runtime_skills, "_extension_runtime_skills", counting_extension)
    first = runtime_skills._discover_skills(str(tmp_path))
    second = runtime_skills._discover_skills(str(tmp_path))
    assert first == second
    # The extension resolver runs once (cache hit serves the second call).
    assert calls["n"] == 1


def test_discover_cache_fingerprint_mismatch_redisovers(monkeypatch, tmp_path):
    root = tmp_path / "skills"
    _write_skill(root / "alpha", "alpha")
    _isolate_discovery(monkeypatch, [root])
    runtime_skills._discover_skills(str(tmp_path))  # warm cache
    # Poison the cached fingerprint so the hit branch falls through.
    key = next(iter(runtime_skills._DISCOVERY_CACHE))
    real_skills = runtime_skills._DISCOVERY_CACHE[key][1]
    runtime_skills._DISCOVERY_CACHE[key] = (("bogus",), real_skills)
    found = runtime_skills._discover_skills(str(tmp_path))
    assert [s["name"] for s in found] == ["alpha"]


def test_cache_lru_evicts_oldest_when_over_max():
    runtime_skills._DISCOVERY_CACHE.clear()
    for i in range(runtime_skills._DISCOVERY_CACHE_MAX + 5):
        runtime_skills._cache_discovered_skills((f"key{i}",), [])
    assert len(runtime_skills._DISCOVERY_CACHE) == runtime_skills._DISCOVERY_CACHE_MAX
    assert ("key0",) not in runtime_skills._DISCOVERY_CACHE
    assert ("key0" + str(runtime_skills._DISCOVERY_CACHE_MAX + 4),) not in (
        runtime_skills._DISCOVERY_CACHE
    )
    # The most-recent key survives.
    last_key = (
        f"key{runtime_skills._DISCOVERY_CACHE_MAX + 4}",
    )
    assert last_key in runtime_skills._DISCOVERY_CACHE


# --------------------------------------------------------------------------- #
# materialize_runtime_skills
# --------------------------------------------------------------------------- #
def test_materialize_bare_config_returns_zero(monkeypatch, tmp_path):
    _isolate_discovery(monkeypatch, [tmp_path])
    out = tmp_path / "out"
    assert runtime_skills.materialize_runtime_skills(out, str(tmp_path), bare_config=True) == 0
    assert not out.exists()


def test_materialize_copies_and_specializes(monkeypatch, tmp_path):
    root = tmp_path / "skills"
    _write_skill(root / "alpha", "alpha", "Alpha body.")
    _isolate_discovery(monkeypatch, [root])
    out = tmp_path / "out"
    count = runtime_skills.materialize_runtime_skills(out, str(tmp_path))
    assert count == 1
    assert (out / "alpha" / "SKILL.md").is_file()


def test_materialize_skips_existing_target(monkeypatch, tmp_path):
    root = tmp_path / "skills"
    _write_skill(root / "alpha", "alpha")
    _isolate_discovery(monkeypatch, [root])
    out = tmp_path / "out"
    (out / "alpha").mkdir(parents=True)
    assert runtime_skills.materialize_runtime_skills(out, str(tmp_path)) == 0


def test_materialize_cleans_up_on_copy_failure(monkeypatch, tmp_path):
    root = tmp_path / "skills"
    _write_skill(root / "alpha", "alpha")
    _isolate_discovery(monkeypatch, [root])
    out = tmp_path / "out"

    def boom(*_a, **_k):
        raise OSError("copy denied")

    monkeypatch.setattr(runtime_skills.shutil, "copytree", boom)
    with pytest.raises(OSError):
        runtime_skills.materialize_runtime_skills(out, str(tmp_path))
    # Partial target dir must have been removed by the except branch.
    assert not (out / "alpha").exists()


def test_materialize_machine_id_template_validates(monkeypatch, tmp_path):
    """Extension skill with the machine_id template variable triggers the
    local-machine-id guard (import + call) before copytree."""
    md = _write_skill(
        tmp_path / "machine", "machine", "Run on {{better_agent.machine_id}}."
    )
    monkeypatch.setattr(runtime_skills, "_skill_roots", lambda _cwd: [])
    monkeypatch.setattr(
        runtime_skills,
        "_extension_runtime_skills",
        lambda: [
            {
                "name": "machine",
                "dir": str(md.parent),
                "path": str(md),
                "template_variables": ["machine_id"],
            }
        ],
    )
    # Inject a no-op identity guard so the template path is exercised without a
    # real node-id check; restore the real module afterward.
    fake = types.ModuleType("local_machine_identity")
    fake.require_matching_local_machine_id = lambda _mid: None
    monkeypatch.setitem(sys.modules, "local_machine_identity", fake)
    out = tmp_path / "out"
    count = runtime_skills.materialize_runtime_skills(
        out, str(tmp_path), machine_id="primary"
    )
    assert count == 1
    assert "primary" in (out / "machine" / "SKILL.md").read_text(encoding="utf-8")
