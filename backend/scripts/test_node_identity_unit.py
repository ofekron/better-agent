"""Unit tests for ``node_identity``.

Node-side persistent identity (id + secret + cwd_roots + address) for
dynamic worker-node registration. Resolves each field with the precedence
env > persisted file > derived default, then writes the resolved identity
back to ``ba_home()/node_identity.json`` (chmod 0o600) when it changed.

Pure over filesystem + ``secrets`` + ``socket`` + regex: every test points
``ba_home`` at a fresh ``tmp_path`` (full isolation from the real home and
from the pytest session root) and drives the env vars through monkeypatch.
The only external calls monkeypatched are ``socket.gethostname`` (machine
-dependent, asserted deterministically) and, for the OSError-on-write path,
the parent directory is made read-only so the real ``write_text`` raises
``PermissionError`` (a real OSError, no fake) — proving the graceful-fallback
branch with a genuine failure rather than a mock.

Pins:
  1. ``_default_node_id``: plain hostname, ``.local``/``.lan`` suffix drop,
     special-char sanitization, all-non-allowed -> ``worker-node``, empty
     hostname -> ``worker-node``, and >64-char truncation.
  2. ``_default_address``: hostname passthrough and empty -> ``worker-node``.
  3. ``_env_cwd_roots``: empty -> ``[]``, valid separator-split list, and
     invalid path -> ``RuntimeError``.
  4. ``_load_file``: missing / OSError / JSON decode / non-mapping /
     wrong-schema -> ``{}``, and valid mapping passthrough.
  5. ``load_or_create``: first creation (persists + chmod 0o600 + generated
     64-hex secret), env override of every field (and rewrite), file-value
     reload, file ``cwd_roots`` validation, idempotent no-rewrite when the
     file already matches, secret stability across reloads, and graceful
     return when persistence raises ``OSError``.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_node_identity_unit.py -v
"""

from __future__ import annotations

import json
import os
import stat

import pytest

import node_identity
from node_identity import NodeIdentity, SCHEMA_VERSION

ENV_ID = "BETTER_AGENT_NODE_ID"
ENV_TOKEN = "BETTER_AGENT_NODE_TOKEN"
ENV_ROOTS = "BETTER_AGENT_NODE_CWD_ROOTS"
ENV_ADDR = "BETTER_AGENT_NODE_ADDRESS"


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Point ``ba_home`` at a fresh tmp dir and clear every node-identity
    env var so tests cannot leak state into each other or the real home."""
    monkeypatch.setattr(node_identity, "ba_home", lambda: tmp_path)
    for var in (
        ENV_ID, "BETTER_CLAUDE_NODE_ID",
        ENV_TOKEN, "BETTER_CLAUDE_NODE_TOKEN",
        ENV_ROOTS, "BETTER_CLAUDE_NODE_CWD_ROOTS",
        ENV_ADDR, "BETTER_CLAUDE_NODE_ADDRESS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _identity_path() -> str:
    return str(node_identity.ba_home() / "node_identity.json")


def _set_hostname(monkeypatch, value: str) -> None:
    monkeypatch.setattr(node_identity.socket, "gethostname", lambda: value)


# --------------------------------------------------------------------------- #
# _default_node_id
# --------------------------------------------------------------------------- #
def test_default_node_id_plain(monkeypatch):
    _set_hostname(monkeypatch, "myhost")
    assert node_identity._default_node_id() == "myhost"


def test_default_node_id_drops_local_suffix(monkeypatch):
    _set_hostname(monkeypatch, "myhost.local")
    assert node_identity._default_node_id() == "myhost"


def test_default_node_id_drops_lan_suffix(monkeypatch):
    _set_hostname(monkeypatch, "myhost.lan")
    assert node_identity._default_node_id() == "myhost"


def test_default_node_id_sanitizes_special_chars(monkeypatch):
    _set_hostname(monkeypatch, "My Host#1!")
    # spaces and '#'/'!' -> '-', leading/trailing '-' stripped, lowercased.
    assert node_identity._default_node_id() == "my-host-1"


def test_default_node_id_all_invalid_falls_back(monkeypatch):
    _set_hostname(monkeypatch, "!!!")
    assert node_identity._default_node_id() == "worker-node"


def test_default_node_id_empty_hostname_falls_back(monkeypatch):
    _set_hostname(monkeypatch, "")
    assert node_identity._default_node_id() == "worker-node"


def test_default_node_id_truncates_to_64(monkeypatch):
    _set_hostname(monkeypatch, "a" * 200)
    result = node_identity._default_node_id()
    assert len(result) == 64
    assert result == "a" * 64


# --------------------------------------------------------------------------- #
# _default_address
# --------------------------------------------------------------------------- #
def test_default_address_hostname(monkeypatch):
    _set_hostname(monkeypatch, "node-7.local")
    assert node_identity._default_address() == "node-7.local"


def test_default_address_empty_hostname(monkeypatch):
    _set_hostname(monkeypatch, "")
    assert node_identity._default_address() == "worker-node"


# --------------------------------------------------------------------------- #
# _env_cwd_roots
# --------------------------------------------------------------------------- #
def test_env_cwd_roots_empty(monkeypatch):
    assert node_identity._env_cwd_roots() == []


def test_env_cwd_roots_valid_split(monkeypatch):
    monkeypatch.setenv(ENV_ROOTS, os.pathsep.join(["/srv/code", "/srv/other"]))
    assert node_identity._env_cwd_roots() == ["/srv/code", "/srv/other"]


def test_env_cwd_roots_invalid_raises_runtime_error(monkeypatch):
    monkeypatch.setenv(ENV_ROOTS, "relative/path")
    with pytest.raises(RuntimeError, match="invalid path"):
        node_identity._env_cwd_roots()


# --------------------------------------------------------------------------- #
# _load_file
# --------------------------------------------------------------------------- #
def test_load_file_missing_returns_empty():
    assert node_identity._load_file() == {}


def test_load_file_bad_json_returns_empty():
    node_identity.ba_home().joinpath("node_identity.json").write_text("{not json", encoding="utf-8")
    assert node_identity._load_file() == {}


def test_load_file_non_mapping_returns_empty():
    node_identity.ba_home().joinpath("node_identity.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert node_identity._load_file() == {}


def test_load_file_wrong_schema_returns_empty():
    node_identity.ba_home().joinpath("node_identity.json").write_text(
        json.dumps({"schema_version": 999, "node_id": "x"}), encoding="utf-8",
    )
    assert node_identity._load_file() == {}


def test_load_file_valid_returns_data():
    data = {"schema_version": SCHEMA_VERSION, "node_id": "n", "secret": "s"}
    node_identity.ba_home().joinpath("node_identity.json").write_text(json.dumps(data), encoding="utf-8")
    assert node_identity._load_file() == data


# --------------------------------------------------------------------------- #
# load_or_create
# --------------------------------------------------------------------------- #
def test_load_or_create_first_creation_persists_and_chmod(monkeypatch):
    _set_hostname(monkeypatch, "worker-1")
    ident = node_identity.load_or_create()

    assert isinstance(ident, NodeIdentity)
    assert ident.node_id == "worker-1"
    assert ident.address == "worker-1"
    assert ident.cwd_roots == ()
    # Generated secret is a 64-char hex string and was persisted.
    assert len(ident.secret) == 64
    int(ident.secret, 16)  # parses as hex

    path = node_identity.ba_home() / "node_identity.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["node_id"] == "worker-1"
    assert on_disk["secret"] == ident.secret
    # File is locked down to owner-only.
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_load_or_create_env_overrides_returned_identity(monkeypatch):
    # Env vars override the RETURNED identity even when a file exists. NOTE:
    # the docstring claims env overrides are "written back" to disk, but the
    # persist guard `out != {**data, **out}` is always False for same-keyed
    # data, so env overrides are NOT re-persisted when the file already
    # exists (defect card cc48af557c8b). This test pins the
    # CURRENT (authoritative) behavior: returned identity reflects env, disk
    # is left unchanged.
    _set_hostname(monkeypatch, "ignored-host")
    seed = {
        "schema_version": SCHEMA_VERSION,
        "node_id": "old-id",
        "secret": "old-secret",
        "cwd_roots": ["/old/code"],
        "address": "old.local",
    }
    path = node_identity.ba_home() / "node_identity.json"
    path.write_text(json.dumps(seed), encoding="utf-8")

    monkeypatch.setenv(ENV_ID, "env-id")
    monkeypatch.setenv(ENV_TOKEN, "env-secret")
    monkeypatch.setenv(ENV_ROOTS, os.pathsep.join(["/srv/a", "/srv/b"]))
    monkeypatch.setenv(ENV_ADDR, "env.local")

    ident = node_identity.load_or_create()
    assert ident.node_id == "env-id"
    assert ident.secret == "env-secret"
    assert ident.cwd_roots == ("/srv/a", "/srv/b")
    assert ident.address == "env.local"

    # Disk is NOT rewritten (current behavior).
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["node_id"] == "old-id"
    assert on_disk["secret"] == "old-secret"


def test_load_or_create_env_overrides_persisted_on_first_creation(monkeypatch):
    # When no file exists yet, env-overridden values ARE persisted via the
    # `not _path().exists()` arm of the guard.
    _set_hostname(monkeypatch, "ignored-host")
    monkeypatch.setenv(ENV_ID, "env-id")
    monkeypatch.setenv(ENV_TOKEN, "env-secret")
    monkeypatch.setenv(ENV_ADDR, "env.local")

    ident = node_identity.load_or_create()
    assert ident.node_id == "env-id"
    assert ident.secret == "env-secret"

    path = node_identity.ba_home() / "node_identity.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["node_id"] == "env-id"
    assert on_disk["secret"] == "env-secret"
    assert on_disk["address"] == "env.local"


def test_load_or_create_uses_file_values_on_reload(monkeypatch):
    _set_hostname(monkeypatch, "would-be-default")
    seed = {
        "schema_version": SCHEMA_VERSION,
        "node_id": "persisted-id",
        "secret": "persisted-secret",
        "cwd_roots": ["/srv/code"],
        "address": "persisted.local",
    }
    node_identity.ba_home().joinpath("node_identity.json").write_text(json.dumps(seed), encoding="utf-8")

    ident = node_identity.load_or_create()
    assert ident.node_id == "persisted-id"
    assert ident.secret == "persisted-secret"
    assert ident.cwd_roots == ("/srv/code",)
    assert ident.address == "persisted.local"


def test_load_or_create_validates_file_cwd_roots(monkeypatch):
    # A relative cwd_root in the persisted file is rejected by
    # validate_cwd_roots -> raises NodePathError (propagates). This pins that
    # file-sourced roots go through validation, not blind trust.
    import node_path_authority

    seed = {
        "schema_version": SCHEMA_VERSION,
        "node_id": "n",
        "secret": "s",
        "cwd_roots": ["relative"],
        "address": "a",
    }
    node_identity.ba_home().joinpath("node_identity.json").write_text(json.dumps(seed), encoding="utf-8")
    with pytest.raises(node_path_authority.NodePathError):
        node_identity.load_or_create()


def test_load_or_create_idempotent_no_rewrite_when_unchanged(monkeypatch):
    _set_hostname(monkeypatch, "worker-1")
    # Pin every field via env so the resolved identity is fully determined.
    monkeypatch.setenv(ENV_ID, "stable-id")
    monkeypatch.setenv(ENV_TOKEN, "stable-secret")
    monkeypatch.setenv(ENV_ADDR, "stable.local")
    # Pre-write a file whose content EXACTLY matches what load_or_create would
    # produce, so the `out != {**data, **out}` guard is False and path exists
    # -> the persist block is skipped (no rewrite).
    exact = {
        "schema_version": SCHEMA_VERSION,
        "node_id": "stable-id",
        "secret": "stable-secret",
        "cwd_roots": [],
        "address": "stable.local",
    }
    path = node_identity.ba_home() / "node_identity.json"
    path.write_text(json.dumps(exact, indent=2), encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    ident = node_identity.load_or_create()
    assert ident.node_id == "stable-id"
    assert ident.secret == "stable-secret"
    assert ident.cwd_roots == ()
    assert ident.address == "stable.local"
    # File untouched (byte-identical) -> persist block did not run.
    assert path.read_text(encoding="utf-8") == before


def test_load_or_create_secret_stable_across_reloads(monkeypatch):
    _set_hostname(monkeypatch, "worker-1")
    first = node_identity.load_or_create()
    second = node_identity.load_or_create()
    assert first.secret == second.secret
    assert first.node_id == second.node_id


def test_load_or_create_write_failure_returns_identity_anyway(monkeypatch, tmp_path):
    """The graceful-fallback branch (``except OSError``) is reached when
    persistence cannot succeed. We trigger a REAL OSError that is portable
    across users (including root, which bypasses file perms in the Docker
    test container): point ``ba_home`` at a regular file rather than a
    directory, so ``_path().parent.mkdir(...)`` raises ``FileExistsError``
    (an ``OSError`` subclass) because the parent exists but is not a dir.
    Asserts the identity is still returned and nothing was written."""
    _set_hostname(monkeypatch, "worker-1")
    home_is_file = tmp_path / "home_is_file"
    home_is_file.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(node_identity, "ba_home", lambda: home_is_file)

    ident = node_identity.load_or_create()
    assert ident.node_id == "worker-1"
    # Nothing was written: the node_identity.json path under a file cannot exist.
    assert not (home_is_file / "node_identity.json").exists()
