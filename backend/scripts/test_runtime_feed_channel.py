"""Runtime canonical-feed advance channel tests.

Pins the runtime→BFF push contract:

  A. `RuntimeFeedChannel` fans out coalesced dirty-root sets to every
     attached subscriber, thread-safely, and drops nothing (dirty-set
     semantics, not frame queues).
  B. `canonical_runtime_journal` fires the advance observer on every
     coverage gain — `ensure_cutover`, `mirror_event` on an
     authoritative root, AND `mirror_event` on a root that has not cut
     over yet (so a feed consumer can trigger the on-demand sync).
  C. `unbind` wakes subscribers with a closed signal so WS handlers
     terminate instead of hanging.

Run with:
    cd backend && .venv/bin/python scripts/test_runtime_feed_channel.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-feed-channel-")
_TMP_HOME_PATH = Path(_TMP_HOME)

import canonical_runtime_journal as crj  # noqa: E402
from runtime_feed_channel import RuntimeFeedChannel  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{PASS if condition else FAIL}  {label}")
    if not condition:
        _failures.append(label)


def test_channel_fanout_and_coalescing() -> None:
    async def scenario() -> None:
        channel = RuntimeFeedChannel()
        channel.bind(asyncio.get_running_loop())
        first = channel.attach()
        second = channel.attach()

        def burst() -> None:
            channel.publish_advance("root-a", 1)
            channel.publish_advance("root-b", 2)
            channel.publish_advance("root-a", 3)

        thread = threading.Thread(target=burst)
        thread.start()
        thread.join()
        roots_first = await asyncio.wait_for(first.wait_drain(), timeout=5)
        roots_second = await asyncio.wait_for(second.wait_drain(), timeout=5)
        check("burst coalesces to one dirty-set per subscriber",
              roots_first == {"root-a", "root-b"} and roots_second == {"root-a", "root-b"})

        channel.detach(second)
        channel.publish_advance("root-c", 4)
        await asyncio.sleep(0)
        roots_first = await asyncio.wait_for(first.wait_drain(), timeout=5)
        check("detached subscriber receives nothing; attached receives root-c",
              roots_first == {"root-c"} and not second.dirty)

        channel.unbind()
        await asyncio.sleep(0)
        try:
            await asyncio.wait_for(first.wait_drain(), timeout=5)
            check("unbind closes subscribers", False)
        except ConnectionError:
            check("unbind closes subscribers", True)

    asyncio.run(scenario())


def test_journal_fires_advance_observer() -> None:
    calls: list[tuple[str, int]] = []
    crj.set_advance_observer(lambda root, seq: calls.append((root, seq)))
    try:
        journal = crj.CanonicalRuntimeJournal(
            catalog_path=_TMP_HOME_PATH / "feed-authority.sqlite",
        )
        session = {"id": "feed-root", "messages": []}
        row = {
            "root_id": "feed-root", "sid": "feed-root", "seq": 1,
            "type": "agent_message", "source": "provider_stream",
            "msg_id": "m1", "turn_id": "m1",
            "data": {"uuid": "u1", "type": "assistant",
                     "message": {"role": "assistant",
                                 "content": [{"type": "text", "text": "hi"}]}},
        }
        journal.ensure_cutover("feed-root", rows=[row], session=session)
        check("ensure_cutover fires advance observer",
              calls[-1:] == [("feed-root", 1)])

        journal.mirror_event(
            root_id="feed-root", sid="feed-root", seq=2,
            event_type="agent_message",
            data={"uuid": "u2", "type": "assistant",
                  "message": {"role": "assistant",
                              "content": [{"type": "text", "text": "again"}]}},
            source="provider_stream", msg_id="m1", event_id="u2", turn_id="m1",
        )
        check("authoritative mirror_event fires advance observer",
              calls[-1:] == [("feed-root", 2)])

        journal.mirror_event(
            root_id="never-cutover", sid="never-cutover", seq=1,
            event_type="agent_message", data={"uuid": "u3"},
            source="provider_stream", msg_id=None, event_id="u3", turn_id=None,
        )
        check("pre-cutover mirror_event still announces the root",
              calls[-1:] == [("never-cutover", 1)])
    finally:
        crj.set_advance_observer(None)


if __name__ == "__main__":
    try:
        test_channel_fanout_and_coalescing()
        test_journal_fires_advance_observer()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if _failures:
        print(f"{len(_failures)} test(s) FAILED")
        sys.exit(1)
    print("all feed channel tests passed")
