import json
import os
import shutil
import stat
import sqlite3
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-agy-")
_BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND))

import config_store  # noqa: E402
from provider import _resolve_class  # noqa: E402
from provider_agy import AgyProvider, fetch_agy_models  # noqa: E402
from runner_agy import _agy_worker_events, _materialize_agy_run_home, main as runner_main  # noqa: E402


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _make_fake_agy(bin_dir: Path) -> Path:
    agy = bin_dir / "agy"
    agy.write_text(
        "#!/usr/bin/env sh\n"
        "set -eu\n"
        "if [ \"$1\" = \"models\" ]; then\n"
        "  printf 'Gemini Test (High)\\nOther Model\\n'\n"
        "  exit 0\n"
        "fi\n"
        "log_file=''\n"
        "conversation=''\n"
        "add_dirs=''\n"
        "timeout=''\n"
        "model=''\n"
        "prompt=''\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    --model) model=\"$2\"; shift 2 ;;\n"
        "    --conversation) conversation=\"$2\"; shift 2 ;;\n"
        "    --add-dir) add_dirs=\"${add_dirs}${2};\"; shift 2 ;;\n"
        "    --print-timeout) timeout=\"$2\"; shift 2 ;;\n"
        "    --log-file) log_file=\"$2\"; shift 2 ;;\n"
        "    -p|--print|--prompt) prompt=\"$2\"; shift 2 ;;\n"
        "    *) printf 'bad argv token: %s\\n' \"$1\" >> \"$AGY_ARGV_LOG\"; exit 2 ;;\n"
        "  esac\n"
        "done\n"
        "if [ -z \"$prompt\" ]; then\n"
        "  printf 'missing prompt\\n' >> \"$AGY_ARGV_LOG\"\n"
        "  exit 2\n"
        "fi\n"
        "if [ \"${AGY_FAKE_AUTH_FAIL:-}\" = \"1\" ]; then\n"
        "  printf 'Authentication required. Please visit the URL to log in:\\n'\n"
        "  printf '  https://accounts.google.com/o/oauth2/auth?fake\\n\\n'\n"
        "  printf 'Waiting for authentication (timeout 30s)...\\n'\n"
        "  printf 'Or, paste the authorization code here and press Enter:\\n'\n"
        "  printf 'Error: authentication timed out.\\n'\n"
        "  exit 0\n"
        "fi\n"
        "sid=\"${conversation:-11111111-2222-3333-4444-555555555555}\"\n"
        "mkdir -p \"$HOME/.gemini/antigravity-cli/conversations\" \"$HOME/.gemini/antigravity-cli/cache\"\n"
        "touch \"$HOME/.gemini/antigravity-cli/conversations/${sid}.db\"\n"
        "printf '{\"%s\":\"%s\"}\\n' \"$PWD\" \"$sid\" > \"$HOME/.gemini/antigravity-cli/cache/last_conversations.json\"\n"
        "if [ -n \"$log_file\" ]; then\n"
        "  printf 'I0621 printmode.go: Print mode: starting (promptLength=%s, model=\"%s\", conversationID=\"%s\")\\n' \"${#prompt}\" \"$model\" \"$conversation\" > \"$log_file\"\n"
        "  printf 'I0621 server.go: Created conversation %s\\n' \"$sid\" >> \"$log_file\"\n"
        "  printf 'I0621 printmode.go: Print mode: conversation=%s, sending message\\n' \"$sid\" >> \"$log_file\"\n"
        "  printf 'I0621 server.go: Stream goroutine exited for %s, sending completion signal\\n' \"$sid\" >> \"$log_file\"\n"
        "fi\n"
        "printf 'model=%s\\n' \"$model\" >> \"$AGY_ARGV_LOG\"\n"
        "printf 'conversation=%s\\n' \"$conversation\" >> \"$AGY_ARGV_LOG\"\n"
        "printf 'add_dirs=%s\\n' \"$add_dirs\" >> \"$AGY_ARGV_LOG\"\n"
        "printf 'timeout=%s\\n' \"$timeout\" >> \"$AGY_ARGV_LOG\"\n"
        "printf 'prompt=%s\\n' \"$prompt\" >> \"$AGY_ARGV_LOG\"\n"
        "printf 'agy says: %s\\n' \"$prompt\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    agy.chmod(agy.stat().st_mode | stat.S_IXUSR)
    return agy


def test_registry_and_capabilities() -> None:
    cls = _resolve_class("agy")
    check(cls is AgyProvider, "provider registry resolves agy")
    check(cls.supports_manager_mode is False, "agy team mode is disabled")
    check(cls.supports_fork is False, "agy fork is disabled")
    check(cls.supports_native_subagents is True, "agy native subagents are enabled")


def test_config_dir_does_not_export_claude_env() -> None:
    old_claude_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    provider = config_store.add_provider({
        "name": "Antigravity",
        "kind": "agy",
        "mode": "subscription",
        "config_dir": "$HOME/.gemini/antigravity-cli",
    })
    try:
        os.environ["CLAUDE_CONFIG_DIR"] = "/tmp/should-clear"
        config_store.apply_env_vars(provider["id"])
        check("CLAUDE_CONFIG_DIR" not in os.environ, "agy config_dir does not become CLAUDE_CONFIG_DIR")
        engine_env = (Path(_TMP_HOME) / "engine.env").read_text(encoding="utf-8")
        check("unset CLAUDE_CONFIG_DIR" in engine_env, "agy engine env unsets CLAUDE_CONFIG_DIR")
    finally:
        if old_claude_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old_claude_dir


def test_model_fetch_and_runner() -> None:
    bin_dir = Path(tempfile.mkdtemp(prefix="bc-test-agy-bin-"))
    fake_home = Path(tempfile.mkdtemp(prefix="bc-test-agy-home-"))
    old_path = os.environ.get("PATH", "")
    old_home = os.environ.get("HOME")
    try:
        _make_fake_agy(bin_dir)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
        os.environ["HOME"] = str(fake_home)
        argv_log = bin_dir / "argv.log"
        os.environ["AGY_ARGV_LOG"] = str(argv_log)
        check(fetch_agy_models() == ["Gemini Test (High)", "Other Model"], "agy models parsed")
        run_dir = Path(tempfile.mkdtemp(prefix="bc-test-agy-run-"))
        (run_dir / "input.json").write_text(
            json.dumps({
                "prompt": "hello",
                "cwd": str(run_dir),
                "model": "Gemini Test (High)",
                "provider_run_config": {
                    "mcp_servers": {"demo": {"command": "demo", "args": []}},
                    "skills": {"demo-skill": "Use demo skill.\n"},
                },
            }),
            encoding="utf-8",
        )
        code = runner_main(run_dir)
        check(code == 0, "runner exits cleanly")
        complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
        check(complete["success"] is True, "runner marks success")
        check(complete["session_id"] == "11111111-2222-3333-4444-555555555555", "runner captures real agy conversation id")
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        check(state["session_id"] == complete["session_id"], "state stores real agy conversation id")
        agy_home = run_dir / "agy-home"
        settings = json.loads((agy_home / ".gemini" / "antigravity-cli" / "settings.json").read_text(encoding="utf-8"))
        check(settings["mcpServers"]["demo"]["command"] == "demo", "agy mcp settings are run-local")
        skill_file = agy_home / ".gemini" / "antigravity-cli" / "builtin" / "skills" / "demo-skill" / "SKILL.md"
        check("Use demo skill." in skill_file.read_text(encoding="utf-8"), "agy skill is written")
        event_rows = [
            json.loads(line)
            for line in (run_dir / "session_events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        check("agy says: hello" in json.dumps(event_rows), "runner emits assistant event")
        check(all(row.get("type") != "assistant" for row in event_rows), "runner does not emit raw assistant rows")
        primary = event_rows[-1]
        check(primary["type"] == "agent_message", "runner emits primary response as agent_message")
        check(primary["data"]["type"] == "assistant", "primary response has assistant inner type")
        check(
            primary["data"]["message"]["content"][0]["type"] == "text",
            "primary response uses renderable text block",
        )
        argv_lines = argv_log.read_text(encoding="utf-8").splitlines()
        check(argv_lines == [
            "model=Gemini Test (High)",
            "conversation=",
            f"add_dirs={run_dir};",
            "timeout=24h",
            "prompt=hello",
        ], "runner passes agy native flags before prompt")

        run_dir_2 = Path(tempfile.mkdtemp(prefix="bc-test-agy-run-resume-"))
        conv_dir = fake_home / ".gemini" / "antigravity-cli" / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        (conv_dir / f"{complete['session_id']}.db").touch()
        (run_dir_2 / "input.json").write_text(
            json.dumps({
                "prompt": "again",
                "cwd": str(run_dir_2),
                "model": "Gemini Test (High)",
                "session_id": complete["session_id"],
            }),
            encoding="utf-8",
        )
        code = runner_main(run_dir_2)
        check(code == 0, "resume runner exits cleanly")
        complete_2 = json.loads((run_dir_2 / "complete.json").read_text(encoding="utf-8"))
        check(complete_2["session_id"] == complete["session_id"], "runner resumes requested agy conversation")
        argv_after_resume = argv_log.read_text(encoding="utf-8")
        check(
            f"conversation={complete['session_id']}" in argv_after_resume,
            "runner passes validated agy conversation id on resume",
        )

        run_dir_3 = Path(tempfile.mkdtemp(prefix="bc-test-agy-run-bad-resume-"))
        valid_sid = "22222222-3333-4444-5555-666666666666"
        (conv_dir / f"{valid_sid}.db").touch()
        cache_dir = fake_home / ".gemini" / "antigravity-cli" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_conversations.json").write_text(
            json.dumps({str(run_dir_3): valid_sid}),
            encoding="utf-8",
        )
        (run_dir_3 / "input.json").write_text(
            json.dumps({
                "prompt": "bad resume",
                "cwd": str(run_dir_3),
                "model": "Gemini Test (High)",
                "session_id": "99999999-9999-9999-9999-999999999999",
            }),
            encoding="utf-8",
        )
        argv_before_bad = argv_log.read_text(encoding="utf-8")
        code = runner_main(run_dir_3)
        check(code == 0, "bad stored id runner exits cleanly")
        complete_3 = json.loads((run_dir_3 / "complete.json").read_text(encoding="utf-8"))
        # Fail closed: an invalid stored id must NOT be "repaired" from the
        # cwd-keyed cache. Many app sessions share a cwd, so adopting the cwd
        # cache's conversation grafts another session's turn onto this one
        # (the cross-session contamination bug). It must start fresh instead —
        # never the cwd-cache id (valid_sid) and never the bad requested id.
        check(complete_3["session_id"] != valid_sid, "invalid stored id is NOT repaired from cwd cache")
        check(
            complete_3["session_id"] != "99999999-9999-9999-9999-999999999999",
            "invalid stored id is not passed through as the conversation",
        )
        check(
            complete_3["session_id"] == "11111111-2222-3333-4444-555555555555",
            "invalid stored id starts a fresh agy conversation",
        )
        argv_for_bad = argv_log.read_text(encoding="utf-8")[len(argv_before_bad):]
        check(
            f"conversation={valid_sid}" not in argv_for_bad
            and "conversation=99999999-9999-9999-9999-999999999999" not in argv_for_bad,
            "runner passes no cwd-cache/bad conversation id to the agy CLI",
        )

        run_dir_4 = Path(tempfile.mkdtemp(prefix="bc-test-agy-run-auth-"))
        (run_dir_4 / "input.json").write_text(
            json.dumps({
                "prompt": "auth fail",
                "cwd": str(run_dir_4),
                "model": "Gemini Test (High)",
            }),
            encoding="utf-8",
        )
        os.environ["AGY_FAKE_AUTH_FAIL"] = "1"
        code = runner_main(run_dir_4)
        os.environ.pop("AGY_FAKE_AUTH_FAIL", None)
        check(code == 1, "auth prompt with exit 0 is classified as failure")
        complete_4 = json.loads((run_dir_4 / "complete.json").read_text(encoding="utf-8"))
        check(complete_4["success"] is False, "auth prompt complete is unsuccessful")
        check("authentication timed out" in complete_4["error"], "auth timeout error is explicit")
        auth_rows = [
            json.loads(line)
            for line in (run_dir_4 / "session_events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        auth_text = auth_rows[-1]["data"]["message"]["content"][0]["text"]
        check(
            auth_text.startswith("Error: Antigravity authentication timed out"),
            "auth failure renders as error text",
        )
    finally:
        os.environ["PATH"] = old_path
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        os.environ.pop("AGY_ARGV_LOG", None)
        os.environ.pop("AGY_FAKE_AUTH_FAIL", None)
        shutil.rmtree(bin_dir, ignore_errors=True)
        shutil.rmtree(fake_home, ignore_errors=True)


def _write_agy_db(path: Path, rows: list[tuple[int, int, bytes]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "create table steps (idx integer, step_type integer, status integer, "
            "has_subtrajectory integer, metadata blob, step_payload blob, render_info blob)"
        )
        con.executemany(
            "insert into steps values (?, ?, 0, 0, ?, ?, ?)",
            [(idx, step_type, b"", payload, b"") for idx, step_type, payload in rows],
        )
        con.commit()
    finally:
        con.close()


def test_native_subagent_events_from_agy_db() -> None:
    home = Path(tempfile.mkdtemp(prefix="bc-test-agy-home-"))
    parent_sid = "11111111-2222-3333-4444-555555555555"
    child_sid = "22222222-3333-4444-5555-666666666666"
    conv_dir = home / ".gemini" / "antigravity-cli" / "conversations"
    try:
        _write_agy_db(conv_dir / f"{parent_sid}.db", [
            (
                0,
                127,
                b'tool123 invoke_subagent {"Subagents":[{"Prompt":"Find files","Role":"Researcher","TypeName":"research"}]}:',
            ),
            (
                1,
                101,
                (
                    b"[Message] timestamp=2026-06-21T10:00:00Z "
                    b"sender=22222222-3333-4444-5555-666666666666 "
                    b"priority=MESSAGE_PRIORITY_HIGH content=done result"
                ),
            ),
        ])
        _write_agy_db(conv_dir / f"{child_sid}.db", [
            (0, 15, b"assistant child text from antigravity"),
            (
                1,
                9,
                b'toolx list_dir {"DirectoryPath":"/tmp","toolAction":"Listing","toolSummary":"List"}:',
            ),
            (2, 23, b"Listed /tmp successfully"),
        ])
        events = _agy_worker_events(
            agy_home=home,
            conversation_id=parent_sid,
            parent_uuid="parent",
        )
        types = [event["type"] for event in events]
        check(types.count("worker_start") == 1, "agy emits one worker_start")
        check("worker_complete" in types, "agy emits worker_complete")
        payload = json.dumps(events)
        check("Researcher" in payload, "worker description comes from invoke_subagent")
        check("done result" in payload, "parent-visible subagent result is captured")
        check("assistant child text from antigravity" in payload, "child conversation text is captured")
        check('"name": "list_dir"' in payload, "child tool use keeps AGY tool name")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_worker_envelopes_hydrate_from_events_jsonl() -> None:
    import session_store
    from event_ingester import event_ingester
    from session_manager import manager as session_manager

    sid = "agy-hydrate-root"
    msg_id = "agy-hydrate-msg"
    session_store.write_session_full({
        "id": sid,
        "cwd": "/tmp",
        "orchestration_mode": "native",
        "messages": [{
            "id": msg_id,
            "role": "assistant",
            "content": "",
            "timestamp": "2026-06-21T10:00:00",
        }],
        "forks": [],
        "_schema_version": 11,
    }, bump_updated_at=False)
    event_ingester.ingest(
        sid,
        sid,
        "worker_start",
        {
            "delegation_id": "agy-worker",
            "worker_session_id": "agy-child",
            "worker_description": "AGY Researcher",
            "panel_kind": "worker",
        },
        source="test",
        msg_id=msg_id,
        cwd_override="",
    )
    event_ingester.ingest(
        sid,
        sid,
        "worker_event",
        {
            "delegation_id": "agy-worker",
            "event": {
                "type": "agent_message",
                "data": {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "child result"}],
                        "model": "agy",
                    },
                    "uuid": "agy-child-event",
                },
            },
        },
        source="test",
        msg_id=msg_id,
        cwd_override="",
    )
    event_ingester.ingest(
        sid,
        sid,
        "worker_complete",
        {
            "delegation_id": "agy-worker",
            "worker_session_id": "agy-child",
            "success": True,
        },
        source="test",
        msg_id=msg_id,
        cwd_override="",
    )
    hydrated = session_manager.get_root_tree(sid)
    msg = hydrated["messages"][0]
    workers = msg.get("workers") or []
    check(len(workers) == 1, "hydrate rebuilds worker panel from event journal")
    check(workers[0]["worker_description"] == "AGY Researcher", "worker_start data is preserved")
    check(len(workers[0].get("events") or []) == 1, "worker_event routes into hydrated panel")
    check(workers[0]["success"] is True, "worker_complete data is preserved")


def test_wrapped_worker_envelopes_route_to_panel() -> None:
    import session_store
    from event_ingester import event_ingester
    from session_manager import manager as session_manager

    sid = "agy-wrapped-root"
    msg_id = "agy-wrapped-msg"
    delegation_id = "agy_subagent_fd04f443-3305-43ea-a28e-3a96e20aa993"
    session_store.write_session_full({
        "id": sid,
        "cwd": "/tmp",
        "orchestration_mode": "native",
        "messages": [{
            "id": msg_id,
            "role": "assistant",
            "content": "",
            "timestamp": "2026-06-21T10:00:00",
        }],
        "forks": [],
        "_schema_version": 11,
    }, bump_updated_at=False)
    event_ingester.ingest(
        sid,
        sid,
        "worker_start",
        {
            "delegation_id": delegation_id,
            "worker_session_id": "agy-child",
            "worker_description": "Codebase Researcher",
            "panel_kind": "worker",
        },
        source="test",
        msg_id=msg_id,
        cwd_override="",
    )
    event_ingester.ingest(
        sid,
        sid,
        "agent_message",
        {
            "type": "agent_message",
            "data": {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "text",
                        "text": "nested primary output",
                    }],
                    "model": "agy",
                },
                "uuid": "wrapped-primary-event",
            },
        },
        source="test",
        msg_id=msg_id,
        cwd_override="",
    )
    event_ingester.ingest(
        sid,
        sid,
        "agent_message",
        {
            "type": "worker_event",
            "data": {
                "delegation_id": delegation_id,
                "event": {
                    "type": "agent_message",
                    "data": {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{
                                "type": "tool_use",
                                "id": "uowa9knb-0",
                                "name": "Agent",
                                "input": {"description": "Codebase Researcher"},
                            }],
                            "model": "agy",
                        },
                        "uuid": "wrapped-worker-event",
                    },
                },
            },
        },
        source="test",
        msg_id=msg_id,
        cwd_override="",
    )
    event_ingester.ingest(
        sid,
        sid,
        "agent_message",
        {
            "type": "worker_complete",
            "data": {
                "delegation_id": delegation_id,
                "worker_session_id": "agy-child",
                "success": True,
            },
        },
        source="test",
        msg_id=msg_id,
        cwd_override="",
    )

    hydrated = session_manager.get_root_tree(sid)
    msg = hydrated["messages"][0]
    workers = msg.get("workers") or []
    events = msg.get("events") or []
    check(len(events) == 1, "wrapped primary envelope hydrates into msg.events")
    check(events[0]["data"]["uuid"] == "wrapped-primary-event", "nested primary event is unwrapped")
    check(events[0]["data"]["type"] == "assistant", "nested primary event has canonical inner type")
    check(len(workers) == 1, "wrapped worker event keeps worker panel")
    check(len(workers[0].get("events") or []) == 1, "wrapped worker_event routes into panel")
    check(workers[0]["events"][0]["data"]["uuid"] == "wrapped-worker-event", "inner event is preserved")
    check(workers[0]["success"] is True, "wrapped worker_complete updates panel")


def test_agy_run_home_overlay_carries_library_for_auth() -> None:
    # Regression: agy stores its OAuth credential under $HOME/Library and has
    # no config-dir env var (unlike the gemini CLI's GEMINI_CLI_HOME), so it
    # hard-wires $HOME/.gemini/antigravity-cli. The scoped run HOME must mirror
    # the real home top-level — carrying Library — or agy can't authenticate and
    # every run fails with "authentication timed out" (seen in backend logs).
    run_dir = Path(tempfile.mkdtemp(prefix="agy-overlay-"))
    real_home = Path.home()
    scoped = _materialize_agy_run_home(
        run_dir, {"mcp_servers": {"x": {"command": "echo", "args": []}}}
    )
    check(scoped is not None, "overlay is materialized when an mcp server is present")
    overlay = Path(scoped["HOME"])
    library = overlay / "Library"
    check(library.is_symlink(), "overlay HOME mirrors real ~/Library so agy can authenticate")
    check(
        library.resolve() == (real_home / "Library").resolve(),
        "overlay Library points at the real home Library",
    )
    # .gemini stays the dedicated overlay: per-run merged settings.json lives
    # here as a real file, not the raw real-home antigravity-cli symlink.
    merged_settings = overlay / ".gemini" / "antigravity-cli" / "settings.json"
    check(merged_settings.is_file() and not merged_settings.is_symlink(), "per-run agy settings.json is a real overlay file")
    check(
        json.loads(merged_settings.read_text())["mcpServers"]["x"]["command"] == "echo",
        "merged overlay settings carry the run-local mcp server",
    )
    shutil.rmtree(run_dir, ignore_errors=True)


def main() -> int:
    tests = [
        test_registry_and_capabilities,
        test_config_dir_does_not_export_claude_env,
        test_model_fetch_and_runner,
        test_native_subagent_events_from_agy_db,
        test_worker_envelopes_hydrate_from_events_jsonl,
        test_wrapped_worker_envelopes_route_to_panel,
        test_agy_run_home_overlay_carries_library_for_auth,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
