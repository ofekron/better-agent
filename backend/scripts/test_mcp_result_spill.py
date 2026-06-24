import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_result_spill import spill_large_result  # noqa: E402


def test_small_result_stays_inline() -> None:
    result = {"success": True, "matches": [{"text": "small"}]}
    assert spill_large_result(result, label="test", token_limit=10_000) == result


def test_large_result_spills_to_tmp_file() -> None:
    result = {
        "success": True,
        "count": 1,
        "rg_args": ["needle"],
        "matches": [{"text": "x" * 200}],
    }
    compact = spill_large_result(result, label="test-get-requirements", token_limit=10)

    assert compact["success"] is True
    assert compact["result_spilled_to_file"] is True
    assert compact["count"] == 1
    assert "rg_args" not in compact
    assert "matches" not in compact
    path = Path(compact["result_path"])
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8")) == result
    path.unlink()


if __name__ == "__main__":
    test_small_result_stays_inline()
    test_large_result_spills_to_tmp_file()
    print("OK: mcp_result_spill")
