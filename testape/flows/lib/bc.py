"""Reusable TestApe subflow builders for the Better Agent web app.

Each function returns a FlowBuilder (NOT yet built) describing one minimal,
independently-reusable state transition. Feature flows compose them via
`fb.subflow(child_builder)`.

Conventions:
- Every builder takes `adapter_id` (build-time param). `base_url` defaults to
  APP_BASE so the same flow runs in any environment.
- Selectors prefer stable hooks: data-testid, aria-label, stable classes.
- Names are deterministic so the same subflow is shared across every feature
  flow that references it.

Environment requirement (FlowBuilder cannot set this in-flow):
- The Chrome window MUST be desktop width (>820px content). Below that the
  sidebar collapses into a closed drawer (no `session-list`) AND the composer
  sends on Enter only at desktop width — `enterIsNewline = viewport.mode !==
  "desktop"` (InputArea.tsx). CDP `Emulation.setDeviceMetricsOverride`
  (`testape emulate`) is per-CDP-session and does NOT carry into a flow run's
  own session, so resize the real OS window once after `testape chrome start`:
    testape chrome resize --port 9224 --width 1440 --height 900
- The fixed Agent Board FAB overlaps the send button, so flows send via Enter,
  not via the send button.
- Flows create fresh NATIVE sessions. Supervisor/Agent-Board modals only
  surface on supervisor sessions and are out of scope here.
"""

from testape_engine.flow_builder import FlowBuilder

APP_BASE = "http://localhost:3000"

# Stable selectors (verified against the live app).
SEL = {
    "login_username": 'input[autocomplete="username"]',
    "login_password": 'input[autocomplete="current-password"]',
    "login_submit": 'button[type="submit"]',
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

# Trivial default prompts so session-creating subflows spawn the cheapest
# possible real turn. Override at run time via the run-time variable.
DEFAULT_NEW_SESSION_PROMPT = "Reply with exactly: TESTAPE_OK"
DEFAULT_FOLLOWUP_PROMPT = "Reply with exactly: TESTAPE_FOLLOWUP"

# Auth. The bc-test tab must be authenticated before open_app can find
# session-list; run the auth__login flow once (cookie lasts 30 days). Username
# is the app's real login (from keychain). The password is NEVER authored in
# plaintext — only a keychain:// reference; the agent resolves it at the typing
# edge and self-provisions via an OS-rendered dialog on first run.
BC_USERNAME = "ofekron"
PASSWORD_KEYCHAIN_REF = "keychain://better_agent_password"


def open_app(adapter_id, base_url=APP_BASE):
    """Transition: anywhere -> app shell loaded.

    Navigates to the app root and proves the session list is visible. First
    subflow of every feature flow.
    """
    import re

    fb = FlowBuilder(name="bc__open_app", adapter_id=adapter_id, folder="bc/lib")
    fb.navigate(base_url)
    fb.assert_url(re.escape(base_url) + r"/.*", fix_allowed=False, skip_allowed=False)
    # Flat delay-then-check: assert_dom evaluates once after `delay`, it is not
    # a poll. The SPA needs a few seconds to mount; 10s covers cold starts.
    fb.assert_dom(SEL["session_list"], predicates=[{"op": "visible"}], delay=10)
    return fb


def login(
    adapter_id,
    base_url=APP_BASE,
    username=BC_USERNAME,
    password_ref=PASSWORD_KEYCHAIN_REF,
):
    """Transition: logged-out -> authenticated app shell.

    Navigates to the app root, proves the login form is showing, types the
    username and the keychain-referenced password, submits, and proves the
    session-list rendered (cookie set). The password is authored only as a
    ``keychain://`` reference; the agent resolves it at type time and prompts
    via an OS dialog on first run, so the plaintext never enters the model, the
    flow source, or the FS DB.

    Run the auth__login feature flow once to authenticate the persistent tab;
    the session cookie then lasts 30 days and every feature flow's open_app
    works directly.
    """
    fb = FlowBuilder(name="bc__login", adapter_id=adapter_id, folder="bc/lib")
    fb.navigate(base_url)
    fb.assert_dom(
        SEL["login_password"], predicates=[{"op": "visible"}], delay=10
    )
    fb.click(selector=SEL["login_username"], delay=0.3)
    fb.variable(var_id="bc_username", name="Login username", default_text=username)
    fb.click(selector=SEL["login_password"], delay=0.3)
    fb.variable(var_id="bc_password", name="Login password", default_text=password_ref)
    fb.click(selector=SEL["login_submit"], delay=0.5)
    # After submit the SPA sets the cookie and renders the shell. session-list
    # is the durable proof of "logged in" (absent on the login screen).
    fb.assert_dom(
        SEL["session_list"], predicates=[{"op": "visible"}], delay=20
    )
    return fb


def open_new_session_modal(adapter_id):
    """Transition: app shell -> new-session modal open.

    Clicks the header "New Session" button and proves the modal's prompt
    textarea is visible. Pairs with submit_new_session.
    """
    fb = FlowBuilder(
        name="bc__open_new_session_modal", adapter_id=adapter_id, folder="bc/lib"
    )
    fb.click(selector=SEL["new_session_btn"], delay=0.5)
    fb.assert_dom(SEL["modal_prompt_textarea"], predicates=[{"op": "visible"}], delay=10)
    return fb


def submit_new_session(adapter_id, prompt=DEFAULT_NEW_SESSION_PROMPT):
    """Transition: new-session modal open -> fresh session view loaded.

    Types the initial prompt (run-time variable `prompt`), clicks Create, and
    proves the session was created: modal closed, the new session is selected,
    and the in-session composer is present.
    """
    fb = FlowBuilder(
        name="bc__submit_new_session", adapter_id=adapter_id, folder="bc/lib"
    )
    fb.click(selector=SEL["modal_prompt_textarea"], delay=0.3)
    fb.variable(var_id="prompt", name="Initial prompt", default_text=prompt)
    fb.click(selector=SEL["modal_create_btn"], delay=0.5)
    fb.assert_dom(SEL["modal_prompt_textarea"], predicates=[{"op": "not_exists"}], delay=20)
    fb.assert_dom(SEL["session_selected"], predicates=[{"op": "exists"}], delay=5)
    fb.assert_dom(SEL["input_textarea"], predicates=[{"op": "visible"}], delay=5)
    return fb


def create_new_session(adapter_id, base_url=APP_BASE, prompt=DEFAULT_NEW_SESSION_PROMPT):
    """Transition: anywhere -> fresh session view loaded.

    Canonical "start a brand new session" setup: open_app -> open modal ->
    submit. Used by every flow that needs a clean session to act on.
    """
    fb = FlowBuilder(
        name="bc__create_new_session", adapter_id=adapter_id, folder="bc/lib"
    )
    fb.subflow(open_app(adapter_id, base_url))
    fb.subflow(open_new_session_modal(adapter_id))
    fb.subflow(submit_new_session(adapter_id, prompt=prompt))
    return fb


def send_message(adapter_id, prompt=DEFAULT_FOLLOWUP_PROMPT):
    """Transition: idle session view -> user message sent.

    Focuses the composer, types the prompt (run-time variable `followup`),
    and sends with Enter (desktop viewport sends on Enter). Proves the user's
    message rendered in the transcript. Does NOT wait for the assistant turn —
    pair with wait_assistant_reply.

    Note: Enter-to-send requires desktop viewport; the send button itself is
    overlapped by the fixed Agent Board FAB and cannot be clicked reliably.
    """
    fb = FlowBuilder(name="bc__send_message", adapter_id=adapter_id, folder="bc/lib")
    fb.click(selector=SEL["input_textarea"], delay=0.3)
    fb.variable(var_id="followup", name="Follow-up prompt", default_text=prompt)
    fb.press_key("Enter", delay=0.5)
    fb.assert_dom(
        SEL["user_message"], predicates=[{"op": "count_gte", "count": 1}], delay=10
    )
    return fb


def wait_assistant_reply(adapter_id, min_messages=1, timeout=180):
    """Transition: turn in flight -> turn finished (assistant replied, idle).

    Polls the transcript DOM until it settles (streaming stopped) via
    wait_for_dom_stable, then confirms the turn is idle: no interrupt/stop
    control and at least `min_messages` assistant bubbles rendered. Pass the
    prior count+1 to assert "a NEW reply" after a follow-up.
    """
    fb = FlowBuilder(
        name="bc__wait_assistant_reply", adapter_id=adapter_id, folder="bc/lib"
    )
    # wait_for_dom_stable is the FlowBuilder polling primitive (assert_dom is
    # NOT a poll). chat_messages stops mutating once the streaming turn ends.
    fb.wait_for_dom_stable(
        selector=SEL["chat_messages"], idle_ms=2500, timeout=timeout
    )
    # Confirm idle: interrupt-btn / stop-btn exist only while a turn runs.
    fb.assert_dom(SEL["interrupt_btn"], predicates=[{"op": "not_exists"}], delay=2)
    fb.assert_dom(SEL["stop_btn"], predicates=[{"op": "not_exists"}], delay=2)
    fb.assert_dom(
        SEL["assistant_message"],
        predicates=[{"op": "count_gte", "count": min_messages}],
        delay=5,
    )
    return fb


def select_session_by_index(adapter_id, index=0):
    """Transition: session view A -> session view B (by list position).

    Clicks the Nth session item in the sidebar. Used by flows that operate on
    an existing session rather than creating a new one.
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
