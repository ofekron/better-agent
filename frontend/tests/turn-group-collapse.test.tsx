import { describe, it, expect, afterEach, beforeEach } from "vitest";
import { act, fireEvent, render, cleanup, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "../src/i18n";
import { Chat } from "../src/components/Chat";
import { TurnGroup as ProductionTurnGroup } from "../src/components/MessageBubble";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";
import { renderApp } from "./harness";
import { sessionLinkMarker } from "../src/utils/linkifyFilePaths";

// `<Chat>`/`<App>` mount an `ExtensionSlots` catalog fetch per UI slot
// (~20 of them) on mount. A test here that doesn't explicitly mock every
// URL leaves `globalThis.fetch` at whatever the PREVIOUS test restored —
// if that's the bare native fetch, one of those calls can escape to the
// real network. A real failure there gets logged via frontendLogger's
// global `window.setTimeout(send, 0)` — a real timer with no unmount
// hook, that can fire during a LATER, unrelated test in this file and
// inflate ITS OWN fetch-spy call count (see the flaky
// `toHaveBeenCalledTimes(1)` assertions below). Defaulting to a harmless
// 200 stub closes that leak at its source for every test in this file;
// tests that care about fetch still override it locally as before.
const SAFE_FETCH_STUB: typeof fetch = async () =>
  new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  globalThis.fetch = SAFE_FETCH_STUB;
});

afterEach(cleanup);

// Production now defaults a completed (non-live) turn to COLLAPSED per
// docs/chat-panel.md's render(turn) algorithm (isLive -> expanded; user
// manually extended -> expanded; else -> collapsed) — see MessageBubble's
// `defaultCollapsed`/`userToggled` state. Tests that need a specific
// starting state (independent of what the natural default happens to be
// for their fixture) force it here via the same explicit header
// interaction available to users.
function TurnGroup({
  initialExpanded,
  keepExpanded,
  ...props
}: React.ComponentProps<typeof ProductionTurnGroup> & {
  /** Force the group to this expanded/collapsed state once, via the same
   * header click a real user would make, the first time the header is
   * found. Forcing via a real click flips production's `userToggled`
   * whenever a click is actually needed, so later renders (and the
   * component's own state) are left alone — a test's own subsequent
   * `fireEvent.click` is never fought. */
  initialExpanded?: boolean;
  /** Keep the top-level group pinned open on every render, overriding the
   * component's own auto-collapse-when-not-running default. Only needed
   * when a test's initial state already matches (so no click occurs to
   * flip `userToggled`) and a later rerender's prop change would otherwise
   * let the natural default re-collapse the group — e.g. observing a
   * NESTED worker/sub-agent panel's own collapse once its run finishes. */
  keepExpanded?: boolean;
}) {
  const rootRef = React.useRef<HTMLDivElement>(null);
  const forcedOnceRef = React.useRef(false);

  // A plain `useEffect` (post-paint), not `useLayoutEffect`: dispatching a
  // click synchronously from within a layout effect re-enters React's own
  // commit phase and the resulting state update is silently dropped (seen
  // empirically — the click fires but never produces a second render).
  // Post-paint effects run in their own task, outside the triggering
  // commit, so the click's state update lands normally.
  React.useEffect(() => {
    if (initialExpanded === undefined || forcedOnceRef.current) return;
    const header = rootRef.current?.querySelector<HTMLButtonElement>(
      ".message-box-header-main",
    );
    if (!header) return;
    forcedOnceRef.current = true;
    const isExpanded = header.getAttribute("aria-expanded") === "true";
    if (isExpanded !== initialExpanded) header.click();
  });

  // `keepExpanded` can't be a plain effect keyed off the WRAPPER's own
  // renders: the group's auto-collapse fires from the CHILD's internal
  // `useEffect` (`setCollapsed`), which re-renders only the child, never
  // the wrapper — so a wrapper effect with no deps never gets a second
  // chance to see the flip and correct it (confirmed empirically: the
  // wrapper's effect ran exactly once per `rerender()` call and missed
  // the child's own later state transition entirely). A MutationObserver
  // watches the actual DOM regardless of which component re-rendered it.
  React.useEffect(() => {
    if (!keepExpanded) return;
    const root = rootRef.current;
    if (!root) return;
    const enforce = () => {
      const header = root.querySelector<HTMLButtonElement>(
        ".message-box-header-main",
      );
      if (header && header.getAttribute("aria-expanded") !== "true") {
        header.click();
      }
    };
    enforce();
    const observer = new MutationObserver(enforce);
    observer.observe(root, {
      attributes: true,
      attributeFilter: ["aria-expanded"],
      subtree: true,
    });
    return () => observer.disconnect();
  }, [keepExpanded]);

  return (
    <div ref={rootRef} className="turn-group-test-host">
      <ProductionTurnGroup {...props} />
    </div>
  );
}

describe("TurnGroup collapsed interrupted indicator", () => {
  it("renders a collapsed assistant session marker as a native link", () => {
    const targetId = "6d0a35c1-b3fc-4ed5-96df-e92e6b2c74cb";
    const marker = sessionLinkMarker(targetId, "Referenced Target");
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u-marker", content: "open it" })}
        responseMessage={makeAssistantMsg({ id: "a-marker", content: marker })}
        initialExpanded={false}
        orchestrationMode="native"
      />,
    );

    const summary = container.querySelector(".collapse-summary");
    expect(summary?.querySelector(`a[href="/s/${targetId}"]`)).not.toBeNull();
    expect(summary?.textContent).not.toContain("[[ba-session:");
  });

  it("collapses a completed chat group by default", async () => {
    const realFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async () =>
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ) as unknown as typeof fetch;
    const userMessage = makeUserMsg({ id: "u1", content: "latest prompt" });
    const assistantMessage = makeAssistantMsg({
      id: "a1",
      content: "finished reply",
      isStreaming: false,
    });

    try {
      const { container } = render(
        <Chat
          messages={[userMessage, assistantMessage]}
          pendingMessages={[]}
          runs={[]}
          streamingEvents={[]}
          isStreaming={false}
          isStopping={false}
          streamingLoadPhase={null}
          onSend={() => true}
          disabled={false}
          session={makeSession()}
          draft=""
          onDraftChange={() => {}}
          queuedPrompt={null}
          onPromoteQueued={() => {}}
        />,
      );

      await waitFor(() => {
        expect(container.querySelector(".user-message-box > .message-box-body")).not.toBeNull();
        expect(container.querySelector(".assistant-message .message-content")).toBeNull();
        expect(container.querySelector(".collapse-summary")?.textContent).toContain("finished reply");
        expect(container.querySelector(".collapse-arrow")?.textContent).toBe("▶");
      });
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it("collapses a completed assistant response by default even when later turns exist", async () => {
    const realFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async () =>
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ) as unknown as typeof fetch;

    try {
      const { container } = render(
        <Chat
          messages={[
            makeUserMsg({ id: "u1", content: "older prompt" }),
            makeAssistantMsg({ id: "a1", content: "older reply", isStreaming: false }),
            makeUserMsg({ id: "u2", content: "latest prompt" }),
            makeAssistantMsg({ id: "a2", content: "streaming reply", isStreaming: true }),
          ]}
          pendingMessages={[]}
          runs={[{ run_id: "run-1", kind: "manager", target_message_id: "a2", pid: null }]}
          streamingEvents={[]}
          isStreaming
          isStopping={false}
          streamingLoadPhase={null}
          onSend={() => true}
          disabled={false}
          session={makeSession()}
          draft=""
          onDraftChange={() => {}}
          queuedPrompt={null}
          onPromoteQueued={() => {}}
        />,
      );

      const firstGroup = container.querySelector<HTMLElement>('[data-message-id="u1"]')?.closest(".turn-group");
      expect(firstGroup).not.toBeNull();
      expect(firstGroup!.querySelector('.user-message-box > .message-box-body')).not.toBeNull();
      expect(firstGroup!.querySelector('.assistant-message .message-content')).toBeNull();
      expect(firstGroup!.querySelector('.collapse-arrow')?.textContent).toBe("▶");
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it("keeps the previous assistant response collapsed by default when a failed turn is appended", () => {
    const firstTurn = [
      makeUserMsg({ id: "u1", content: "working prompt" }),
      makeAssistantMsg({ id: "a1", content: "working reply", isStreaming: false }),
    ];
    const props = {
      pendingMessages: [],
      runs: [],
      streamingEvents: [],
      isStreaming: false,
      isStopping: false,
      streamingLoadPhase: null,
      onSend: () => true,
      disabled: false,
      session: makeSession(),
      draft: "",
      onDraftChange: () => {},
      queuedPrompt: null,
      onPromoteQueued: () => {},
    } satisfies Partial<React.ComponentProps<typeof Chat>>;
    const { container, rerender } = render(<Chat {...props} messages={firstTurn} />);

    const firstGroupBefore = container.querySelector<HTMLElement>('[data-message-id="u1"]')?.closest(".turn-group");
    expect(firstGroupBefore).not.toBeNull();
    expect(firstGroupBefore!.querySelector('.assistant-message .message-content')).toBeNull();
    expect(firstGroupBefore!.querySelector('.collapse-arrow')?.textContent).toBe("▶");

    rerender(
      <Chat
        {...props}
        messages={[
          ...firstTurn,
          makeUserMsg({
            id: "u2",
            content: "failed prompt",
            status: "error",
            errorText: "unsupported provider config schema",
          }),
        ]}
      />,
    );

    const firstGroup = container.querySelector<HTMLElement>('[data-message-id="u1"]')?.closest(".turn-group");
    expect(firstGroup).not.toBeNull();
    expect(firstGroup!.querySelector('.assistant-message .message-content')).toBeNull();
    expect(firstGroup!.querySelector('.collapse-arrow')?.textContent).toBe("▶");
  });

  it("preserves an explicit manual expand while later failed and successful turns are appended", () => {
    const firstTurn = [
      makeUserMsg({ id: "u1", content: "working prompt" }),
      makeAssistantMsg({ id: "a1", content: "working reply", isStreaming: false }),
    ];
    const props = {
      pendingMessages: [],
      runs: [],
      streamingEvents: [],
      isStreaming: false,
      isStopping: false,
      streamingLoadPhase: null,
      onSend: () => true,
      disabled: false,
      session: makeSession(),
      draft: "",
      onDraftChange: () => {},
      queuedPrompt: null,
      onPromoteQueued: () => {},
    } satisfies Partial<React.ComponentProps<typeof Chat>>;
    const { container, rerender } = render(<Chat {...props} messages={firstTurn} />);
    expect(container.querySelector('[data-message-id="a1"] .message-content')).toBeNull();
    const firstHeader = screen.getByRole("button", { name: /User/i });
    fireEvent.click(firstHeader);
    expect(container.querySelector('[data-message-id="a1"] .message-content')).not.toBeNull();

    const failedTurn = makeUserMsg({
      id: "u2",
      content: "failed prompt",
      status: "error",
      errorText: "unsupported provider config schema",
    });
    rerender(<Chat {...props} messages={[...firstTurn, failedTurn]} />);
    expect(container.querySelector('[data-message-id="a1"] .message-content')).not.toBeNull();

    rerender(
      <Chat
        {...props}
        messages={[
          ...firstTurn,
          failedTurn,
          makeUserMsg({ id: "u3", content: "later prompt" }),
          makeAssistantMsg({ id: "a3", content: "later reply", isStreaming: false }),
        ]}
      />,
    );
    expect(container.querySelector('[data-message-id="a1"] .message-content')).not.toBeNull();
  });

  it("keeps the user prompt body visible when the assistant response is manually collapsed", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "latest prompt" })}
        responseMessage={makeAssistantMsg({ id: "a1", content: "final reply" })}
        initialExpanded={false}
        orchestrationMode="manager"
      />,
    );

    expect(container.querySelector(".user-message-box > .message-box-body")).not.toBeNull();
    expect(container.textContent).toContain("latest prompt");

    // The group chevron never folds the prompt text.
    fireEvent.click(screen.getByRole("button", { name: /User/i }));
    expect(container.querySelector(".user-message-box > .message-box-body")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /User/i }));
    expect(container.querySelector(".user-message-box > .message-box-body")).not.toBeNull();

    // Only the prompt's own chevron folds it, independently of the group.
    const promptToggle = container.querySelector<HTMLElement>(".prompt-collapse-toggle");
    expect(promptToggle).not.toBeNull();
    fireEvent.click(promptToggle!);
    expect(container.querySelector(".user-message-box > .message-box-body")).toBeNull();
    expect(container.querySelector(".message-box-collapsed-body")?.textContent).toContain("latest prompt");
    fireEvent.click(promptToggle!);
    expect(container.querySelector(".user-message-box > .message-box-body")).not.toBeNull();
  });

  it("collapses the latest assistant body when manually collapsed", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "latest prompt" })}
        responseMessage={makeAssistantMsg({ id: "a1", content: "final reply" })}
        initialExpanded={true}
        orchestrationMode="manager"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /User/i }));

    expect(container.querySelector(".user-message-box > .message-box-body")).not.toBeNull();
    expect(container.querySelector(".assistant-message .message-content")).toBeNull();
    expect(container.textContent).toContain("final reply");
    expect(container.querySelector(".collapse-arrow")?.textContent).toBe("▶");
  });

  it("auto-collapses a live latest group once the turn finishes", async () => {
    const userMessage = makeUserMsg({ id: "u1", content: "latest prompt" });
    const runningAssistant = makeAssistantMsg({
      id: "a1",
      content: "streaming reply",
      isStreaming: true,
    });
    const finishedAssistant = {
      ...runningAssistant,
      isStreaming: false,
      content: "finished reply",
    };

    const props = {
      pendingMessages: [],
      streamingEvents: [],
      isStopping: false,
      streamingLoadPhase: null,
      onSend: () => true,
      disabled: false,
      session: makeSession(),
      draft: "",
      onDraftChange: () => {},
      queuedPrompt: null,
      onPromoteQueued: () => {},
    } satisfies Partial<React.ComponentProps<typeof Chat>>;

    const { container, rerender } = render(
      <Chat
        {...props}
        messages={[userMessage, runningAssistant]}
        runs={[{ run_id: "run-1", kind: "manager", target_message_id: "a1", pid: null }]}
        isStreaming
      />,
    );

    expect(container.querySelector(".assistant-message .message-content")).not.toBeNull();
    expect(container.querySelector(".collapse-arrow")?.textContent).toBe("▼");

    rerender(
      <Chat
        {...props}
        messages={[userMessage, finishedAssistant]}
        runs={[]}
        isStreaming={false}
      />,
    );

    await waitFor(() => {
      expect(container.querySelector(".user-message-box > .message-box-body")).not.toBeNull();
      expect(container.querySelector(".assistant-message .message-content")).toBeNull();
      expect(container.querySelector(".collapse-summary")?.textContent).toContain("finished reply");
      expect(container.querySelector(".collapse-arrow")?.textContent).toBe("▶");
    });
  });

  it("auto-collapses the active group by default after terminal websocket frames", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const userMessage = makeUserMsg({ id: "u1", content: "live prompt", seq: 0 });
    const assistantMessage = makeAssistantMsg({
      id: "a1",
      content: "streaming reply",
      seq: 1,
      isStreaming: true,
    });

    h.emitMany([
      { type: "turn_start", data: { app_session_id: session.id, manager_session_id: "agent-1" } },
      { type: "messages_replay", data: { app_session_id: session.id, messages: [userMessage, assistantMessage] } },
      {
        type: "run_state",
        data: {
          app_session_id: session.id,
          runs: [{ run_id: "run-1", kind: "manager", target_message_id: "a1", pid: null }],
        },
      },
    ]);
    await h.flush();

    expect(h.$('[data-testid="assistant-message"][data-message-id="a1"] .message-content')).not.toBeNull();

    const finalAssistantMessage = {
      ...assistantMessage,
      content: "final reply",
      isStreaming: false,
      omitted_payloads: { events: { revision: "rev-1", count: 1 } },
      events: undefined,
    };
    // `turn_complete` also fires a background `session_reconciled` REST
    // refetch (see App.tsx's `onTurnTerminal`). The harness's emitMany
    // keeps the mock backend's REST store in sync with WS-delivered
    // `messages_delta` frames, so that refetch converges on what WS
    // already applied instead of dropping it.
    h.emitMany([
      { type: "messages_delta", data: { app_session_id: session.id, messages: [finalAssistantMessage] } },
      { type: "turn_complete", data: { app_session_id: session.id, session_id: "agent-1", success: true } },
      { type: "run_state", data: { app_session_id: session.id, runs: [] } },
    ]);
    await h.flush();

    expect(h.$('[data-testid="assistant-message"][data-message-id="a1"] .message-content')).toBeNull();
    expect(h.raw.container.textContent).toContain("final reply");
    expect(h.$(".collapse-arrow")?.textContent).toBe("▶");
    h.unmount();
  });

  it("keeps a completed latest group expanded while session monitoring marks it running", async () => {
    // `sessionRunning` (session.is_running, driven by monitoring_state !==
    // "stopped") is deliberately wired into ONLY the latest turn group's
    // `isRunning` (see Chat.tsx: `sessionRunning={g.isLatest ? sessionRunning
    // || showPendingPromptAsRunning : false}`) as a safety net against a race
    // where the session-level signal arrives before this group's own
    // `runs`/`isStreaming` catch up. That's a DIFFERENT signal from
    // chat-panel.md's per-turn `isLive`, and intentionally keeps the latest
    // group expanded (not collapsed-by-default) while it's live.
    const session = makeSession({
      messages: [
        makeUserMsg({ id: "u1", content: "done", seq: 0 }),
        makeAssistantMsg({ id: "a1", content: "finished reply", seq: 1, isStreaming: false }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "session_monitoring_changed",
      data: {
        session_id: session.id,
        monitoring_state: "active",
        cwd: session.cwd,
        node_id: session.node_id ?? "primary",
      },
    });
    await h.flush();

    expect(h.$('[data-testid="user-message"][data-message-id="u1"] > .message-box-body')).not.toBeNull();
    expect(h.$('[data-testid="assistant-message"][data-message-id="a1"] .message-content')).not.toBeNull();
    expect(h.raw.container.textContent).toContain("finished reply");
    expect(h.$(".collapse-arrow")?.textContent).toBe("▼");
    h.unmount();
  });

  it("shows the Interrupted box when the group is collapsed", () => {
    const userMessage = makeUserMsg({ id: "u1", content: "do a thing" });
    const assistantMessage = makeAssistantMsg({
      id: "a1",
      content: "partial reply",
      stopped_at: new Date("2024-01-01T10:00:00Z").toISOString(),
      interrupted_by_msg_id: "u2",
    });

    const { container } = render(
      <TurnGroup
        initiatorMessage={userMessage}
        responseMessage={assistantMessage}
        initialExpanded={false}
        orchestrationMode="manager"
      />,
    );

    // Collapsed: the assistant message body is not mounted...
    expect(container.querySelector('[data-message-id="a1"] .message-content')).toBeNull();
    // ...but the Interrupted indicator must still be present.
    const indicator = container.querySelector(".stopped-indicator");
    expect(indicator).not.toBeNull();
    expect(indicator!.textContent).toContain("Interrupted");
  });

  it("shows assistant failures when the group is collapsed", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "do a thing" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "",
          error: true,
          errorText: "interrupted",
        })}
        initialExpanded={false}
        orchestrationMode="native"
      />,
    );

    expect(container.querySelector('[data-message-id="a1"] .message-content')).toBeNull();
    const status = container.querySelector(".message-status.status-error");
    expect(status).not.toBeNull();
    expect(status!.textContent).toContain("Failed");
    expect(status!.textContent).toContain("interrupted");
  });

  it("shows the Reconnecting pill (not nothing, not 'No output') for a collapsed detached turn with no content", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "do a thing" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "",
          isDetached: true,
        })}
        initialExpanded={false}
        orchestrationMode="native"
      />,
    );

    expect(container.querySelector('[data-message-id="a1"] .message-content')).toBeNull();
    const pill = container.querySelector(".detached-pill");
    expect(pill).not.toBeNull();
    expect(pill!.textContent).toContain("Reconnecting");
    expect(container.querySelector(".collapse-summary")).toBeNull();
  });

  it("shows the Retrying pill (not nothing, not 'No output') for a collapsed rate-limited turn with no content", () => {
    const retryAt = new Date(Date.now() + 30_000).toISOString();
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "do a thing" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "",
          retrying_until: retryAt,
        })}
        initialExpanded={false}
        orchestrationMode="native"
      />,
    );

    expect(container.querySelector('[data-message-id="a1"] .message-content')).toBeNull();
    const pill = container.querySelector(".retrying-pill");
    expect(pill).not.toBeNull();
    expect(container.querySelector(".collapse-summary")).toBeNull();
  });

  it("prefers finalized assistant content over Bash event tails in collapsed preview", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "do a thing" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "Final TLDR visible to user",
          stub: {
            event_count: 3,
            last_events: [
              {
                type: "agent_message",
                data: {
                  type: "assistant",
                  message: {
                    role: "assistant",
                    content: [
                      {
                        type: "tool_use",
                        id: "tool-1",
                        name: "Bash",
                        input: { cmd: "git status" },
                      },
                    ],
                  },
                },
              },
              {
                type: "agent_message",
                data: {
                  type: "user",
                  message: {
                    role: "user",
                    content: [
                      {
                        type: "tool_result",
                        tool_use_id: "tool-1",
                        content: "Bash output should not be the preview",
                      },
                    ],
                  },
                },
              },
              {
                type: "agent_message",
                data: {
                  type: "assistant",
                  message: {
                    role: "assistant",
                    content: [
                      {
                        type: "text",
                        text: "Final TLDR visible to user",
                      },
                    ],
                  },
                },
              },
            ],
          },
        })}
        initialExpanded={false}
        orchestrationMode="native"
      />,
    );

    expect(container.textContent).toContain("Final TLDR visible to user");
    expect(container.textContent).not.toContain("Bash output should not be the preview");
  });

  it("prefers finalized assistant content over unrelated collapsed event previews", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "is it safe?" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "Final safety answer visible to user",
          events: [
            {
              type: "output",
              data: { output: "Action output should not be the collapsed preview" },
            },
          ],
        })}
        initialExpanded={false}
        orchestrationMode="native"
      />,
    );

    expect(container.textContent).toContain("Final safety answer visible to user");
    expect(container.textContent).not.toContain("Action output should not be the collapsed preview");
  });

  it("renders Markdown in finalized content when collapsed event data is stubbed", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u-md", content: "show result" })}
        responseMessage={makeAssistantMsg({
          id: "a-md",
          content: [
            "## Executive summary",
            "",
            "**Goal:** keep the result readable",
            "",
            "Use `runtime-requirements.txt`.",
            "",
            "[runtime-requirements.txt](bcfile:%2Ftmp%2Fruntime-requirements.txt)",
          ].join("\\n"),
          stub: {
            event_count: 1,
            last_events: [{ type: "turn_complete", data: {} }],
          },
        })}
        initialExpanded={false}
        orchestrationMode="native"
      />,
    );

    expect(container.querySelector(".collapse-summary")).toBeNull();
    const markdown = container.querySelector(".turn-group-children .message-box-body [data-test-md]");
    expect(markdown).not.toBeNull();
    expect(markdown?.textContent).toContain("## Executive summary");
    expect(markdown?.textContent).toContain("**Goal:** keep the result readable");
    expect(markdown?.textContent).toContain("runtime-requirements.txt");
  });

  it("renders Markdown when a finalized response has no event preview", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u-md-fallback", content: "show result" })}
        responseMessage={makeAssistantMsg({
          id: "a-md-fallback",
          content: [
            "## Executive summary",
            "",
            "**Goal:** keep the result readable",
            "",
            "```text",
            "8f2323bb9",
            "```",
            "",
            "[MessageBubble.tsx](bcfile:%2Ftmp%2FMessageBubble.tsx)",
          ].join("\\n"),
          events: [],
        })}
        initialExpanded={false}
        orchestrationMode="native"
      />,
    );

    const summary = container.querySelector(".collapse-summary");
    expect(summary).not.toBeNull();
    const markdown = summary?.querySelector(".message-box-body [data-test-md]");
    expect(markdown).not.toBeNull();
    expect(markdown?.textContent).toContain("## Executive summary");
    expect(markdown?.textContent).toContain("**Goal:** keep the result readable");
    expect(markdown?.textContent).toContain("8f2323bb9");
    expect(markdown?.textContent).toContain("MessageBubble.tsx");
  });

  it("keeps finalized assistant content when output events carry the same text", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "summarize" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "Final content mirrors output event",
          events: [
            {
              type: "output",
              data: { output: "Final content mirrors output event" },
            },
          ],
        })}
        initialExpanded={false}
        orchestrationMode="native"
      />,
    );

    expect(container.textContent).toContain("Final content mirrors output event");
  });

  it("keeps streaming collapsed groups on the latest event preview", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "keep going" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "partial assistant text",
          isStreaming: true,
          events: [
            {
              type: "output",
              data: { output: "Streaming action preview remains visible" },
            },
          ],
        })}
        initialExpanded={false}
        orchestrationMode="native"
      />,
    );

    expect(container.textContent).toContain("Streaming action preview remains visible");
  });

  it("keeps completed no-content collapsed groups on the latest event preview", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "run action" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "",
          events: [
            {
              type: "output",
              data: { output: "Completed action preview remains visible" },
            },
          ],
        })}
        initialExpanded={false}
        orchestrationMode="native"
      />,
    );

    expect(container.textContent).toContain("Completed action preview remains visible");
  });

  it("renders escaped unicode bullet separators as bullets", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "review" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "before\n\\u2022 \\u2022 \\u2022\nafter",
        })}
        initialExpanded={true}
        orchestrationMode="manager"
      />,
    );

    expect(container.textContent).toContain("• • •");
    expect(container.textContent).not.toContain("\\u2022");
  });

  it("renders escaped unicode bullets inside sub-session panels as bullets", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "review" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          workers: [
            {
              delegation_id: "sub-1",
              worker_session_id: "session-a",
              worker_description: "Adversarial review",
              panel_kind: "sub_session",
              is_new: false,
              instructions_preview: "",
              events: [
                {
                  type: "output",
                  data: { output: "Residual risks:\n\\u2022 first\n\\u2022 second" },
                },
              ],
            },
          ],
        })}
        initialExpanded={true}
        orchestrationMode="manager"
      />,
    );

    expect(container.querySelector(".collapse-ellipsis")?.textContent).toBe("• • •");
    expect(container.textContent).not.toContain("\\u2022");

    fireEvent.click(screen.getByRole("button", { name: /Adversarial review/i }));

    expect(container.textContent).toContain("• first");
    expect(container.textContent).toContain("• second");
    expect(container.textContent).not.toContain("\\u2022");
  });

  it("auto-collapses a nested sub-session panel after its worker run completes", async () => {
    const worker = {
      delegation_id: "sub-1",
      worker_session_id: "session-a",
      worker_description: "Adversarial review",
      panel_kind: "sub_session" as const,
      is_new: false,
      instructions_preview: "",
      events: [
        {
          type: "output" as const,
          data: { output: "running review output" },
        },
      ],
    };
    const { container, rerender } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "review" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          workers: [worker],
        })}
        runs={[
          {
            run_id: "run-sub-1",
            kind: "worker",
            target_message_id: "a1",
            delegation_id: "sub-1",
            pid: null,
            started_at: "2026-07-03T00:00:00Z",
            last_event_at: "2026-07-03T00:00:01Z",
          },
        ]}
        keepExpanded
        orchestrationMode="manager"
      />,
    );

    expect(screen.getByRole("button", { name: /Adversarial review/i }).getAttribute("aria-expanded")).toBe("true");
    expect(container.textContent).toContain("running review output");

    rerender(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "review" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          workers: [{ ...worker, success: true }],
        })}
        runs={[]}
        keepExpanded
        orchestrationMode="manager"
      />,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Adversarial review/i }).getAttribute("aria-expanded")).toBe("false");
    });
    expect(container.querySelector(".collapse-ellipsis")?.textContent).toBe("• • •");
  });

  it("tracks native Codex child activity from the owning turn and terminal panels", async () => {
    const child = (id: string, label: string, success?: boolean, parent?: string) => ({
      delegation_id: id,
      worker_session_id: `thread-${id}`,
      worker_description: label,
      panel_kind: "worker" as const,
      run_mode: "codex_subagent",
      parent_delegation_id: parent,
      is_new: false,
      instructions_preview: "",
      events: [{ type: "output" as const, data: { output: `${label} output` } }],
      ...(success === undefined ? {} : { success }),
    });
    const activeRoot = {
      ...child("codex-root", "Active Codex child"),
      jsonl_path: "/tmp/sanitized-codex-child.jsonl",
      new_byte_offset: 42,
    };
    const completedSibling = child("codex-done", "Completed Codex child", true);
    const failedSibling = child("codex-failed", "Failed Codex child", false);
    const activeNested = child("codex-nested", "Nested Codex child", undefined, "codex-root");
    const parentRun = {
      run_id: "parent-run",
      kind: "manager" as const,
      target_message_id: "a1",
      pid: null,
    };
    const { rerender } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "coordinate children" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "parent-only status",
          isStreaming: true,
          workers: [activeRoot, completedSibling, failedSibling, activeNested],
        })}
        runs={[parentRun]}
        keepExpanded
        orchestrationMode="native"
      />,
    );

    const expanded = (label: RegExp) =>
      screen.getByRole("button", { name: label }).getAttribute("aria-expanded");
    expect(expanded(/Active Codex child/i)).toBe("true");
    expect(expanded(/Nested Codex child/i)).toBe("true");
    expect(expanded(/Completed Codex child/i)).toBe("false");
    expect(expanded(/Failed Codex child/i)).toBe("false");

    rerender(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "coordinate children" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "parent-only final",
          isStreaming: false,
          workers: [
            { ...activeRoot, success: true },
            completedSibling,
            failedSibling,
            { ...activeNested, success: true },
          ],
        })}
        runs={[]}
        keepExpanded
        orchestrationMode="native"
      />,
    );

    await waitFor(() => {
      expect(expanded(/Active Codex child/i)).toBe("false");
      expect(expanded(/Nested Codex child/i)).toBe("false");
    });
  });

  it("auto-collapses a nested native sub-agent block after its tool result arrives", async () => {
    const events = [
      {
        type: "agent_message" as const,
        data: {
          type: "assistant",
          message: {
            content: [
              {
                type: "tool_use",
                id: "tool-1",
                name: "Task",
                input: { description: "review" },
              },
            ],
          },
        },
      },
      {
        type: "agent_message" as const,
        data: {
          type: "assistant",
          parent_tool_use_id: "tool-1",
          message: {
            content: [{ type: "text", text: "nested review output" }],
          },
        },
      },
    ];
    const { container, rerender } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "review" })}
        responseMessage={makeAssistantMsg({ id: "a1", events })}
        initialExpanded={true}
        orchestrationMode="manager"
      />,
    );

    expect(container.querySelector(".sub-agent-header")?.getAttribute("aria-expanded")).toBe("true");
    expect(container.textContent).toContain("nested review output");

    rerender(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "review" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          events: [
            ...events,
            {
              type: "agent_message" as const,
              data: {
                type: "user",
                message: {
                  content: [
                    {
                      type: "tool_result",
                      tool_use_id: "tool-1",
                      content: "done",
                    },
                  ],
                },
              },
            },
          ],
        })}
        initialExpanded={true}
        orchestrationMode="manager"
      />,
    );

    await waitFor(() => {
      expect(container.querySelector(".sub-agent-header")?.getAttribute("aria-expanded")).toBe("false");
    });
    expect(container.querySelector(".sub-agent-block .collapse-ellipsis")?.textContent).toBe("• • •");
  });

  it("renders creation-only sub-session panels without an empty expand toggle", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "review" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          workers: [
            {
              delegation_id: "sub-created-1",
              worker_session_id: "session-a",
              worker_description: "Adversarial review for ask button UI/mobile fix created",
              panel_kind: "sub_session_created",
              is_new: true,
              instructions_preview: "",
              events: [],
            },
          ],
        })}
        initialExpanded={true}
        orchestrationMode="manager"
      />,
    );

    expect(container.textContent).toContain("Sub Session Created");
    expect(container.textContent).toContain("Adversarial review for ask button UI/mobile fix created");
    expect(screen.queryByRole("button", { name: /Adversarial review/i })).toBeNull();
    expect(container.querySelector(".timeline-static-header")).not.toBeNull();
    expect(container.querySelector(".timeline-toggle-header")).toBeNull();
    expect(container.querySelector(".collapsible-timeline-block .collapse-arrow")).toBeNull();
  });

  it("renders collapsed sub-agent ellipsis as bullets", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "review" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          events: [
            {
              type: "tool_call",
              data: {
                tool: "Task",
                args: { description: "review" },
                tool_use_id: "tool-1",
              },
            },
            {
              type: "output",
              data: {
                output: "sub-agent output",
                parent_tool_use_id: "tool-1",
              },
            },
          ],
        })}
        initialExpanded={true}
        orchestrationMode="manager"
      />,
    );

    const header = container.querySelector<HTMLElement>(".sub-agent-header");
    expect(header).not.toBeNull();
    fireEvent.click(header!);

    expect(container.querySelector(".sub-agent-block .collapse-ellipsis")?.textContent).toBe("• • •");
    expect(container.textContent).not.toContain("\\u2022");
  });

  it("edits and submits an alter from the user message bubble", async () => {
    const onAlterUserMessage = vi.fn(() => true);
    render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "old prompt" })}
        responseMessage={makeAssistantMsg({ id: "a1", content: "done" })}
        onAlterTurnMessage={onAlterUserMessage}
        orchestrationMode="manager"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Alter" }));
    const editor = screen.getByDisplayValue("old prompt");
    fireEvent.change(editor, { target: { value: "new prompt" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Send altered" }));
    });

    expect(onAlterUserMessage).toHaveBeenCalledTimes(1);
    expect(onAlterUserMessage.mock.calls[0][0].id).toBe("u1");
    expect(onAlterUserMessage.mock.calls[0][1]).toBe("new prompt");
  });

  it("shows Alter only on the latest user prompt in Chat", () => {
    render(
      <Chat
        messages={[
          makeUserMsg({ id: "u1", content: "first prompt" }),
          makeAssistantMsg({ id: "a1", content: "first reply" }),
          makeUserMsg({ id: "u2", content: "latest prompt" }),
          makeAssistantMsg({ id: "a2", content: "latest reply" }),
        ]}
        pendingMessages={[]}
        runs={[]}
        streamingEvents={[]}
        isStreaming={false}
        isStopping={false}
        streamingLoadPhase={null}
        onSend={() => true}
        onAlterUserMessage={() => true}
        disabled={false}
        session={makeSession()}
        draft=""
        onDraftChange={() => {}}
        queuedPrompt={null}
        onPromoteQueued={() => {}}
      />,
    );

    const userMessages = screen.getAllByTestId("user-message");
    expect(within(userMessages[0]).queryByRole("button", { name: "Alter" })).toBeNull();
    expect(within(userMessages[1]).getByRole("button", { name: "Alter" })).toBeTruthy();
  });

  it("shows 'Stopped' (not Interrupted) when stopped without an interrupting message", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "partial",
          stopped_at: new Date("2024-01-01T10:00:00Z").toISOString(),
        })}
        initialExpanded={false}
        orchestrationMode="manager"
      />,
    );

    const indicator = container.querySelector(".stopped-indicator");
    expect(indicator).not.toBeNull();
    expect(indicator!.textContent).toContain("Stopped");
    expect(indicator!.textContent).not.toContain("Interrupted");
  });

  it("renders no stopped indicator for a collapsed group that completed normally", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1" })}
        responseMessage={makeAssistantMsg({ id: "a1", content: "done" })}
        initialExpanded={false}
        orchestrationMode="manager"
      />,
    );

    expect(container.querySelector(".stopped-indicator")).toBeNull();
  });

  it("shows steer prompts when the group is collapsed", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "start work" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "final answer after steer",
          events: [
            {
              type: "steer_prompt",
              data: {
                uuid: "steer-1",
                prompt: "also include queued interrupt",
              },
            },
            {
              type: "agent_message",
              data: {
                uuid: "a-event-1",
                type: "assistant",
                message: {
                  content: [
                    {
                      type: "text",
                      text: "final answer after steer",
                    },
                  ],
                },
              },
            },
          ],
        })}
        initialExpanded={false}
        orchestrationMode="native"
      />,
    );

    expect(container.querySelector('[data-message-id="a1"] .message-content')).toBeNull();
    expect(container.querySelector(".event-steer-prompt")?.textContent).toContain(
      "also include queued interrupt",
    );
    expect(container.textContent).toContain("final answer after steer");
  });

  it("keeps the collapsed final preview owned by the parent response", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "delegate work" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "parent final text",
        })}
        childTurnGroups={[
          {
            initiator: makeUserMsg({ id: "child-u1", content: "review the work" }),
            response: makeAssistantMsg({
              id: "child-a1",
              content: "child final text",
            }),
          },
        ]}
        initialExpanded={false}
        orchestrationMode="manager"
      />,
    );

    expect(container.querySelector('[data-message-id="a1"] .message-content')).toBeNull();
    expect(container.textContent).toContain("parent final text");
    expect(container.textContent).not.toContain("child final text");
  });

  it("does not promote nested child output when the parent has no final text", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "delegate work" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          events: [
            {
              type: "output",
              data: {
                output: "outer setup",
              },
            },
            {
              type: "tool_call",
              data: {
                tool: "delegate",
                args: {},
                tool_use_id: "tool-1",
              },
            },
            {
              type: "output",
              data: {
                output: "nested final text chunk",
                parent_tool_use_id: "tool-1",
              },
            },
          ],
        })}
        initialExpanded={false}
        orchestrationMode="manager"
      />,
    );

    expect(container.querySelector('[data-message-id="a1"] .message-content')).toBeNull();
    expect(container.textContent).not.toContain("nested final text chunk");
    expect(container.textContent).not.toContain("outer setup");
    expect(container.textContent).toContain("delegate");
  });

  it("hydrates stubbed historical events immediately while expanded", async () => {
    const realFetch = globalThis.fetch;
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify(makeAssistantMsg({
          id: "a1",
          content: "full content",
          events: [
            {
              type: "output",
              data: { output: "full expanded output" },
            },
          ],
        })),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    try {
      const { container } = render(
        <TurnGroup
          initiatorMessage={makeUserMsg({ id: "u1", content: "expensive history" })}
          responseMessage={makeAssistantMsg({
            id: "a1",
            content: "stale fallback content",
            events: [],
            stub: {
              event_count: 40,
              last_events: [
                {
                  type: "output",
                  data: { output: "stub collapsed preview" },
                },
              ],
            },
          })}
          sessionId="s1"
          initialExpanded={true}
          orchestrationMode="native"
        />,
      );

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledTimes(1);
        expect(container.textContent).toContain("full expanded output");
      });
      expect(fetchMock.mock.calls[0][0]).toBe(
        "/api/sessions/s1/messages/a1/events",
      );
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it("hydrates projected historical events immediately while expanded", async () => {
    const realFetch = globalThis.fetch;
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify(makeAssistantMsg({
          id: "a1",
          content: "full content",
          events: [
            {
              type: "output",
              data: { output: "full projected output" },
            },
          ],
        })),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    try {
      const { container } = render(
        <TurnGroup
          initiatorMessage={makeUserMsg({ id: "u1", content: "expensive history" })}
          responseMessage={makeAssistantMsg({
            id: "a1",
            content: "stale fallback content",
            events: undefined,
            omitted_payloads: { events: { revision: "rev-1", count: 1 } },
          })}
          sessionId="s1"
          initialExpanded={true}
          orchestrationMode="native"
        />,
      );

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledTimes(1);
        expect(container.textContent).toContain("full projected output");
      });
      expect(fetchMock.mock.calls[0][0]).toBe(
        "/api/sessions/s1/messages/a1/events",
      );
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it("hydrates worker panels omitted from the compact message stub", async () => {
    const realFetch = globalThis.fetch;
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify(makeAssistantMsg({
          id: "a1",
          workers: [{
            delegation_id: "codex-child",
            worker_session_id: "codex-thread-child",
            worker_description: "Hydrated Codex child",
            panel_kind: "worker",
            run_mode: "codex_subagent",
            is_new: false,
            instructions_preview: "",
            success: true,
            events: [{
              type: "agent_message",
              data: {
                uuid: "codex-child-output",
                type: "assistant",
                message: { content: [{ type: "text", text: "hydrated child output" }] },
              },
            }],
          }],
        })),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    try {
      render(
        <TurnGroup
          initiatorMessage={makeUserMsg({ id: "u1", content: "delegate" })}
          responseMessage={makeAssistantMsg({
            id: "a1",
            workers: [],
            omitted_payloads: { events: { revision: "worker-rev", count: 1 } },
          })}
          sessionId="s1"
          initialExpanded={true}
          orchestrationMode="native"
        />,
      );

      await waitFor(() => {
        expect(screen.getByRole("button", { name: /Hydrated Codex child/i })).toBeTruthy();
      });
      fireEvent.click(screen.getByRole("button", { name: /Hydrated Codex child/i }));
      expect(screen.getByText("hydrated child output")).toBeTruthy();
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it("hydrates terminal worker metadata without losing newer live events", async () => {
    const realFetch = globalThis.fetch;
    let resolveFetch!: (response: Response) => void;
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    const worker = {
      delegation_id: "codex-child",
      worker_session_id: "codex-thread-child",
      worker_description: "Hydrating Codex child",
      panel_kind: "worker" as const,
      run_mode: "codex_subagent",
      is_new: false,
      instructions_preview: "",
      events: [{ type: "output" as const, data: { output: "newer live output" } }],
    };

    try {
      render(
        <TurnGroup
          initiatorMessage={makeUserMsg({ id: "u1", content: "delegate" })}
          responseMessage={makeAssistantMsg({
            id: "a1",
            workers: [worker],
            omitted_payloads: { events: { revision: "worker-rev", count: 1 } },
          })}
          runs={[{
            run_id: "run-codex-child",
            kind: "worker",
            target_message_id: "a1",
            delegation_id: "codex-child",
            pid: null,
            started_at: "2026-08-04T09:00:00Z",
            last_event_at: "2026-08-04T09:00:01Z",
          }]}
          sessionId="s1"
          initialExpanded={true}
          orchestrationMode="native"
        />,
      );

      const panel = screen.getByRole("button", { name: /Hydrating Codex child/i });
      expect(panel.getAttribute("aria-expanded")).toBe("true");
      resolveFetch(new Response(JSON.stringify(makeAssistantMsg({
        id: "a1",
        workers: [{
          ...worker,
          success: true,
          events: [{ type: "output", data: { output: "hydrated final output" } }],
        }],
      })), { status: 200, headers: { "Content-Type": "application/json" } }));

      await waitFor(() => expect(panel.getAttribute("aria-expanded")).toBe("false"));
      fireEvent.click(panel);
      expect(screen.getByText("newer live output")).toBeTruthy();
      expect(screen.getByText("hydrated final output")).toBeTruthy();
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it("never lazily fetches events for a streaming message", async () => {
    const realFetch = globalThis.fetch;
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    try {
      const { rerender, unmount } = render(
        <TurnGroup
          initiatorMessage={makeUserMsg({ id: "u1", content: "go" })}
          responseMessage={makeAssistantMsg({
            id: "a1",
            content: "lead-in",
            isStreaming: true,
            events: [{ type: "output", data: { output: "live tail" } }],
            omitted_payloads: { events: { revision: "rev-1", count: 3 } },
          })}
          initialExpanded={true}
          sessionId="s1"
          orchestrationMode="native"
        />,
      );

      // Every in-flight delta re-folds the revision; none may cost a request.
      for (const revision of ["rev-2", "rev-3", "rev-4"]) {
        rerender(
          <TurnGroup
            initiatorMessage={makeUserMsg({ id: "u1", content: "go" })}
            responseMessage={makeAssistantMsg({
              id: "a1",
              content: `text ${revision}`,
              isStreaming: true,
              events: [{ type: "output", data: { output: "live tail" } }],
              omitted_payloads: { events: { revision, count: 3 } },
            })}
            initialExpanded={true}
            sessionId="s1"
            orchestrationMode="native"
          />,
        );
      }

      await Promise.resolve();
      expect(fetchMock).not.toHaveBeenCalled();
      unmount();
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it("keeps projected events hydrated across manual collapse and expand", async () => {
    const realFetch = globalThis.fetch;
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify(makeAssistantMsg({
          id: "a1",
          content: "full content",
          events: [
            {
              type: "output",
              data: { output: "manual full output" },
            },
          ],
        })),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    try {
      const baseResponse = makeAssistantMsg({
        id: "a1",
        content: "stale fallback content",
        events: undefined,
        omitted_payloads: { events: { revision: "rev-1", count: 1 } },
      });
      const { container, rerender } = render(
        <TurnGroup
          initiatorMessage={makeUserMsg({ id: "u1", content: "manual prompt" })}
          responseMessage={baseResponse}
          initialExpanded={true}
          sessionId="s1"
          orchestrationMode="native"
        />,
      );

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledTimes(1);
        expect(container.textContent).toContain("manual full output");
      });

      rerender(
        <TurnGroup
          initiatorMessage={makeUserMsg({ id: "u1", content: "manual prompt" })}
          responseMessage={{
            ...baseResponse,
            content: "fresh compact content",
            retrying_until: "2026-07-07T12:00:00.000Z",
          }}
          initialExpanded={true}
          sessionId="s1"
          orchestrationMode="native"
        />,
      );
      expect(container.textContent).toContain("fresh compact content");
      expect(container.textContent).toContain("manual full output");

      fireEvent.click(screen.getByRole("button", { name: /User/i }));
      expect(container.querySelector(".assistant-message .message-content")).toBeNull();

      fireEvent.click(screen.getByRole("button", { name: /User/i }));
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(container.textContent).toContain("manual full output");
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it("keeps child projected events hydrated across parent collapse and expand", async () => {
    const realFetch = globalThis.fetch;
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify(makeAssistantMsg({
          id: "child-a1",
          content: "child full content",
          events: [
            {
              type: "output",
              data: { output: "child full output" },
            },
          ],
        })),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    try {
      const { container } = render(
        <TurnGroup
          initiatorMessage={makeUserMsg({ id: "u1", content: "parent prompt" })}
          responseMessage={makeAssistantMsg({ id: "a1", content: "parent response" })}
          childTurnGroups={[
            {
              initiator: makeUserMsg({
                id: "child-u1",
                content: "child prompt",
                parent_id: "u1",
              }),
              response: makeAssistantMsg({
                id: "child-a1",
                content: "child stale fallback",
                events: undefined,
                omitted_payloads: { events: { revision: "child-rev-1", count: 1 } },
              }),
            },
          ]}
          initialExpanded={true}
          sessionId="s1"
          orchestrationMode="native"
        />,
      );

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledTimes(1);
        expect(container.textContent).toContain("child full output");
      });

      fireEvent.click(screen.getByRole("button", { name: /User/i }));
      expect(container.textContent).not.toContain("child full output");

      fireEvent.click(screen.getByRole("button", { name: /User/i }));
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(container.textContent).toContain("child full output");
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it("consolidates the running indication into the same footer as the provider chips", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "live prompt" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "streaming reply",
          isStreaming: true,
          run_meta: {
            provider_id: "claude",
            model: "claude-sonnet-4-6",
            reasoning_effort: "high",
          },
        })}
        initialExpanded={true}
        sessionId="s1"
        orchestrationMode="native"
        runs={[{ run_id: "run-1", kind: "manager", target_message_id: "a1", pid: null }]}
      />,
    );

    const footer = container.querySelector<HTMLElement>(".assistant-run-meta-footer");
    expect(footer).not.toBeNull();
    expect(footer!.classList.contains("is-running")).toBe(true);

    const providerChips = footer!.querySelector<HTMLElement>(".run-meta-chips");
    const runningIndicator = footer!.querySelector<HTMLElement>(".run-badge-stack");

    expect(providerChips).not.toBeNull();
    expect(runningIndicator).not.toBeNull();
    // No separate standalone running-indicator element remains.
    expect(container.querySelector(".streaming-footer")).toBeNull();
    expect(
      providerChips!.compareDocumentPosition(runningIndicator!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("still shows provider/model chips alone once the turn is no longer running", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "done prompt" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "finished reply",
          isStreaming: false,
          run_meta: {
            provider_id: "claude",
            model: "claude-sonnet-4-6",
            reasoning_effort: "high",
          },
        })}
        initialExpanded={true}
        sessionId="s1"
        orchestrationMode="native"
        runs={[]}
      />,
    );

    const footer = container.querySelector<HTMLElement>(".assistant-run-meta-footer");
    expect(footer).not.toBeNull();
    expect(footer!.classList.contains("is-running")).toBe(false);
    expect(footer!.querySelector(".run-meta-chips")).not.toBeNull();
    expect(footer!.querySelector(".run-badge-stack")).toBeNull();
  });
});
