from __future__ import annotations

import _test_home

_test_home.isolate("bc-test-tool-schemas-")

import runner  # noqa: E402


def _schema_type(schema: dict, field: str) -> object:
    return schema["properties"][field]["type"]


def test_optional_tool_defaults_are_omitted_not_null() -> None:
    cases = [
        (runner._OPEN_FILE_PANEL_INPUT_SCHEMA, "start_line"),
        (runner._OPEN_FILE_PANEL_INPUT_SCHEMA, "end_line"),
        (runner._OPEN_FILE_PANEL_INPUT_SCHEMA, "selected_start"),
        (runner._OPEN_FILE_PANEL_INPUT_SCHEMA, "selected_end"),
    ]
    nullable = [
        field
        for schema, field in cases
        if isinstance(_schema_type(schema, field), list)
        and "null" in _schema_type(schema, field)
    ]
    assert not nullable, (
        f"optional tool defaults must omit fields instead of allowing null: {nullable}"
    )


def test_canvas_tool_is_not_public_runner_logic() -> None:
    assert not hasattr(runner, "_CANVAS_INPUT_SCHEMA"), "public runner has canvas schema"
    assert not hasattr(runner, "_build_canvas_tool"), "public runner has canvas tool builder"
    assert not hasattr(runner, "_CREDENTIAL_REQUEST_INPUT_SCHEMA"), \
        "public runner has credential schema"
