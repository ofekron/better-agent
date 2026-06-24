from __future__ import annotations

import os
import sys
import tempfile

import _test_home
_test_home.isolate("bc-test-tool-schemas-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import runner  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond:
        FAILURES.append(msg)


def schema_type(schema: dict, field: str) -> object:
    return schema["properties"][field]["type"]


def t_optional_tool_defaults_are_omitted_not_null() -> None:
    cases = [
        (runner._OPEN_FILE_PANEL_INPUT_SCHEMA, "start_line"),
        (runner._OPEN_FILE_PANEL_INPUT_SCHEMA, "end_line"),
        (runner._OPEN_FILE_PANEL_INPUT_SCHEMA, "selected_start"),
        (runner._OPEN_FILE_PANEL_INPUT_SCHEMA, "selected_end"),
    ]
    nullable = [
        field
        for schema, field in cases
        if isinstance(schema_type(schema, field), list) and "null" in schema_type(schema, field)
    ]
    check(not nullable, f"optional tool defaults omit fields instead of allowing null: {nullable}")


def t_canvas_tool_is_not_public_runner_logic() -> None:
    check(not hasattr(runner, "_CANVAS_INPUT_SCHEMA"), "public runner has no canvas schema")
    check(not hasattr(runner, "_build_canvas_tool"), "public runner has no canvas tool builder")
    check(not hasattr(runner, "_CREDENTIAL_REQUEST_INPUT_SCHEMA"), "public runner has no credential schema")


def main() -> int:
    for name, fn in [
        ("optional tool defaults are omitted not null", t_optional_tool_defaults_are_omitted_not_null),
        ("canvas tool is not public runner logic", t_canvas_tool_is_not_public_runner_logic),
    ]:
        print(f"\n--- {name} ---")
        try:
            fn()
        except Exception as e:
            FAILURES.append(f"{name}: {e!r}")
            import traceback
            traceback.print_exc()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} assertion(s)")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
