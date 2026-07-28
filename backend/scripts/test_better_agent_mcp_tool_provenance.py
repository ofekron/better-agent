import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner_better_agent import EventEmitter


def test_mcp_tool_provenance_is_persisted() -> None:
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "events.jsonl"
        emitter = EventEmitter(path)
        emitter.set_tool_provenance({
            "adapters_list": {
                "server_name": "testape",
                "tool_name": "adapters_list",
                "config": {"secret": "must-not-persist"},
            },
        })
        emitter.feed_tool_call_delta(0, "call-1", "adapters_list", "{}")
        emitter.finalize_tool_calls()
        emitter.close()

        event = json.loads(path.read_text().splitlines()[-1])
        [tool_use] = event["message"]["content"]
        assert tool_use["name"] == "adapters_list"
        assert tool_use["mcp_server"] == "testape"
        assert tool_use["mcp_tool"] == "adapters_list"
        assert "config" not in tool_use


if __name__ == "__main__":
    test_mcp_tool_provenance_is_persisted()
    print("PASS")
