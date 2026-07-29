from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def _load_module():
    public_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(public_root / "sdk"))
    sys.path.insert(0, str(public_root / "backend"))
    path = Path(__file__).resolve().parents[1] / "mcp" / "server.py"
    spec = importlib.util.spec_from_file_location("gtm_extension_mcp", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completed(args, *, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_manifest_and_local_server() -> None:
    module = _load_module()
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "better-agent-extension.json").read_text())
    import extension_store

    validated = extension_store.validate_manifest(manifest)
    assert manifest["id"] == "ofek-dev.gtm"
    assert manifest["surfaces"] == ["skills", "runtime_mcp"]
    assert manifest["permissions"] == {"internal_loopback": True}
    assert manifest["entrypoints"]["mcp"][0]["requires_backend_auth"] is True
    assert validated["entrypoints"]["settings"][1]["type"] == "secret"
    assert [skill["name"] for skill in validated["entrypoints"]["skills"]] == [
        "gtm-scout",
        "gtm-writer",
        "gtm-rep",
        "gtm-closer",
        "gtm-mission-control",
        "gtm-pmf-doctor",
        "gtm-market-fit-canvas",
    ]
    for skill in validated["entrypoints"]["skills"]:
        assert (root / skill["path"] / "SKILL.md").is_file()
    required = manifest["protocol"]["smoke_test"]["required_paths"]
    assert "mcp/server.py" in required
    for skill in validated["entrypoints"]["skills"]:
        assert f"{skill['path']}/SKILL.md" in required
    for relative in required:
        assert (root / relative).exists()
    tool = module.build_server()._tool_manager.get_tool("posts_create")
    block_schema = tool.parameters["$defs"]["PostBlock"]
    assert block_schema["required"] == ["content"]
    assert block_schema["properties"]["content"]["type"] == "string"
    assert block_schema["properties"]["media"]["items"]["type"] == "string"


def test_local_mcp_tool_invocation_does_not_use_runtime_broker(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module,
        "_run",
        lambda argv: {"success": True, "argv": argv},
    )
    tool = module.build_server()._tool_manager.get_tool("groups_list")
    assert asyncio.run(tool.run({})) == {
        "success": True,
        "argv": ["integrations:groups"],
    }


def test_api_key_mode_isolates_home_and_parent_secrets(monkeypatch) -> None:
    module = _load_module()
    calls = []
    monkeypatch.setattr(module, "_settings", lambda: {
        "auth_mode": "api_key",
        "api_key": "postiz-secret",
        "api_url": "https://api.postiz.com",
    })
    monkeypatch.setattr(module, "_find_cli", lambda: ["/bin/postiz"])
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args[-1] == "--version":
            return _completed(args, stdout="2.0.15\n")
        return _completed(args, stdout='header\n{"ok":true}\n')

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.integrations_list()["data"] == {"ok": True}
    env = calls[-1][1]["env"]
    assert "POSTIZ_API_KEY" not in calls[0][1]["env"]
    assert env["POSTIZ_API_KEY"] == "postiz-secret"
    assert env["HOME"] == env["USERPROFILE"]
    assert env["HOME"] != os.environ.get("HOME")
    assert "UNRELATED_SECRET" not in env
    assert calls[-1][1]["shell"] is False


def test_oauth_mode_does_not_pass_api_key(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setenv("POSTIZ_API_KEY", "ambient-secret")
    env = module._minimal_env({"auth_mode": "oauth"})
    assert "POSTIZ_API_KEY" not in env
    assert env.get("HOME") == os.environ.get("HOME")


@pytest.mark.parametrize("url", [
    "http://attacker.example",
    "https://user:pass@example.com",
    "https://api.postiz.com/path",
    "https://api.postiz.com?key=value",
])
def test_api_url_rejects_credential_leak_targets(url) -> None:
    module = _load_module()
    with pytest.raises(module.PostizError):
        module._validate_api_url(url)


def test_api_url_allows_https_and_literal_loopback() -> None:
    module = _load_module()
    assert module._validate_api_url("https://self-hosted.example") == "https://self-hosted.example"
    assert module._validate_api_url("http://127.0.0.1:5000") == "http://127.0.0.1:5000"


def test_version_mismatch_fails_before_domain_command(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_settings", lambda: {"auth_mode": "oauth"})
    monkeypatch.setattr(module, "_find_cli", lambda: ["/bin/postiz"])
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return _completed(args, stdout="2.0.14\n")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.groups_list() == {
        "success": False,
        "error_kind": "version_mismatch",
        "error": "Postiz CLI version 2.0.15 is required; found 2.0.14",
    }
    assert calls == [["/bin/postiz", "--version"]]


def test_thread_blocks_preserve_content_media_pairing(monkeypatch) -> None:
    module = _load_module()
    calls = []
    monkeypatch.setattr(module, "_run", lambda argv: calls.append(argv) or {"success": True})
    module.posts_create(
        blocks=[
            {"content": "one", "media": ["one.jpg"]},
            {"content": "two", "media": []},
            {"content": "three", "media": ["three.jpg", "three-2.jpg"]},
        ],
        integrations=["x-1"],
        date="2026-08-01T12:00:00Z",
    )
    expected_blocks = [
        "posts:create",
        "--content", "one", "--media", "one.jpg",
        "--content", "two", "--media", "",
        "--content", "three", "--media", "three.jpg,three-2.jpg",
    ]
    assert calls[0][:len(expected_blocks)] == expected_blocks
    media_values = [
        calls[0][index + 1]
        for index, value in enumerate(calls[0])
        if value == "--media"
    ]
    assert media_values == ["one.jpg", "", "three.jpg,three-2.jpg"]


def test_invalid_mutation_never_spawns(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_run", lambda _argv: pytest.fail("must not spawn"))
    with pytest.raises(module.PostizError):
        module.posts_create([], ["x-1"], "2026-08-01T12:00:00Z")
    with pytest.raises(module.PostizError):
        module.posts_status("post-1", "publish")


def test_upload_rejects_traversal_and_symlink_escape(tmp_path, monkeypatch) -> None:
    module = _load_module()
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"secret")
    (root / "escape.jpg").symlink_to(outside)
    monkeypatch.setenv("BETTER_AGENT_CWD", str(root))
    monkeypatch.setattr(module, "_run", lambda _argv: pytest.fail("must not spawn"))
    for value in ("../secret.jpg", str(outside), "escape.jpg"):
        with pytest.raises(module.PostizError):
            module.upload(value)


def test_upload_passes_one_forward_slash_path_argument(tmp_path, monkeypatch) -> None:
    module = _load_module()
    media = tmp_path / "a&b.jpg"
    media.write_bytes(b"image")
    monkeypatch.setenv("BETTER_AGENT_CWD", str(tmp_path))
    calls = []
    monkeypatch.setattr(module, "_run", lambda argv: calls.append(argv) or {"success": True})
    module.upload(media.name)
    assert calls == [["upload", media.as_posix()]]


def test_windows_shim_resolves_to_node_without_shell(tmp_path, monkeypatch) -> None:
    module = _load_module()
    shim = tmp_path / "postiz.cmd"
    entrypoint = tmp_path / "node_modules" / "postiz" / "dist" / "index.js"
    entrypoint.parent.mkdir(parents=True)
    shim.write_text("@echo off")
    entrypoint.write_text("")
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda name: str(shim) if name == "postiz" else "C:/node/node.exe",
    )
    assert module._find_cli() == ["C:/node/node.exe", str(entrypoint)]


def test_redaction_happens_before_output_bound() -> None:
    module = _load_module()
    prefix = "x" * (module._MAX_OUTPUT - 2)
    output = prefix + "secret-value"
    redacted = module._redact(output, ["secret-value"])
    assert "secret" not in redacted
    assert redacted.endswith("[R")


def test_timeout_and_version_errors_redact_secret(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_settings", lambda: {
        "auth_mode": "api_key",
        "api_key": "postiz-secret",
        "api_url": "https://api.postiz.com",
    })
    monkeypatch.setattr(module, "_find_cli", lambda: ["/bin/postiz"])
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(args[0], 15, stderr="postiz-secret")
        ),
    )
    result = module.groups_list()
    assert result["error_kind"] == "timeout"
    assert "[REDACTED]" in result["error"]
    assert "postiz-secret" not in result["error"]


def test_version_mismatch_redacts_secret(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_settings", lambda: {
        "auth_mode": "api_key",
        "api_key": "postiz-secret",
        "api_url": "https://api.postiz.com",
    })
    monkeypatch.setattr(module, "_find_cli", lambda: ["/bin/postiz"])
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: _completed([], stdout="postiz-secret"),
    )
    result = module.groups_list()
    assert result["error_kind"] == "version_mismatch"
    assert "postiz-secret" not in result["error"]
    assert "[REDACTED]" in result["error"]


def test_missing_cli_is_structured(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_settings", lambda: {"auth_mode": "oauth"})
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    assert module.groups_list() == {
        "success": False,
        "error_kind": "dependency_missing",
        "error": "Postiz CLI 2.0.15 is required on PATH",
    }


def test_settings_transport_failure_is_structured(monkeypatch) -> None:
    module = _load_module()

    class FailingClient:
        def get_settings(self):
            raise module.BetterAgentError("secret transport detail")

    monkeypatch.setattr(module, "Client", FailingClient)
    assert module.groups_list() == {
        "success": False,
        "error_kind": "settings_unavailable",
        "error": "Postiz extension settings are unavailable",
    }


def test_output_parsing_and_error_redaction(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_settings", lambda: {
        "auth_mode": "api_key",
        "api_key": "postiz-secret",
        "api_url": "https://api.postiz.com",
    })
    monkeypatch.setattr(module, "_find_cli", lambda: ["/bin/postiz"])
    outcomes = iter([
        _completed([], stdout="2.0.15\n"),
        _completed([], returncode=1, stderr="request failed: postiz-secret"),
    ])
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: next(outcomes))
    result = module.posts_delete("post-1")
    assert result == {
        "success": False,
        "error_kind": "cli_failed",
        "exit_code": 1,
        "error": "request failed: [REDACTED]",
    }
