import { afterEach, describe, it, expect } from "vitest";
import { act, render, waitFor } from "@testing-library/react";
import React from "react";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";
import { MessageBubble } from "../src/components/MessageBubble";
import {
  buildInlineTagsPreamble,
  mergeTagsIntoPrompt,
} from "../src/utils/inlineTagsPrompt";
import type { InlineTag } from "../src/types/inlineTag";
import { ASK_SINGLETON_ID } from "../src/askSession";
import { SIDEBAR_MINIMIZED_WIDTH } from "../src/sidebarLayout";

describe("message rendering", () => {
  const defaultViewport = {
    width: window.innerWidth,
    height: window.innerHeight,
  };
  const fileEditMeta = (filePath: string, content = "", persistent = true) => ({
    file_paths: [filePath],
    original_contents: { [filePath]: content },
    persistent,
  });
  const setViewport = (width: number, height: number) => {
    Object.defineProperty(window, "innerWidth", {
      value: width,
      configurable: true,
    });
    Object.defineProperty(window, "innerHeight", {
      value: height,
      configurable: true,
    });
    window.dispatchEvent(new Event("resize"));
  };
  const setDesktopViewport = () => {
    setViewport(1400, 900);
  };
  afterEach(() => {
    setViewport(defaultViewport.width, defaultViewport.height);
  });

  it("reserves the chat header for an empty Ask session", async () => {
    const askEmpty = makeSession({
      id: ASK_SINGLETON_ID,
      name: "Ask",
      orchestration_mode: "virtual",
      messages: [],
    });
    const hEmpty = await renderApp({ seed: { sessions: [askEmpty] } });
    expect(hEmpty.$(".input-area .ask-greeting")).toBeNull();
    expect(hEmpty.$('[data-testid="chat-messages"] .ask-hero-wrap')).not.toBeNull();
    hEmpty.unmount();
  });

  it("hides the Ask header while a picker result is unresolved", async () => {
    const askResult = {
      results: [{ id: "target", name: "Target", cwd: "/tmp", first_user_prompt: "" }],
      reasoning: "matching work",
    };
    const askPending = makeSession({
      id: ASK_SINGLETON_ID,
      name: "Ask",
      orchestration_mode: "virtual",
      messages: [
        makeUserMsg({ id: "ask-u", content: "find auth work" }),
        makeAssistantMsg({
          id: "ask-a",
          content: "matching work",
          ask_result: askResult,
        }),
      ],
    });
    const hPending = await renderApp({
      seed: { sessions: [askPending] },
    });
    expect(hPending.$(".input-area .ask-greeting")).toBeNull();
    expect(hPending.$('[data-testid="chat-messages"] .ask-header-wrap')).toBeNull();
    hPending.unmount();
  });

  it("restores the compact Ask header after picker resolution", async () => {
    const askResult = {
      results: [{ id: "target", name: "Target", cwd: "/tmp", first_user_prompt: "" }],
      reasoning: "matching work",
    };
    const askResolved = makeSession({
      id: ASK_SINGLETON_ID,
      name: "Ask",
      orchestration_mode: "virtual",
      messages: [
        makeUserMsg({ id: "ask-u", content: "find auth work" }),
        makeAssistantMsg({
          id: "ask-a",
          content: "matching work",
          ask_result: askResult,
          chosen_session_id: "target",
        }),
      ],
    });
    const hResolved = await renderApp({
      seed: { sessions: [askResolved] },
    });
    expect(hResolved.$(".input-area .ask-greeting")).toBeNull();
    expect(hResolved.$('[data-testid="chat-messages"] .ask-header-wrap')).not.toBeNull();
    expect(hResolved.$('[data-testid="chat-messages"] .ask-hero-wrap')).toBeNull();
    hResolved.unmount();
  });

  // "completed turns render collapsed by default; expanding reveals the
  // response" and "clicking a turn's header toggles it between collapsed
  // and expanded" are DELETED, not ported: on the native path
  // (src/surface/TurnView.tsx's `ResultRow`) a turn's final result
  // renders UNCONDITIONALLY, never gated by collapse state — only
  // intermediate body content (tool calls/explanation trail) is ever
  // hidden behind the live/manuallyExtended `Ellipsis` gate. There is no
  // representable "the finished answer is hidden until you expand"
  // state to construct; see tests/chat-collapse-fullstack.test.tsx's
  // deletion for the same root cause, and tests/surface/toolInteraction
  // .test.tsx for native body-expansion coverage.

  // "user message status badge renders 'sending' on optimistic bubble"
  // is DELETED, not ported — same REAL PRODUCT GAP documented in
  // tests/client-id.test.ts's ledger entry: the native path has no
  // optimistic client-side echo of a just-sent prompt at all (no
  // "pending"/"sending" NodeWire status exists; a typed_prompt only
  // appears once the backend confirms it).

  it("renders persisted user file attachments", () => {
    const message = makeUserMsg({
      id: "u-file",
      content: "read this",
      files: [{
        name: "notes.txt",
        media_type: "text/plain",
        size: 1536,
      }],
    });
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "native",
      }),
    );

    expect(container.querySelector(".message-file-name")?.textContent).toBe("notes.txt");
    expect(container.querySelector(".message-file-size")?.textContent).toBe("1.5 KB");
    unmount();
  });

  it("renders optimistic local user attachment previews", () => {
    const message = makeUserMsg({
      id: "u-pending",
      content: "look",
      status: "sending",
      images: [{
        media_type: "image/png",
        dataUrl: "data:image/png;base64,abc",
      }],
      files: [{
        name: "draft.md",
        media_type: "text/markdown",
        size: 8,
      }],
    });
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "native",
      }),
    );

    expect(container.querySelector(".message-image")?.getAttribute("src")).toBe("data:image/png;base64,abc");
    expect(container.querySelector(".message-file-name")?.textContent).toBe("draft.md");
    unmount();
  });


  it("fetches full events for compacted non-stub assistant messages", async () => {
    const fullEvent = {
      type: "agent_message" as const,
      data: {
        type: "assistant",
        uuid: "ev-full",
        message: {
          role: "assistant",
          content: [{ type: "text", text: "Fetched event text" }],
        },
      },
    };
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (async () => ({
      json: async () => makeAssistantMsg({
        id: "a-omitted",
        content: "Fetched event text",
        events: [fullEvent],
      }),
    })) as typeof fetch;
    try {
      const message = makeAssistantMsg({
        id: "a-omitted",
        content: "",
        events: undefined,
        omitted_payloads: { events: { revision: "rev-1", count: 1 } },
      });
      const { container, unmount } = render(
        React.createElement(MessageBubble, {
          message,
          sessionId: "s1",
          orchestrationMode: "native",
        }),
      );
      await waitFor(() => {
        expect(container.textContent).toContain("Fetched event text");
      });
      unmount();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("refetches compacted non-stub events when the omitted payload revision changes", async () => {
    let fetchCount = 0;
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (async () => {
      fetchCount += 1;
      const text = fetchCount === 1 ? "First event text" : "Updated event text";
      return {
        json: async () => makeAssistantMsg({
          id: "a-omitted",
          content: text,
          events: [{
            type: "agent_message" as const,
            data: {
              type: "assistant",
              uuid: "ev-full",
              message: {
                role: "assistant",
                content: [{ type: "text", text }],
              },
            },
          }],
        }),
      };
    }) as typeof fetch;
    try {
      const first = makeAssistantMsg({
        id: "a-omitted",
        content: "",
        events: undefined,
        omitted_payloads: { events: { revision: "rev-1", count: 1 } },
      });
      const { container, rerender, unmount } = render(
        React.createElement(MessageBubble, {
          message: first,
          sessionId: "s1",
          orchestrationMode: "native",
        }),
      );
      await waitFor(() => {
        expect(container.textContent).toContain("First event text");
      });

      const second = makeAssistantMsg({
        ...first,
        omitted_payloads: { events: { revision: "rev-2", count: 1 } },
      });
      rerender(
        React.createElement(MessageBubble, {
          message: second,
          sessionId: "s1",
          orchestrationMode: "native",
        }),
      );
      await waitFor(() => {
        expect(container.textContent).toContain("Updated event text");
      });
      expect(fetchCount).toBe(2);
      unmount();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("renders a TodoWrite followed by its backend snapshot once", () => {
    const todos = [
      { content: "Review changes", status: "completed" as const },
      { content: "Commit and push", status: "in_progress" as const },
    ];
    const message = makeAssistantMsg({
      events: [
        {
          type: "tool_call",
          data: {
            tool: "TodoWrite",
            tool_use_id: "todo-1",
            args: { todos },
          },
        },
        {
          type: "todos_snapshot",
          data: { todos },
        },
      ],
    });
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "native",
      }),
    );

    expect(container.querySelectorAll(".todos-snapshot")).toHaveLength(1);
    expect(container.textContent).toContain("Review changes");
    expect(container.textContent).toContain("Commit and push");
    unmount();
  });

  it("renders todo snapshots outside assistant text action groups", () => {
    const todos = [
      { content: "Check grouping", status: "in_progress" as const },
    ];
    const message = makeAssistantMsg({
      events: [
        {
          type: "output",
          data: { output: "Updating the task list." },
        },
        {
          type: "tool_call",
          data: {
            tool: "TodoWrite",
            tool_use_id: "todo-1",
            args: { todos },
          },
        },
        {
          type: "todos_snapshot",
          data: { todos },
        },
      ],
    });
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "native",
      }),
    );

    expect(container.querySelectorAll("[data-testid='auto-action-group']")).toHaveLength(0);
    expect(container.querySelector(".todos-snapshot")?.textContent).toContain("Check grouping");
    unmount();
  });

  // "renders model switch events as a trailing turn-group marker" is
  // DELETED, not ported — it exercises the exact same "trailing banner
  // with no owning next turn yet" legacy state already deleted (with the
  // full native-model rationale) in tests/model-switch-grouping.test.tsx.
  // The "preceding next prompt" case that DOES have a native equivalent
  // is already ported there.

  it("dedups consecutive identical todo snapshots", () => {
    const todos = [
      { content: "Run targeted tests", status: "completed" as const },
    ];
    const message = makeAssistantMsg({
      events: [
        { type: "todos_snapshot", data: { todos } },
        { type: "todos_snapshot", data: { todos } },
      ],
    });
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "native",
      }),
    );

    expect(container.querySelectorAll(".todos-snapshot")).toHaveLength(1);
    expect(container.textContent).toContain("Run targeted tests");
    unmount();
  });

  it("standalone user message timestamp renders in the bottom footer", () => {
    const message = makeUserMsg({
      id: "u1",
      content: "hello",
      timestamp: "2024-01-01T10:00:00Z",
    });
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "native",
      }),
    );

    const content = container.querySelector(".user-message .message-content");
    expect(content).not.toBeNull();
    const footer = content!.lastElementChild;
    expect(footer?.classList.contains("message-box-footer")).toBe(true);
    expect(footer?.querySelector(".user-message-time")?.textContent).toMatch(/\d{2}:\d{2}:\d{2}/);
    unmount();
  });

  // "error event shows the error text on the failed user bubble" and "a
  // persisted assistant with error=true renders error chrome" are
  // DELETED, not ported: the native model represents turn errors through a
  // dedicated `failure` Contract Node (FailurePayloadWire), a SEPARATE
  // content node, not a status flag stamped onto the user's own
  // typed_prompt bubble — there's no structural equivalent to port THESE
  // specific bubble-status-shaped assertions to. The underlying user-
  // observable gap the second case exercised (`.error-block-toggle`
  // disclosure, `.error-copy-btn` clipboard copy) has since been CLOSED —
  // `FailureChip` (src/surface/leaf/Chips.tsx, `surface-failure-chip`) now
  // has the same expand/disclosure + copy chrome at parity — see
  // tests/surface/nativeGaps.test.tsx's "FailureChip — copy + expand/
  // disclosure chrome" suite for the restored coverage.

  it("thinking text mentioning 'API Error' is NOT mis-rendered as an error block", async () => {
    // Regression: the assistant's thinking prose (e.g. while investigating
    // error-handling code) often quotes substrings like "API Error: 429".
    // The previous heuristic in MessageBubble.renderSingleEvent matched
    // those substrings and rendered the entire thought as a red
    // .event-error div. Real errors flow via assistant.error/errorText
    // (.message-status.status-error chrome — covered above) or via
    // type:"error" WS events; thinking events with error keywords are
    // ALWAYS prose, never errors.
    const thoughtProse =
      "Let me trace where errors get rendered. The MessageBubble checks " +
      "for /API Error|authentication_error|Failed to authenticate/i in the " +
      "thinking text and renders it red. So when prose mentions 'API Error: " +
      "429' as a quote it false-positives. I need to investigate the actual " +
      "feeders to see if any code path emits thinking events with API Error " +
      "text — my hypothesis is that none do and the heuristic can be deleted.";
    const session = makeSession({
      // Native mode: NativeStrategy.getEvents reads message.events.
      // (Manager mode would read message.manager.events — but the bug
      // was reproduced in a native session, so mirror that.)
      orchestration_mode: "native",
      messages: [
        makeUserMsg({ id: "u", content: "investigate the bug" }),
        makeAssistantMsg({
          id: "a",
          content: "",
          // isStreaming=true so the group doesn't auto-collapse — the
          // bug was observed live during streaming.
          isStreaming: true,
          events: [
            {
              type: "thinking",
              data: { thought: thoughtProse },
            },
          ],
        }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    // The thinking event renders either inside the (uncollapsed)
    // assistant subtree OR — when the group is collapsed — inside the
    // user bubble's last-event preview slot. Both go through
    // `renderSingleEvent`, so either path exposes the bug. Assert on
    // the whole chat surface to stay robust to either layout.
    const chat = h.$('[data-testid="chat-messages"]');
    expect(chat).not.toBeNull();
    expect(chat!.querySelector(".event-error")).toBeNull();
    expect(chat!.querySelector(".thinking-block")).not.toBeNull();
    h.unmount();
  });

  it("does not render assistant prose after WebSearch as the search result", async () => {
    const message = makeAssistantMsg({
      id: "a",
      content: "",
      events: [
        {
          type: "output",
          data: {
            output: "I'll verify current model options before recommending anything.",
          },
        },
        {
          type: "tool_call",
          data: {
            tool: "WebSearch",
            args: {
              query: "embedding models for code retrieval",
            },
            tool_use_id: "ws_1",
          },
        },
        {
          type: "output",
          data: {
            output: "Best candidates:\n\n- Qwen/Qwen3-Embedding-0.6B\n- BAAI/bge-m3",
          },
        },
      ],
    });
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "native",
      }),
    );

    // The WebSearch action isn't the turn's final item (trailing prose
    // follows it), so it renders inside a collapsed AutoActionGroup by
    // default — open it before looking for the tool card.
    const groupHeader = await waitFor(() => {
      const el = container.querySelector<HTMLElement>(".auto-action-group-header");
      expect(el).not.toBeNull();
      return el!;
    });
    await act(async () => {
      groupHeader.click();
    });

    await waitFor(() => expect(container.querySelector(".tool-call")).not.toBeNull());
    const tool = container.querySelector(".tool-call");
    expect(tool).not.toBeNull();
    expect(tool!.textContent ?? "").toContain("WebSearch");
    expect(tool!.querySelector(".tool-result-inline, .tool-result-block")).toBeNull();

    const boxes = Array.from(container.querySelectorAll(".message-box"));
    expect(boxes).toHaveLength(2);
    expect(boxes[1].textContent ?? "").toContain("Best candidates");
    unmount();
  });

  it("renders legacy worker panels whose events array is absent", () => {
    const message = makeAssistantMsg({
      id: "a",
      content: "",
      events: [
        {
          type: "tool_call",
          data: {
            tool: "delegate",
            args: {},
            tool_use_id: "delegate-1",
          },
        },
      ],
      workers: [
        {
          delegation_id: "deleg-1",
          worker_session_id: "worker-1",
          worker_description: "Legacy worker",
          is_new: false,
          instructions_preview: "",
        } as never,
      ],
    });

    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "manager",
      }),
    );

    expect(container.textContent ?? "").toContain("Legacy worker");
    unmount();
  });

  it("renders steer prompt image attachments", () => {
    const message = makeAssistantMsg({
      id: "a",
      content: "",
      events: [
        {
          type: "steer_prompt",
          data: {
            prompt: "look at this",
            images: [{ filename: "steer-0.png", media_type: "image/png" }],
          },
        },
      ],
    });

    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        sessionId: "sid",
        orchestrationMode: "native",
      }),
    );

    const img = container.querySelector(".event-steer-prompt .message-image");
    expect(img).not.toBeNull();
    expect(img?.getAttribute("src")).toBe("/api/sessions/sid/images/steer-0.png");
    unmount();
  });

  it("renders an explicit Bash tool_result once when present", async () => {
    const marker = "BASH_RESULT_VISIBLE_ONCE";
    const message = makeAssistantMsg({
      id: "a",
      content: "",
      events: [
        {
          type: "agent_message",
          data: {
            type: "assistant",
            message: {
              content: [
                {
                  type: "tool_use",
                  id: "bash_1",
                  name: "Bash",
                  input: { command: "echo visible" },
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
              content: [
                {
                  type: "tool_result",
                  tool_use_id: "bash_1",
                  content: marker,
                },
              ],
            },
          },
        },
      ],
    });
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "native",
      }),
    );

    await waitFor(() => expect(container.querySelector(".tool-call")).not.toBeNull());
    const tool = container.querySelector(".tool-call");
    expect(tool).not.toBeNull();
    expect(tool!.textContent ?? "").toContain("echo visible");
    expect(tool!.querySelector(".bash-result-content")).not.toBeNull();
    expect(tool!.textContent ?? "").toContain(marker);
    const boxes = Array.from(container.querySelectorAll(".message-box"));
    expect(boxes.some((box) => (box.textContent ?? "").includes(marker))).toBe(false);
    unmount();
  });

  it("renders an orphaned tool_result as chat output", async () => {
    const marker = "ORPHAN_CODEX_RESULT_VISIBLE";
    const message = makeAssistantMsg({
      id: "a",
      content: "",
      events: [
        {
          type: "agent_message",
          data: {
            type: "user",
            message: {
              content: [
                {
                  type: "tool_result",
                  tool_use_id: "missing_tool",
                  content: marker,
                },
              ],
            },
          },
        },
      ],
    });
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "native",
      }),
    );

    expect(container.querySelector(".message-box")?.textContent ?? "").toContain(marker);
    expect(container.querySelector(".tool-call")).toBeNull();
    unmount();
  });

  // "a stopped assistant renders the stopped indicator" is DELETED, not
  // ported to THIS file's direct-`<MessageBubble>` shape — the gap it
  // flagged (no `.stopped-indicator`-equivalent existed under
  // src/surface/) has since been CLOSED: `StoppedIndicator`
  // (src/surface/leaf/StoppedIndicator.tsx), driven by `TurnEntry.phase
  // === "stopped"` (a `turn_lifecycle` frame), wired into TurnView.tsx —
  // see tests/surface/nativeGaps.test.tsx for the restored coverage
  // (component-level + a harness-level TurnView integration case).

  it("empty session (native) renders an empty surface with no turns", async () => {
    // Native equivalent of the legacy "empty session renders an empty
    // chat with no placeholder messages" case — the old ".chat-empty"
    // welcome placeholder doesn't exist on either path (only the Ask
    // singleton renders a greeting hero); a regular empty session's
    // surface simply mounts with zero turns.
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "s-empty", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => backend.seedSurface("s-empty", { turns: [] }),
    });
    await h.selectSession("s-empty");

    await h.waitFor(() => h.$('[data-testid="surface-chat-view"]') !== null);
    expect(h.$$('[data-testid="surface-turn"]')).toHaveLength(0);
    h.unmount();
  });

  it("shows the Better Agent B mark in the app sidebar", async () => {
    const session = makeSession({ messages: [] });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const mark = h.$(".sidebar-header-row .better-agent-brand-mark");
    expect(mark).not.toBeNull();
    expect(mark!.textContent).toBe("B");
    expect(h.$(".app-title-brand .app-title")).not.toBeNull();
    h.unmount();
  });

  it("chat toolbar shows the session name", async () => {
    const session = makeSession({ name: "research session" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    expect(h.toJSON().chat.title).toBe("research session");
    h.unmount();
  });

  it("Raw JSON toggle button is present in the toolbar", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    await h.click(".chat-toolbar-overflow-trigger");
    const labels = Array.from(h.$$(".raw-toggle")).map((b) => b.textContent);
    expect(labels).toContain("chat.rawJsonButton");
    h.unmount();
  });

  // buildInlineTagsPreamble emits a structured `<inline-tags><c><sel>...`
  // tag block, rendered as `.inline-tags-card` cards (not raw markdown),
  // so each tag's selected text and comment render as separate DOM nodes
  // instead of markdown-prone "[Selected text]: ... User comment: yes"
  // lines.
  // "inline-tag preamble renders selected text and user comments
  // visibly" is DELETED, not ported to THIS file's direct-`<MessageBubble>`
  // shape — the gap it flagged has since been CLOSED: `TypedPromptView`
  // (src/surface/nodes/TypedPrompt.tsx) now special-cases an
  // `<inline-tags>` segment (parsed via the SAME shared, rendering-agnostic
  // parsers legacy used — src/utils/artificialSections.ts +
  // src/utils/inlineTagsPrompt.ts, no forked second parser) into
  // `.inline-tags-cards`/`.comment-card.inline-tags-card` chips, at parity
  // with legacy — see tests/surface/nativeGaps.test.tsx for the restored
  // coverage.

  it("inline-tag preamble is expanded by default", () => {
    const tags: InlineTag[] = [
      {
        id: "t1",
        messageId: "m1",
        selectedText: "ephemeral",
        comment: "yes",
        timestamp: "2026-04-30T15:01:27.976Z",
      },
    ];
    const content = buildInlineTagsPreamble(tags) + "\nPlease address.";
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message: makeUserMsg({ id: "u-inline-tags", content }),
        orchestrationMode: "native",
      }),
    );

    const chip = container.querySelector(".artificial-section-inline-tags");
    expect(chip?.classList.contains("expanded")).toBe(true);
    expect(
      chip?.querySelector(".artificial-section-header")?.getAttribute("aria-expanded"),
    ).toBe("true");
    expect(chip?.textContent).toContain("ephemeral");
    expect(chip?.textContent).toContain("yes");
    unmount();
  });

  it("mergeTagsIntoPrompt sends only the preamble when prompt is empty (no review fallback text)", () => {
    const tags: InlineTag[] = [
      {
        id: "t1",
        messageId: "m1",
        selectedText: "ephemeral",
        comment: "yes",
        timestamp: "2026-04-30T15:01:27.976Z",
      },
    ];
    const merged = mergeTagsIntoPrompt("   ", tags);
    expect(merged).toBe(buildInlineTagsPreamble(tags));
    expect(merged).not.toMatch(/address the user's comments/i);
    expect(merged).not.toMatch(/please review/i);
  });

  it("mergeTagsIntoPrompt appends the typed prompt after the preamble", () => {
    const tags: InlineTag[] = [
      {
        id: "t1",
        messageId: "m1",
        selectedText: "ephemeral",
        comment: "yes",
        timestamp: "2026-04-30T15:01:27.976Z",
      },
    ];
    expect(mergeTagsIntoPrompt("do the thing", tags)).toBe(
      buildInlineTagsPreamble(tags) + "\ndo the thing",
    );
  });

  // ── FR-FILE.0.1 enforcement ────────────────────────────────────
  // Pin that the file viewer auto-mounts when the user enters a
  // File-Mode session — the overlay state is DERIVED from
  // currentSession.working_mode + working_mode_meta (no local
  // shadowing). And that the persistent flavor hides the Done button.
  it("FR-FILE.0.1: selecting a persistent file-edit session auto-mounts the overlay and hides Done", async () => {
    const session = makeSession({
      id: "fe-persistent",
      name: "✏️ Edit — foo.txt",
      working_mode: "file_editing",
      working_mode_meta: fileEditMeta("/tmp/proj/foo.txt", "hello"),
    });
    const h = await renderApp({
      seed: {
        sessions: [session],
        files: { "/tmp/proj/foo.txt": "hello" },
      },
    });
    await h.selectSession(session.id);

    expect(h.$('[data-testid="file-editor-overlay"]')).not.toBeNull();
    expect(h.$('[data-testid="file-editor-cancel-btn"]')).not.toBeNull();
    expect(h.$('[data-testid="file-editor-done-btn"]')).toBeNull();
    h.unmount();
  });

  it("File-edit overlay does not mount for legacy single-file metadata", async () => {
    const session = makeSession({
      id: "fe-legacy",
      working_mode: "file_editing",
      working_mode_meta: {
        file_path: "/tmp/legacy.txt",
        original_content: "legacy",
        persistent: true,
      },
    });
    const h = await renderApp({
      seed: {
        sessions: [session],
        files: { "/tmp/legacy.txt": "legacy" },
      },
    });
    await h.selectSession(session.id);

    expect(h.$('[data-testid="file-editor-overlay"]')).toBeNull();
    h.unmount();
  });

  it("FR-FILE.0.1: sidebar filter includes persistent file-edit session, excludes temporal + prompt_engineering", async () => {
    const persistentFE = makeSession({
      id: "fe-p",
      name: "✏️ persistent",
      working_mode: "file_editing",
      working_mode_meta: fileEditMeta("/p.txt"),
    });
    const temporalFE = makeSession({
      id: "fe-t",
      name: "✏️ temporal",
      working_mode: "file_editing",
      working_mode_meta: fileEditMeta("/t.txt", "", false),
    });
    const eng = makeSession({
      id: "eng-1",
      name: "eng",
      working_mode: "prompt_engineering",
      working_mode_meta: {
        parent_session_id: "regular-1",
        temp_file_path: "/eng.md",
      },
    });
    const regular = makeSession({ id: "regular-1", name: "normal" });
    const h = await renderApp({
      seed: { sessions: [persistentFE, temporalFE, eng, regular] },
    });

    const sidebarIds = h.toJSON().sidebar.sessions.map((s) => s.id);
    expect(sidebarIds).toContain("fe-p");
    expect(sidebarIds).toContain("regular-1");
    expect(sidebarIds).not.toContain("fe-t");
    expect(sidebarIds).not.toContain("eng-1");
    h.unmount();
  });

  // ── Md flip-view + file-edit layout ───────────────────────────
  // A `working_mode: "file_editing"` session (array-shaped
  // working_mode_meta, the only currently-supported shape per
  // FR-FILE.0.1) always renders through MultiFileEditor, which mounts
  // each per-file FileEditor with `diskWritable={false}` (real project
  // files the agent owns — see the prop doc in FileEditor.tsx). In
  // FileEditorPrimitives.tsx, `editing = mdEditing || !diskWritable`,
  // so `.md` files there are ALWAYS shown in (read-only) Monaco, never
  // the rendered-markdown view — because comment/selection capture
  // (`useMonacoSelectionCapture`, FR-FILE.0.5) is only wired to the
  // Monaco editor, not the ReactMarkdown formatted view. So the
  // FR-FILE.0.16 markdown view/edit toggle (format-by-default,
  // double-click to raw-edit) is unreachable for this working mode —
  // it only applies to the diskWritable=true FileEditor usage (the
  // prompt-engineering flow), which is unit-tested directly in
  // tests/markdown-edit-view.test.tsx. This is a possible FR-FILE.0.16
  // gap for file_editing sessions, flagged for a product decision
  // rather than silently changed here (fixing it risks breaking
  // FR-FILE.0.5 comment-selection on markdown files).
  it("file-editing md session renders a read-only Monaco editor, not the FR-FILE.0.16 formatted view", async () => {
    const session = makeSession({
      id: "fe-md-monaco",
      name: "✏️ Edit — x.md",
      working_mode: "file_editing",
      working_mode_meta: fileEditMeta("/tmp/x.md"),
    });
    const h = await renderApp({
      seed: { sessions: [session], files: { "/tmp/x.md": "# Hello" } },
    });
    await h.selectSession(session.id);

    expect(h.$('[data-testid="eng-file-md-monaco"]')).not.toBeNull();
    expect(h.$('[data-testid="eng-file-md-formatted"]')).toBeNull();
    h.unmount();
  });

  it("Layout: FileEditor defaults to 'file' view mode (not 'diff') on first open", async () => {
    const session = makeSession({
      id: "fe-default-file",
      working_mode: "file_editing",
      working_mode_meta: fileEditMeta("/tmp/z.md"),
    });
    const h = await renderApp({
      seed: { sessions: [session], files: { "/tmp/z.md": "" } },
    });
    await h.selectSession(session.id);

    const fileBtn = h.$('[data-testid="eng-view-file"]');
    const diffBtn = h.$('[data-testid="eng-view-diff"]');
    expect(fileBtn?.className).toContain("active");
    expect(diffBtn?.className).not.toContain("active");
    h.unmount();
  });

  it("Layout: sidebar auto-collapses and resizer hides while file-edit overlay is active", async () => {
    localStorage.removeItem("better-agent-sidebar-minimized");
    setDesktopViewport();
    const fe = makeSession({
      id: "fe-trim",
      working_mode: "file_editing",
      working_mode_meta: fileEditMeta("/tmp/w.md"),
    });
    const h = await renderApp({
      seed: {
        sessions: [fe],
        files: { "/tmp/w.md": "" },
        uiSelection: {
          selected_project: null,
          remembered_session_by_project: {},
          open_session_tab_ids: [fe.id],
        },
      },
    });

    const sidebar = h.$(".sidebar") as HTMLElement | null;
    expect(sidebar?.className).toContain("sidebar-minimized");
    expect(sidebar?.style.width).toBe(`${SIDEBAR_MINIMIZED_WIDTH}px`);
    expect(h.$(".sidebar-resizer")).toBeNull();
    expect(h.$(".session-list-wrapper")).toBeNull();
    expect(h.$('[aria-label="sidebar.expand"]')).toBeNull();
    h.unmount();
  });

  it("Layout: sidebar expands back after leaving file-edit overlay", async () => {
    localStorage.removeItem("better-agent-sidebar-minimized");
    setDesktopViewport();
    const fe = makeSession({
      id: "fe-back",
      working_mode: "file_editing",
      working_mode_meta: fileEditMeta("/tmp/back.md"),
    });
    const regular = makeSession({
      id: "regular-back",
      name: "regular",
      topbar_pinned: true,
      topbar_pinned_at: new Date().toISOString(),
    });
    const h = await renderApp({
      seed: {
        sessions: [fe, regular],
        files: { "/tmp/back.md": "" },
        uiSelection: {
          selected_project: null,
          remembered_session_by_project: {},
          open_session_tab_ids: [fe.id, regular.id],
        },
      },
    });
    // With two open tabs seeded, which one resolves as "current" first
    // (auto-select vs. tab-restore) is a race. Click fe's own topbar tab
    // (not the sidebar row — the sidebar list is hidden while minimized,
    // i.e. exactly when fe is already current) to deterministically make
    // it current before asserting the sidebar collapses for it.
    // Seeding two open tabs kicks off a multi-hop async cascade (auth ->
    // sessions -> ui-selection -> topbar-pinned -> per-session summary/
    // "opened" fetches) before SessionTabs renders both movement keys.
    // renderApp's initial flush only guarantees one settle cycle, which can
    // land mid-cascade; wait for both tabs to actually be in the DOM before
    // clicking, rather than assuming a fixed number of flushes sufficed.
    await h.waitFor(() => h.$$("[data-tab-movement-key]").length === 2);
    await h.click(`[data-tab-movement-key="${fe.id}"] .session-tab`);
    expect((h.$(".sidebar") as HTMLElement).style.width).toBe(`${SIDEBAR_MINIMIZED_WIDTH}px`);

    await h.click(`[data-tab-movement-key="${regular.id}"] .session-tab`);
    expect((h.$(".sidebar") as HTMLElement).className).not.toContain("sidebar-minimized");
    expect((h.$(".sidebar") as HTMLElement).style.width).toBe("280px");
    expect(h.$(".sidebar-resizer")).not.toBeNull();
    h.unmount();
  });

  it("Layout: desktop sidebar can be minimized and restored", async () => {
    localStorage.removeItem("better-agent-sidebar-minimized");
    Object.defineProperty(window, "innerWidth", {
      value: 1400,
      configurable: true,
    });
    const session = makeSession({ id: "sidebar-minimize" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const collapse = h.$('[aria-label="sidebar.minimize"]') as HTMLButtonElement | null;
    expect(collapse).not.toBeNull();
    await h.click('[aria-label="sidebar.minimize"]');

    const sidebar = h.$(".sidebar") as HTMLElement | null;
    expect(sidebar?.className).toContain("sidebar-minimized");
    expect(sidebar?.style.width).toBe("52px");
    expect(h.$(".sidebar-resizer")).toBeNull();
    expect(h.$(".session-list-wrapper")).toBeNull();

    const expand = h.$('[aria-label="sidebar.expand"]') as HTMLButtonElement | null;
    expect(expand).not.toBeNull();
    await h.click('[aria-label="sidebar.expand"]');

    expect((h.$(".sidebar") as HTMLElement).className).not.toContain("sidebar-minimized");
    expect(h.$(".sidebar-resizer")).not.toBeNull();
    h.unmount();
  });

  it("Layout: inner chat-vs-file divider defaults to ~50% of (innerWidth - collapsed sidebar) on first open", async () => {
    localStorage.removeItem("better-agent-sidebar-minimized");
    setDesktopViewport();
    const fe = makeSession({
      id: "fe-divider",
      working_mode: "file_editing",
      working_mode_meta: fileEditMeta("/tmp/div.md"),
    });
    const h = await renderApp({
      seed: {
        sessions: [fe],
        files: { "/tmp/div.md": "" },
        uiSelection: {
          selected_project: null,
          remembered_session_by_project: {},
          open_session_tab_ids: [fe.id],
        },
      },
    });

    const expected = Math.max(
      500,
      Math.floor((1400 - SIDEBAR_MINIMIZED_WIDTH) / 2),
    );
    const fv = h.$(".prompt-eng-fileviewer") as HTMLElement | null;
    expect(fv).not.toBeNull();
    expect(fv!.style.width).toBe(`${expected}px`);
    h.unmount();
  });

  // Negative control: pins that the old-format preamble (the shape we
  // shipped before the fix) actually does lose the user's comment in the
  // rendered bubble — proving the regression test above is meaningful.
  it("legacy preamble shape with [Selected text]:/ʼʼʼ loses User comment line", async () => {
    const legacyContent = [
      "The user reviewed some code/text and left the following inline comments:",
      "",
      "---",
      "[Selected text]:",
      '"""',
      "ephemeral",
      '"""',
      "",
      "[User comment]: yes",
      "",
      "---",
      "",
      "Please address the user's comments.",
    ].join("\n");
    const session = makeSession({
      messages: [makeUserMsg({ id: "u1", content: legacyContent })],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    const u1 = h.toJSON().chat.messages.find((m) => m.id === "u1")!;
    // Markdown link-reference parsing eats the entire "[User comment]: yes"
    // line — that's the bug. If this assertion ever flips, the renderer
    // changed and the new-format fix may not be needed anymore.
    expect(u1.text).not.toMatch(/User comment:\s*yes/);
    h.unmount();
  });
});
