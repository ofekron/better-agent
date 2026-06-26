"""Reusable TestApe subflow builders for the Better Agent web app.

Each function returns a FlowBuilder (NOT yet built) describing one minimal,
independently-reusable state transition. Feature flows compose them via
`fb.subflow(child_builder)`.

Conventions:
- Every builder takes `adapter_id` and the env-swing `base_url` (build-time
  param, not a run-time variable) so the same flow runs in any environment.
- Selectors prefer stable hooks: data-testid, aria-label, stable classes.
- Names are deterministic (`stable_id=True` default) so the same subflow is
  shared across every feature flow that references it.
"""

from testape_engine.flow_builder import FlowBuilder

APP_BASE = "http://localhost:3000"

# Stable selectors (verified against the live app).
SEL = {
    "new_session_btn": '[aria-label="New Session"]',
    "modal_prompt_textarea": "textarea.ns-investigation-textarea",
    "modal_create_btn": ".modal-content .btn-primary",
    "session_list": '[data-testid="session-list"]',
    "session_item": '[data-testid="session-item"]',
    "session_selected": '[data-testid="session-list-selected"]',
    "chat_container": '[data-testid="chat-container"]',
    "chat_messages": '[data-testid="chat-messages"]',
    "input_textarea": '[data-testid="input-textarea"]',
    "send_btn": '[data-testid="send-btn"]',
    "user_message": '[data-testid="user-message"]',
    "assistant_message": '[data-testid="assistant-message"]',
    "interrupt_btn": '[data-testid="interrupt-btn"]',
    "stop_btn": '[data-testid="stop-btn"]',
}

# A trivial default prompt so session-creating subflows spawn the cheapest
# possible real turn. Override at run time via --var prompt=...
DEFAULT_NEW_SESSION_PROMPT = "Reply with exactly: TESTAPE_OK"
DEFAULT_FOLLOWUP_PROMPT = "Reply with exactly: TESTAPE_OK"


def open_app(adapter_id, base_url=APP_BASE):
    """Transition: anywhere -> app shell loaded.

    Navigates to the app root and proves the adapter is attached to a live,
    rendered Better Agent page (session list present). Used as the first
    subflow of every feature flow.
    """
    import re

    fb = FlowBuilder(name="bc__open_app", adapter_id=adapter_id, folder="bc/lib")
    fb.navigate(base_url)
    fb.assert_url(re.escape(base_url) + r"/.*", fix_allowed=False, skip_allowed=False)
    # Flat delay-then-check: the session list is a live region (timestamps,
    # running pulses) that never passes a DOM-stable poll, so we assert
    # visibility directly after giving the SPA time to mount.
    fb.assert_dom(SEL["session_list"], predicates=[{"op": "visible"}], delay=10)
    return fb


def open_new_session_modal(adapter_id):
    """Transition: app shell -> new-session modal open.

    Clicks the header "New Session" button and proves the modal's initial
    prompt textarea is visible. Pairs with submit_new_session (or any flow
    that exercises modal controls) as the precondition.
    """
    fb = FlowBuilder(
        name="bc__open_new_session_modal", adapter_id=adapter_id, folder="bc/lib"
    )
    fb.click(selector=SEL["new_session_btn"], delay=0.5)
    fb.assert_dom(
        SEL["modal_prompt_textarea"], predicates=[{"op": "visible"}], delay=10
    )
    return fb


def submit_new_session(adapter_id, prompt=DEFAULT_NEW_SESSION_PROMPT):
    """Transition: new-session modal open -> fresh session view loaded.

    Types the initial prompt (run-time variable `prompt`), clicks Create, and
    proves the session was created: modal closed, the new session is the
    selected one in the list, and the in-session composer is present.
    """
    fb = FlowBuilder(
        name="bc__submit_new_session", adapter_id=adapter_id, folder="bc/lib"
    )
    fb.click(selector=SEL["modal_prompt_textarea"], delay=0.3)
    fb.variable(var_id="prompt", name="Initial prompt", default_text=prompt)
    fb.click(selector=SEL["modal_create_btn"], delay=0.5)
    # Flat delay-then-check: chat_container is a live region during the
    # spawned turn, so a DOM-stable poll won't converge. Wait long enough
    # for Create -> backend session create -> navigate -> modal close.
    fb.assert_dom(
        SEL["modal_prompt_textarea"],
        predicates=[{"op": "not_exists"}],
        delay=20,
    )
    fb.assert_dom(
        SEL["session_selected"], predicates=[{"op": "exists"}], delay=5
    )
    fb.assert_dom(SEL["input_textarea"], predicates=[{"op": "visible"}], delay=5)
    return fb


def create_new_session(adapter_id, base_url=APP_BASE, prompt=DEFAULT_NEW_SESSION_PROMPT):
    """Transition: anywhere -> fresh session view loaded.

    Convenience composition of the two modal subflows plus open_app. This is
    the canonical "start a brand new session" setup used by every flow that
    needs a clean session to act on.
    """
    fb = FlowBuilder(
        name="bc__create_new_session", adapter_id=adapter_id, folder="bc/lib"
    )
    fb.subflow(open_app(adapter_id, base_url))
    fb.subflow(open_new_session_modal(adapter_id))
    fb.subflow(submit_new_session(adapter_id, prompt=prompt))
    return fb


def send_followup_message(adapter_id, prompt=DEFAULT_FOLLOWUP_PROMPT):
    """Transition: idle session view -> user message sent.

    Types a follow-up prompt into the in-session composer and sends it, then
    proves the user's message rendered in the transcript. Does NOT wait for
    the assistant turn to finish — pair with wait_assistant_turn_done.
    """
    fb = FlowBuilder(
        name="bc__send_followup_message", adapter_id=adapter_id, folder="bc/lib"
    )
    fb.click(selector=SEL["input_textarea"], delay=0.3)
    fb.variable(var_id="followup", name="Follow-up prompt", default_text=prompt)
    fb.click(selector=SEL["send_btn"], delay=0.5)
    fb.assert_dom(
        SEL["user_message"], predicates=[{"op": "count_gte", "count": 1}], delay=15
    )
    return fb


def wait_assistant_turn_done(adapter_id, timeout=120):
    """Transition: turn in flight -> turn finished (assistant replied, idle).

    Waits for the transcript DOM to settle and proves at least one assistant
    message rendered and no interrupt/stop control remains (turn complete).
    """
    fb = FlowBuilder(
        name="bc__wait_assistant_turn_done", adapter_id=adapter_id, folder="bc/lib"
    )
    fb.wait_for_dom_stable(
        selector=SEL["chat_messages"], idle_ms=1500, timeout=timeout
    )
    fb.assert_dom(
        SEL["assistant_message"],
        predicates=[{"op": "count_gte", "count": 1}],
        delay=15,
    )
    fb.assert_dom(
        SEL["interrupt_btn"], predicates=[{"op": "not_exists"}], delay=10
    )
    return fb


def select_session_by_index(adapter_id, index=0):
    """Transition: session view A -> session view B (by list position).

    Clicks the Nth session item in the sidebar. Used by flows that operate
    on an existing session rather than creating a new one.
    """
    fb = FlowBuilder(
        name="bc__select_session_by_index", adapter_id=adapter_id, folder="bc/lib"
    )
    # nth-of-type is acceptable here: list order is stable within a run and
    # the index is the parameter under test.
    nth = f'{SEL["session_item"]}:nth-of-type({index + 1})'
    fb.click(selector=nth, delay=0.5)
    fb.assert_dom(SEL["session_selected"], predicates=[{"op": "exists"}], delay=8)
    return fb
