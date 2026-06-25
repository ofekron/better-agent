"""Functional tests for the shared team chat store.

Exercises the full lifecycle (create/read/post/delete), per-reader cursor
independence, the empty-message rejection rule, and path-traversal guarding.
Runs against an isolated BETTER_AGENT_HOME so it can never touch real state.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home

_test_home.isolate("bc-test-chat-store-")

import chat_store as c  # noqa: E402


def _expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


# create + post + self-read
c.create_chat(chat_id="ops", created_by="A", name="Ops")
a1 = c.post_and_read(chat_id="ops", reader_id="A", message="hello")
_expect(a1["count"] == 1 and a1["new_messages"][0]["text"] == "hello", "self-read after post")
_expect(a1["new_messages"][0]["sender_id"] == "A", "sender stamped")

# independent reader cursor: B sees A's message, A does not re-see on re-read
b1 = c.post_and_read(chat_id="ops", reader_id="B", message="")
_expect(b1["count"] == 1, "B sees A's message on first read")
a2 = c.post_and_read(chat_id="ops", reader_id="A", message="")
_expect(a2["count"] == 0, "A re-read yields nothing new")

# only-new-since-cursor: A posts, B reads -> only the new one
a3 = c.post_and_read(chat_id="ops", reader_id="A", message="second")
b2 = c.post_and_read(chat_id="ops", reader_id="B", message="")
_expect(b2["count"] == 1 and b2["new_messages"][0]["text"] == "second", "B sees only new message")

caught = c.create_chat(
    chat_id="caught-up",
    created_by="A",
    name="Caught Up",
    new_readers_see_history=False,
)
_expect(caught["new_readers_see_history"] is False, "caught-up setting stored")
c.post_and_read(chat_id="caught-up", reader_id="A", message="before B")
b_first = c.post_and_read(chat_id="caught-up", reader_id="B", message="")
_expect(b_first["count"] == 0 and b_first["cursor"] == 1, "B starts caught up")
c_history = c.read_history(chat_id="caught-up")
_expect(c_history["count"] == 1, "history read returns prior message")
_expect(c_history["messages"][0]["text"] == "before B", "history read includes prior message")
b_after_history = c.post_and_read(chat_id="caught-up", reader_id="B", message="")
_expect(b_after_history["count"] == 0 and b_after_history["cursor"] == 1, "history read does not move unread cursor")
c.post_and_read(chat_id="caught-up", reader_id="A", message="after B")
b_next = c.post_and_read(chat_id="caught-up", reader_id="B", message="")
_expect(
    b_next["count"] == 1 and b_next["new_messages"][0]["text"] == "after B",
    "B sees messages after first read",
)
c_override = c.post_and_read(
    chat_id="caught-up",
    reader_id="C",
    message="",
    history_mode=c.HISTORY_MODE_UNREAD,
)
_expect(c_override["count"] == 2, "first read can override default to unread history")
d_override = c.post_and_read(
    chat_id="ops",
    reader_id="D",
    message="",
    history_mode=c.HISTORY_MODE_CAUGHT_UP,
)
_expect(d_override["count"] == 0 and d_override["cursor"] == 2, "first read can override default to caught up")

c.create_chat(chat_id="caught-up-post", created_by="A", new_readers_see_history=False)
c.post_and_read(chat_id="caught-up-post", reader_id="A", message="seed")
c_first_post = c.post_and_read(chat_id="caught-up-post", reader_id="C", message="from C")
_expect(c_first_post["count"] == 1, "first-time poster sees own post only")
_expect(c_first_post["new_messages"][0]["text"] == "from C", "first post returned")
listed = {chat["id"]: chat for chat in c.list_chats()}
_expect(listed["ops"]["new_readers_see_history"] is True, "default history setting listed")
_expect(listed["ops"]["sender_policy"] == c.SENDER_POLICY_OPEN, "default sender policy listed")
_expect(listed["ops"]["sender_ids"] == [], "default sender ids listed")
_expect(listed["caught-up"]["new_readers_see_history"] is False, "caught-up setting listed")

# empty / whitespace messages are never stored
before = len(c.post_and_read(chat_id="ops", reader_id="A", message="   ")["new_messages"])
_expect(before == 0, "whitespace-only message is not stored")

allow_chat = c.create_chat(
    chat_id="allow-room",
    created_by="owner",
    sender_policy=c.SENDER_POLICY_ALLOWLIST,
    sender_ids=["allowed"],
)
_expect(allow_chat["sender_policy"] == c.SENDER_POLICY_ALLOWLIST, "allowlist policy stored")
c.post_and_read(chat_id="allow-room", reader_id="allowed", message="allowed post")
c.post_and_read(chat_id="allow-room", reader_id="owner", message="owner post")
try:
    c.post_and_read(chat_id="allow-room", reader_id="blocked", message="blocked post")
    raise AssertionError("sender outside allowlist should fail")
except c.ChatStoreError:
    pass
read_blocked = c.post_and_read(chat_id="allow-room", reader_id="blocked", message="")
_expect(read_blocked["count"] == 2, "blocked sender can still read")
try:
    c.set_sender_policy(
        chat_id="allow-room",
        owner_id="blocked",
        sender_policy=c.SENDER_POLICY_OPEN,
    )
    raise AssertionError("non-owner policy change should fail")
except c.ChatStoreError:
    pass

disallow = c.set_sender_policy(
    chat_id="allow-room",
    owner_id="owner",
    sender_policy=c.SENDER_POLICY_DISALLOWLIST,
    sender_ids=["blocked"],
)
_expect(disallow["sender_policy"] == c.SENDER_POLICY_DISALLOWLIST, "disallowlist policy stored")
c.post_and_read(chat_id="allow-room", reader_id="allowed", message="allowed after policy change")
try:
    c.post_and_read(chat_id="allow-room", reader_id="blocked", message="blocked after policy change")
    raise AssertionError("sender in disallowlist should fail")
except c.ChatStoreError:
    pass

open_policy = c.set_sender_policy(
    chat_id="allow-room",
    owner_id="owner",
    sender_policy=c.SENDER_POLICY_OPEN,
    sender_ids=["blocked"],
)
_expect(open_policy["sender_policy"] == c.SENDER_POLICY_OPEN, "open policy restored")
c.post_and_read(chat_id="allow-room", reader_id="blocked", message="open post")

# duplicate create fails
try:
    c.create_chat(chat_id="ops", created_by="X")
    raise AssertionError("duplicate create should fail")
except c.ChatStoreError:
    pass

# posting to a missing chat fails
try:
    c.post_and_read(chat_id="missing", reader_id="A", message="x")
    raise AssertionError("missing chat should fail")
except c.ChatStoreError:
    pass

# path traversal in chat_id is rejected
for bad in ("../escape", "a/b", "a\\b", ".."):
    try:
        c.create_chat(chat_id=bad, created_by="A")
        raise AssertionError(f"traversal id {bad!r} should be rejected")
    except c.ChatStoreError:
        pass

# delete returns existed flag and is idempotent
_expect(c.delete_chat("ops") is True, "delete existing returns True")
_expect(c.delete_chat("ops") is False, "delete missing returns False")

print("chat store tests: OK")
