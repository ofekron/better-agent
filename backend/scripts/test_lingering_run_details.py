"""Locks `Provider.lingering_run_details` — the rich per-run snapshot the
background-strip info panel reads. Constructs real objects over a temp run
dir (no CLI subprocess, no mocks of the system under test): verifies the
method surfaces run_id / mode / started_at / target_message_id and the
originating prompt read from input.json, and filters to lingering runs of
the requested session only.

Run: python backend/scripts/test_lingering_run_details.py
"""
import os
import sys
import tempfile
from pathlib import Path

# Isolate state dir BEFORE importing any backend module.
os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="ba-linger-details-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from provider_claude import ClaudeProvider, RunState  # noqa: E402


def _make_run(run_dir: Path, *, prompt: str, app_session_id: str,
              mode: str = "native", lingering: bool = True,
              started_at: str = "2026-06-30T10:00:00Z") -> RunState:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.json").write_text(
        f'{{"prompt": "{prompt}"}}', encoding="utf-8")
    return RunState(
        run_id=run_dir.name,
        run_dir=run_dir,
        popen=None,
        mode=mode,
        app_session_id=app_session_id,
        queue=None,
        started_at=started_at,
        lingering=lingering,
    )


def main() -> int:
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="ba-linger-details-runs-"))

    prov = ClaudeProvider.__new__(ClaudeProvider)
    prov._runs = {}

    # Two lingering runs for sess-A, one for sess-B, one finished (not lingering).
    prov._runs["a1"] = _make_run(tmp / "a1", prompt="watch the dev server",
                                  app_session_id="sess-A", mode="native")
    prov._runs["a2"] = _make_run(tmp / "a2", prompt="tail logs",
                                  app_session_id="sess-A", mode="manager",
                                  started_at="2026-06-30T11:00:00Z")
    prov._runs["b1"] = _make_run(tmp / "b1", prompt="other session work",
                                  app_session_id="sess-B")
    prov._runs["a3"] = _make_run(tmp / "a3", prompt="already done",
                                  app_session_id="sess-A", lingering=False)

    details = prov.lingering_run_details("sess-A")

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    check([d["run_id"] for d in details] == ["a1", "a2"],
          f"only sess-A lingering runs returned, got {[d['run_id'] for d in details]}")
    by_id = {d["run_id"]: d for d in details}
    check(by_id["a1"]["prompt"] == "watch the dev server",
          "a1 prompt read from input.json")
    check(by_id["a1"]["mode"] == "native", "a1 mode surfaced")
    check(by_id["a1"]["started_at"] == "2026-06-30T10:00:00Z",
          "a1 started_at surfaced")
    check(by_id["a2"]["mode"] == "manager", "a2 mode surfaced")
    check("target_message_id" in by_id["a1"], "target_message_id key present")
    check(prov.lingering_run_details("sess-Z") == [],
          "unknown session -> empty list")

    # Missing input.json -> empty prompt, not a crash.
    bare = tmp / "bare"
    bare.mkdir()
    prov._runs["bare"] = RunState(run_id="bare", run_dir=bare, popen=None,
                                  mode="native", app_session_id="sess-A",
                                  queue=None, lingering=True)
    bare_details = {d["run_id"]: d for d in prov.lingering_run_details("sess-A")}
    check(bare_details["bare"]["prompt"] == "",
          "missing input.json yields empty prompt, no exception")

    if failures:
        print(f"FAILED: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: lingering_run_details contract")
    return 0


if __name__ == "__main__":
    sys.exit(main())
