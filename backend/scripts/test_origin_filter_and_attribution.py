"""Locks the double-writer / backfill attribution fixes:

1. ClaudeProvider._accept_resumed_line — origin filter for a resumed run
   tailing a session jsonl shared with a lingering babysitter instance:
   own prompt chain accepted, foreign monitor turns rejected, metadata
   passes, subagent lines admitted via tool ids, compaction root fails
   open.
2. turn_manager._attempt_has_assistant_reply + ClaudeProvider.
   has_lingering_sibling — forwarded-but-unanswered detection inputs.
3. event_journal parentUuid rung — non-sidechain chain inheritance with
   real-user-prompt reset (unit-level via _is_real_user_prompt and
   _owner_for_parent_uuid).
4. render_tree_hydrate._bracket_orphan_rows — backfill-inverted windows
   must neither drop orphans nor duplicate them across messages.

Run: python backend/scripts/test_origin_filter_and_attribution.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _test_home
_test_home.isolate("origin-filter-test-")

import asyncio  # noqa: E402
from pathlib import Path  # noqa: E402

from provider_claude import ClaudeProvider, RunState  # noqa: E402
from turn_manager import _attempt_has_assistant_reply  # noqa: E402
from render_tree_hydrate import _bracket_orphan_rows  # noqa: E402
from event_journal import EventJournalWriter as _EventJournal  # noqa: E402


FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


def _provider() -> ClaudeProvider:
    return ClaudeProvider({
        "id": "test-prov", "name": "t", "kind": "claude",
        "mode": "subscription", "base_url": "", "config_dir": "",
    })


def _rs(**kw) -> RunState:
    defaults = dict(
        run_id="run-A", run_dir=Path("."), popen=None, mode="native",
        app_session_id="app-1", queue=asyncio.Queue(),
        resume_requested=True, prompt_text="what is the status?",
        origin_filter_active=True,
    )
    defaults.update(kw)
    return RunState(**defaults)


def user_line(uuid, parent, text, prompt_id=None, **kw):
    d = {
        "type": "user", "uuid": uuid, "parentUuid": parent,
        "isSidechain": False,
        "message": {"role": "user", "content": text},
    }
    if prompt_id:
        d["promptId"] = prompt_id
    d.update(kw)
    return d


def asst_line(uuid, parent, text="ok", blocks=None, **kw):
    content = blocks if blocks is not None else [{"type": "text", "text": text}]
    d = {
        "type": "assistant", "uuid": uuid, "parentUuid": parent,
        "isSidechain": False,
        "message": {"role": "assistant", "content": content},
    }
    d.update(kw)
    return d


# ---------------------------------------------------------------------------
# 1. Origin filter — incident scenario
# ---------------------------------------------------------------------------
prov = _provider()
rs = _rs()

# Metadata (no uuid) always passes.
check("meta queue-operation accepted",
      prov._accept_resumed_line(rs, {"type": "queue-operation"}))
check("meta file-history accepted",
      prov._accept_resumed_line(rs, {"type": "file-history-snapshot"}))

# Foreign monitor turn BEFORE our anchor: rejected.
foreign_notif = user_line("f-notif", "f-prev", "<task-notification>row done</task-notification>", prompt_id="pid-F")
check("pre-anchor foreign notification rejected",
      not prov._accept_resumed_line(rs, foreign_notif))
foreign_reply = asst_line("f-reply", "f-notif", "Row 52 passed")
check("pre-anchor foreign reply rejected",
      not prov._accept_resumed_line(rs, foreign_reply))

# Stale synthetic closer of a previous turn (has uuid + foreign parent).
stale_closer = asst_line("stale-1", "old-user", "No response requested.")
stale_closer["message"]["model"] = "<synthetic>"
check("stale synthetic closer rejected",
      not prov._accept_resumed_line(rs, stale_closer))

# Our own prompt line anchors (text matches prompt_text).
anchor = user_line("u-anchor", "f-reply", "what is the status?\n\n<bc-todo-reminder>x</bc-todo-reminder>", prompt_id="pid-A")
check("own prompt anchors", prov._accept_resumed_line(rs, anchor))
check("anchor recorded", rs.origin_anchor_uuid == "u-anchor"
      and rs.origin_anchor_prompt_id == "pid-A")

# Our answer chain accepted; tool ids harvested.
own_asst = asst_line("a-1", "u-anchor", blocks=[
    {"type": "text", "text": "Checking."},
    {"type": "tool_use", "id": "tool-1", "name": "Task", "input": {}},
])
check("own assistant accepted", prov._accept_resumed_line(rs, own_asst))
check("tool id harvested", "tool-1" in rs.origin_accepted_tool_ids)

# Our tool_result user line: same promptId → accepted.
own_result = user_line("u-res", "a-1", "", prompt_id="pid-A")
own_result["message"]["content"] = [
    {"type": "tool_result", "tool_use_id": "tool-1", "content": "done"},
]
check("own tool_result accepted (promptId)",
      prov._accept_resumed_line(rs, own_result))

# Subagent (sidechain) line: parentUuid rooted in its OWN file, but
# parent_tool_use_id points at our accepted Task tool_use.
side_root = asst_line("s-1", None, "side hello",
                      isSidechain=True, parent_tool_use_id="tool-1")
check("subagent line accepted via tool id",
      prov._accept_resumed_line(rs, side_root))
side_foreign = asst_line("s-f", None, "foreign side",
                         isSidechain=True, parent_tool_use_id="tool-OTHER")
check("foreign subagent line rejected",
      not prov._accept_resumed_line(rs, side_foreign))

# Interleaved foreign monitor turn AFTER anchor: rejected (has parent).
foreign2 = user_line("f-n2", "f-reply", "<task-notification>next</task-notification>", prompt_id="pid-F")
check("post-anchor foreign notification rejected",
      not prov._accept_resumed_line(rs, foreign2))
foreign2_reply = asst_line("f-r2", "f-n2", "Row 53 passed")
check("post-anchor foreign reply rejected",
      not prov._accept_resumed_line(rs, foreign2_reply))

# Compaction: brand-new null-parent non-meta root → fail open.
compact_root = user_line("c-root", None, "continued after compaction")
check("compaction root fails open",
      prov._accept_resumed_line(rs, compact_root))
check("filter disabled after fail-open", not rs.origin_filter_active)
check("post-fail-open foreign accepted",
      prov._accept_resumed_line(rs, asst_line("f-r3", "f-n2", "Row 54")))

# Anchor soft-match: mismatching text still anchors (fail-safe).
rs2 = _rs(prompt_text="completely different prompt")
odd = user_line("u-odd", None, "[file attachment preamble] whatever", prompt_id="pid-B")
check("mismatched first real user line still anchors",
      prov._accept_resumed_line(rs2, odd) and rs2.origin_anchor_uuid == "u-odd")

# ---------------------------------------------------------------------------
# 2. Forwarded-but-unanswered inputs
# ---------------------------------------------------------------------------
check("no reply on empty attempt", not _attempt_has_assistant_reply([]))
check("queue-op is not a reply", not _attempt_has_assistant_reply(
    [{"type": "agent_message", "data": {"type": "queue-operation"}}]))
synth = {"type": "agent_message", "data": {
    "type": "assistant",
    "message": {"model": "<synthetic>", "content": [
        {"type": "text", "text": "No response requested."}]},
}}
check("synthetic marker is not a reply", not _attempt_has_assistant_reply([synth]))
real = {"type": "agent_message", "data": {
    "type": "assistant",
    "message": {"content": [{"type": "text", "text": "hi"}]},
}}
check("real text is a reply", _attempt_has_assistant_reply([real]))

prov2 = _provider()
rs_active = _rs(run_id="run-new")
rs_active.session_id = "native-1"
rs_linger = _rs(run_id="run-linger", origin_filter_active=False)
rs_linger.session_id = "native-1"
rs_linger.lingering = True
prov2._runs = {"run-new": rs_active, "run-linger": rs_linger}
check("lingering sibling detected", prov2.has_lingering_sibling("run-new"))
rs_linger.lingering = False
check("no sibling when not lingering", not prov2.has_lingering_sibling("run-new"))
rs_linger.lingering = True
rs_linger.session_id = "native-2"
check("no sibling across different sids", not prov2.has_lingering_sibling("run-new"))

# ---------------------------------------------------------------------------
# 3. parentUuid ladder rung
# ---------------------------------------------------------------------------
jr = _EventJournal.__new__(_EventJournal)
payload_notif = {"type": "user", "message": {"role": "user", "content": "<task-notification>x</task-notification>"}}
payload_prompt = {"type": "user", "message": {"role": "user", "content": "run another one"}}
payload_toolres = {"type": "user", "message": {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "t1"}]}}
payload_asst = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "text", "text": "No response requested."}]}}
check("real prompt detected", _EventJournal._is_real_user_prompt(payload_prompt))
check("notification is not a real prompt",
      not _EventJournal._is_real_user_prompt(payload_notif))
check("tool_result is not a real prompt",
      not _EventJournal._is_real_user_prompt(payload_toolres))
check("assistant is not a real prompt",
      not _EventJournal._is_real_user_prompt(payload_asst))


class _Ev:
    def __init__(self, data, root_id="root-1"):
        self.data = data
        self.root_id = root_id


jr._event_messages = {("root-1", "parent-u"): "msg-5"}
ev_child = _Ev({"type": "assistant", "uuid": "child-a", "parentUuid": "parent-u",
                "isSidechain": False,
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "No response requested."}]}})
check("non-sidechain child inherits parent owner",
      jr._owner_for_parent_uuid(ev_child) == "msg-5")
ev_prompt = _Ev({"type": "user", "uuid": "new-prompt", "parentUuid": "parent-u",
                 "isSidechain": False,
                 "message": {"role": "user", "content": "next question"}})
check("real prompt does NOT inherit",
      jr._owner_for_parent_uuid(ev_prompt) is None)
ev_side = _Ev({"type": "assistant", "uuid": "side-a", "parentUuid": "parent-u",
               "isSidechain": True,
               "message": {"role": "assistant", "content": []}})
check("sidechain excluded from rung",
      jr._owner_for_parent_uuid(ev_side) is None)

# ---------------------------------------------------------------------------
# 4. Orphan bracketing under backfill
# ---------------------------------------------------------------------------
# Live rows: msgA seqs 10-20, msgB seqs 30-40. Backfill appended msgA
# history rows at seqs 100-110 (inverting the old floor/ceil windows).
msgs = [(1, {"id": "msgA"}), (3, {"id": "msgB"})]
by_msg = {
    "msgA": [{"seq": 10}, {"seq": 20}, {"seq": 100}, {"seq": 110}],
    "msgB": [{"seq": 30}, {"seq": 40}],
}
orphans = [{"seq": 15}, {"seq": 35}, {"seq": 105}, {"seq": 500}, {"seq": 5}]
out = _bracket_orphan_rows(msgs, by_msg, orphans)
placed = {row["seq"]: mid for mid, rows in out.items() for row in rows}
check("orphan between msgA live rows -> msgA", placed.get(15) == "msgA")
check("orphan between msgB rows -> msgB", placed.get(35) == "msgB")
check("backfill-window orphan -> msgA (not dropped)", placed.get(105) == "msgA")
check("trailing orphan -> nearest preceding (msgA backfill)", placed.get(500) == "msgA")
check("orphan before all named rows -> first message", placed.get(5) == "msgA")
total_placed = sum(len(rows) for rows in out.values())
check("every orphan placed exactly once", total_placed == len(orphans))

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES: {FAILURES}")
    sys.exit(1)
print("ALL PASS")
