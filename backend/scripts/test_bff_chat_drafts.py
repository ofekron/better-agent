from __future__ import annotations

import ast
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_home


_TEST_HOME = _test_home.isolate(prefix="ba-bff-chat-drafts-")


def _runtime_defines_draft_route() -> bool:
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(main_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            value = decorator.args[0]
            if isinstance(value, ast.Constant) and value.value == "/api/sessions/{session_id}/draft":
                return True
    return False


def test_chat_draft_store_is_monotonic_and_projected() -> None:
    import app_chat_draft_store
    import bff_projection

    session_id = "session-1"
    first = app_chat_draft_store.update(
        session_id,
        draft_input="new",
        client_seq=20,
        draft_images=[{"id": "image-1"}],
    )
    assert first["draft_input"] == "new"
    stale = app_chat_draft_store.update(
        session_id,
        draft_input="old",
        client_seq=10,
        draft_images=[],
    )
    assert stale["rejected"] is True
    assert stale["draft_input"] == "new"
    tree = {"id": session_id, "draft_input": "runtime-stale", "forks": []}
    projected = bff_projection.project_json(f"/api/sessions/{session_id}", tree)
    assert projected["draft_input"] == "new"
    assert projected["draft_input_seq"] == 20
    assert projected["draft_images"] == [{"id": "image-1"}]


def test_runtime_has_no_chat_draft_route_or_store_dependency() -> None:
    backend = Path(__file__).resolve().parents[1]
    assert not _runtime_defines_draft_route()
    for name in ("main.py", "orchestrator.py", "session_manager.py", "session_store.py"):
        source = (backend / name).read_text(encoding="utf-8")
        assert "draft_store" not in source, name
    assert not (backend / "draft_store.py").exists()


def test_draft_limits_fail_closed() -> None:
    import app_chat_draft_store

    try:
        app_chat_draft_store.update(
            "session-2",
            draft_input="x" * (2 * 1024 * 1024 + 1),
            client_seq=1,
        )
    except ValueError as exc:
        assert "2 MiB" in str(exc)
    else:
        raise AssertionError("oversized draft accepted")


if __name__ == "__main__":
    failures = 0
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                try:
                    fn()
                    print(f"PASS {name}")
                except Exception as exc:
                    failures += 1
                    print(f"FAIL {name}: {exc}")
    finally:
        shutil.rmtree(_TEST_HOME)
    sys.exit(1 if failures else 0)
