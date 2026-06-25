#!/usr/bin/env python3
import os
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp_home = tempfile.mkdtemp(prefix="ba-windsurf-native-")
os.environ["HOME"] = _tmp_home
os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="ba-windsurf-state-")

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

import native_session_miner as nsm  # noqa: E402
import native_session_prompt_search as nsps  # noqa: E402
import native_transcript_index as nti  # noqa: E402


KEY = b"safeCodeiumworldKeYsecretBalloon"


def _varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _field(field: int, wire_type: int, value: bytes | int) -> bytes:
    head = _varint((field << 3) | wire_type)
    if wire_type == 0:
        return head + _varint(int(value))
    if wire_type == 2:
        raw = value if isinstance(value, bytes) else str(value).encode()
        return head + _varint(len(raw)) + raw
    raise AssertionError(wire_type)


def _msg(field: int, *parts: bytes) -> bytes:
    return _field(field, 2, b"".join(parts))


def _meta(seconds: int = 1_767_467_722) -> bytes:
    return _msg(5, _msg(1, _field(1, 0, seconds)), _field(12, 2, b"conv-1"), _field(28, 2, b"claude-sonnet-4-6"))


def _step(step_id: int, variant_field: int, variant: bytes) -> bytes:
    return _msg(2, _field(1, 0, step_id), _field(4, 0, 1), _meta(), _field(variant_field, 2, variant))


def _fixture_plaintext() -> bytes:
    tool = _msg(7, _field(1, 2, b"tool-1"), _field(2, 2, b"code_search"), _field(3, 2, b'{"query":"optimizer timing"}'))
    edit_target = _msg(2, _msg(1, _field(8, 2, b"file:///repo/optimizer.py")))
    return b"".join([
        _field(1, 2, b"trajectory-1"),
        _step(14, 19, _field(2, 2, b"where is optimizer timing configured?")),
        _step(15, 20, _field(1, 2, b"I will inspect optimizer.py now.") + tool),
        _step(16, 30, _field(4, 2, b"Plan\nInspect and answer.")),
        _step(17, 10, edit_target),
        _step(18, 13, _field(1, 2, b"optimizer") + _field(2, 2, b"*.py")),
    ])


def _write_encrypted_fixture() -> Path:
    root = Path(_tmp_home) / ".codeium" / "cascade"
    root.mkdir(parents=True)
    nonce = b"123456789012"
    blob = nonce + AESGCM(KEY).encrypt(nonce, _fixture_plaintext(), None)
    path = root / "session-one.pb"
    path.write_bytes(blob)
    return path


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_windsurf_elements_and_index() -> None:
    path = _write_encrypted_fixture()
    elements = nsm.NativeCandidate(
        key="test", sid=path.stem, cwd="", data={}, transcript=path, mtime=0, format="windsurf"
    ).parse_elements()
    kinds = [element.kind for element in elements]
    check(kinds == ["user_prompt", "assistant_text", "tool_call", "assistant_text", "tool_call", "tool_call"], kinds)
    check(elements[0].text == "where is optimizer timing configured?", "missing user prompt")
    check(elements[2].tool_name == "code_search", "missing tool name")
    check(elements[0].timestamp == "2026-01-03T19:15:22Z", elements[0].timestamp)

    discovered = [candidate for candidate in nsm.iter_all_native_candidates() if candidate.format == "windsurf"]
    check([candidate.transcript for candidate in discovered] == [path], "windsurf candidate not discovered")

    fallback_matches = nsps.search_in_native_session_transcript(query="optimizer timing", max_matches=5)
    check(any(match["element_kind"] == "user_prompt" for match in fallback_matches), fallback_matches)

    nti.reset_for_test()
    try:
        first = nti.refresh_once(full=True)
        check(first["touched"] == 1, first)
        rows = nti.run_readonly_sql(
            "SELECT tag, element_kind, tool_name, text FROM native_element_fts "
            "WHERE native_element_fts MATCH 'optimizer' ORDER BY element_index"
        )["rows"]
        check(any(row[0] == "windsurf" and row[1] == "user_prompt" for row in rows), rows)
        check(any(row[0] == "windsurf" and row[2] == "code_search" for row in rows), rows)
    finally:
        nti.shutdown()


def main() -> int:
    try:
        test_windsurf_elements_and_index()
    finally:
        shutil.rmtree(_tmp_home, ignore_errors=True)
        shutil.rmtree(os.environ["BETTER_AGENT_HOME"], ignore_errors=True)
    print("PASS native Windsurf miner/index")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
