"""Unit coverage for extension_mcp.py.

Distinct from test_extension_mcp_launcher_security.py (which exercises the
launcher *subprocess* / resolution path in extension_store) — here we cover
this module directly: the launcher-server-item dict builder and its
frozen-vs-dev command derivation. No subprocess is spawned.
"""
from __future__ import annotations

import sys
from pathlib import Path

import extension_mcp


# --------------------------------------------------------- launcher_server_item

def test_launcher_server_item_uses_explicit_command_and_args():
    item = extension_mcp.launcher_server_item(
        "ext-1", "srv-1", command="/path/to/bin", args=["--flag", "x"],
    )
    assert item == {
        "command": "/path/to/bin",
        "args": ["--flag", "x"],
        "env": {
            extension_mcp._MARKER_EXTENSION_ID: "ext-1",
            extension_mcp._MARKER_SERVER_NAME: "srv-1",
        },
    }


def test_launcher_server_item_partial_inputs_trigger_derivation(monkeypatch):
    # command given but args None -> the `or` forces full derivation.
    monkeypatch.setattr(extension_mcp, "_launcher_command",
                        lambda eid, sn: ("DERIVED", ["d", eid, sn]))
    item = extension_mcp.launcher_server_item("ext-2", "srv-2", command="/bin")
    assert item["command"] == "DERIVED"
    assert item["args"] == ["d", "ext-2", "srv-2"]


def test_launcher_server_item_derives_env_markers_always_set(monkeypatch):
    monkeypatch.setattr(extension_mcp, "_launcher_command",
                        lambda eid, sn: ("c", ["a"]))
    item = extension_mcp.launcher_server_item("ext-3", "srv-3")
    # Markers carry the caller's ids regardless of command derivation.
    assert item["env"][extension_mcp._MARKER_EXTENSION_ID] == "ext-3"
    assert item["env"][extension_mcp._MARKER_SERVER_NAME] == "srv-3"


# ------------------------------------------------------------ _launcher_command

def test_launcher_command_dev_path_resolves_sibling_script(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    command, args = extension_mcp._launcher_command("ext", "srv")
    assert command == sys.executable
    expected_script = Path(extension_mcp.__file__).with_name("extension_mcp_launcher.py")
    assert args == [str(expected_script), "ext", "srv"]


def test_launcher_command_frozen_path_uses_flag_form(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    try:
        command, args = extension_mcp._launcher_command("ext-f", "srv-f")
    finally:
        monkeypatch.delattr(sys, "frozen", raising=False)
    assert command == sys.executable
    assert args == ["--extension-mcp", "ext-f", "srv-f"]
