from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


home = tempfile.mkdtemp(prefix="ba-ambient-policy-")
os.environ["BETTER_AGENT_HOME"] = home
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ambient_mcp_policy_store


def main() -> None:
    try:
        assert ambient_mcp_policy_store.public() == {
            "share_all_eligible": True,
            "excluded_ids": [],
            "generation": 0,
            "updated_at": None,
        }

        ambient_mcp_policy_store.mutate_and_reconcile(
            lambda policy: policy["excluded_ids"].extend(["extension:coordination"]),
            lambda: None,
        )
        assert ambient_mcp_policy_store.is_exposed("extension:coordination") is False
        assert ambient_mcp_policy_store.is_exposed("extension:future") is True

        before = ambient_mcp_policy_store.get()
        try:
            ambient_mcp_policy_store.mutate_and_reconcile(
                lambda policy: policy.update({"share_all_eligible": False}),
                lambda: (_ for _ in ()).throw(RuntimeError("PCS unavailable")),
            )
            raise AssertionError("failed reconciliation was accepted")
        except RuntimeError as exc:
            assert str(exc) == "PCS unavailable"
        assert ambient_mcp_policy_store.get() == before

        path = Path(home) / "ambient_mcp_policy.json"
        path.write_text(json.dumps({"version": 0}), encoding="utf-8")
        try:
            ambient_mcp_policy_store.get()
            raise AssertionError("old policy schema was accepted")
        except ValueError as exc:
            assert "unsupported" in str(exc)
        print("PASS ambient MCP policy persistence and rollback")
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
