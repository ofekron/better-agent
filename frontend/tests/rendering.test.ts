import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import React from "react";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";
import { MessageBubble } from "../src/components/MessageBubble";
import { buildInlineTagsPreamble } from "../src/utils/inlineTagsPrompt";
import type { InlineTag } from "../src/types/inlineTag";
import { ASK_SINGLETON_ID } from "../src/askSession";

describe("message rendering", () => {
  it("Ask description lives above the prompt and follows picker resolution", async () => {
    const askEmpty = makeSession({
      id: ASK_SINGLETON_ID,
      name: "Ask",
      orchestration_mode: "virtual",
      messages: [],
    });
    const hEmpty = await renderApp({ seed: { sessions: [askEmpty] } });
    await hEmpty.selectSession(ASK_SINGLETON_ID);
    expect(hEmpty.$(".input-area .ask-greeting")).not.toBeNull();
    expect(hEmpty.$('[data-testid="chat-messages"] .ask-greeting')).toBeNull();
    hEmpty.unmount();

    const askResult = {
      session_ids: ["target"],
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
      seed: { sessions: [askPending, makeSession({ id: "target" })] },
    });
    await hPending.selectSession(ASK_SINGLETON_ID);
    expect(hPending.$(".input-area .ask-greeting")).toBeNull();
    hPending.unmount();

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
      seed: { sessions: [askResolved, makeSession({ id: "target" })] },
    });
    await hResolved.selectSession(ASK_SINGLETON_ID);
    expect(hResolved.$(".input-area .ask-greeting")).not.toBeNull();
    hResolved.unmount();
  });

  it("auto-collapses prior turns and only renders the latest assistant", async () => {
    const session = makeSession({
      messages: [
        makeUserMsg({ id: "u1", content: "old" }),
        makeAssistantMsg({ id: "a1", content: "old reply" }),
        makeUserMsg({ id: "u2", content: "new" }),
        makeAssistantMsg({ id: "a2", content: "new reply" }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const ids = h.toJSON().chat.messages.map((m) => m.id);
    expect(ids).toContain("u1");
    expect(ids).toContain("u2");
    expect(ids).toContain("a2");
    // Prior turn's assistant is collapsed and therefore not in the DOM.
    expect(ids).not.toContain("a1");
    h.unmount();
  });

  it("'Expand All' toolbar button expands collapsed prior turns", async () => {
    const session = makeSession({
      messages: [
        makeUserMsg({ id: "u1", content: "old" }),
        makeAssistantMsg({ id: "a1", content: "old reply" }),
        makeUserMsg({ id: "u2", content: "new" }),
        makeAssistantMsg({ id: "a2", content: "new reply" }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    expect(h.toJSON().chat.messages.map((m) => m.id)).not.toContain("a1");

    await h.clickByText(/^Expand All$/);

    const ids = h.toJSON().chat.messages.map((m) => m.id);
    expect(ids).toContain("a1");
    expect(ids).toContain("a2");
    h.unmount();
  });

  it("user message status badge renders 'sending' on optimistic bubble", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.typeAndSend("queued");

    const sending = h.toJSON().chat.messages.find(
      (m) => m.role === "user" && m.status === "sending",
    );
    expect(sending).toBeDefined();
    expect(sending!.text).toContain("queued");
    h.unmount();
  });

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

  it("error event shows the error text on the failed user bubble", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.typeAndSend("oops");

    h.emit({ type: "turn_start", data: { session_id: session.id } });
    h.emit({
      type: "error",
      data: { error: "Backend exploded", session_id: session.id },
    });
    await h.flush();

    const failed = h.toJSON().chat.messages.find(
      (m) => m.role === "user" && m.status === "error",
    );
    expect(failed).toBeDefined();
    // Error chrome renders the message somewhere on the user bubble.
    const errorBlock = h.$(".message-status.status-error");
    expect(errorBlock).not.toBeNull();
    expect(errorBlock!.textContent).toContain("Failed");
    h.unmount();
  });

  it("a persisted assistant with error=true renders error chrome", async () => {
    const session = makeSession({
      messages: [
        makeUserMsg({ id: "u", content: "trigger" }),
        makeAssistantMsg({
          id: "a",
          content: "API Error: 500",
          error: true,
          errorText: "API Error: something broke",
        }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    expect(h.$(".message-status.status-error")).not.toBeNull();
    h.unmount();
  });

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

  it("renders an explicit WebSearch tool_result when present", async () => {
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
                  id: "ws_1",
                  name: "WebSearch",
                  input: { query: "embedding models" },
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
                  tool_use_id: "ws_1",
                  content: "Search results for embedding models returned.",
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

    const tool = container.querySelector(".tool-call");
    expect(tool).not.toBeNull();
    expect(tool!.textContent ?? "").toContain("WebSearch");
    expect(tool!.querySelector(".tool-result-inline, .tool-result-block")).not.toBeNull();
    expect(tool!.textContent ?? "").toContain("Search results for embedding models returned.");
    unmount();
  });

  it("a stopped assistant renders the stopped indicator", async () => {
    const session = makeSession({
      messages: [
        makeUserMsg({ id: "u", content: "go" }),
        makeAssistantMsg({
          id: "a",
          content: "partial",
          stopped_at: new Date().toISOString(),
        }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    // StoppedIndicator renders inside the assistant message_content area.
    const assistant = h.$('[data-testid="assistant-message"]');
    expect(assistant).not.toBeNull();
    expect(assistant!.textContent ?? "").toMatch(/[Ss]topped/);
    h.unmount();
  });

  it("empty session shows the placeholder welcome text", async () => {
    const session = makeSession({ messages: [] });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const empty = h.$(".chat-empty");
    expect(empty).not.toBeNull();
    expect(empty!.textContent).toContain("Better Agent");
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

  it("Trace and Raw JSON toggle buttons are present in the toolbar", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const labels = Array.from(h.$$(".raw-toggle")).map((b) => b.textContent);
    expect(labels).toContain("Trace");
    expect(labels).toContain("Raw JSON");
    h.unmount();
  });

  // Regression: pre-fix the preamble used `[Selected text]:` and `"""`,
  // which ReactMarkdown parsed as link reference definitions and ate the
  // labels + their following lines. The user's "yes" comment vanished
  // from the rendered bubble even though the text was on the wire.
  it("inline-tag preamble renders selected text and user comments visibly", async () => {
    const tags: InlineTag[] = [
      {
        id: "t1",
        messageId: "m1",
        selectedText: "ephemeral",
        comment: "yes",
        timestamp: "2026-04-30T15:01:27.976Z",
      },
      {
        id: "t2",
        messageId: "m1",
        selectedText: "Replace the chat input area temporarily?",
        comment: "yes",
        timestamp: "2026-04-30T15:01:49.745Z",
      },
    ];
    const content = buildInlineTagsPreamble(tags) + "\nPlease address.";
    const session = makeSession({
      messages: [makeUserMsg({ id: "u1", content })],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const u1 = h.toJSON().chat.messages.find((m) => m.id === "u1");
    expect(u1).toBeDefined();
    expect(u1!.text).toContain("ephemeral");
    expect(u1!.text).toContain("Replace the chat input area temporarily?");
    // Both "User comment: yes" lines must survive markdown rendering.
    const yesCount = (u1!.text.match(/User comment:\s*yes/g) ?? []).length;
    expect(yesCount).toBe(2);
    h.unmount();
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
      working_mode_meta: {
        file_path: "/tmp/proj/foo.txt",
        original_content: "hello",
        persistent: true,
      },
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

  it("FR-FILE.0.1: sidebar filter includes persistent file-edit session, excludes temporal + prompt_engineering", async () => {
    const persistentFE = makeSession({
      id: "fe-p",
      name: "✏️ persistent",
      working_mode: "file_editing",
      working_mode_meta: { file_path: "/p.txt", persistent: true },
    });
    const temporalFE = makeSession({
      id: "fe-t",
      name: "✏️ temporal",
      working_mode: "file_editing",
      working_mode_meta: { file_path: "/t.txt" },
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
  it("FR-MD-FLIP: persistent file-edit md session opens to formatted view, not Monaco", async () => {
    const session = makeSession({
      id: "fe-md-formatted",
      name: "✏️ Edit — x.md",
      working_mode: "file_editing",
      working_mode_meta: {
        file_path: "/tmp/x.md",
        original_content: "",
        persistent: true,
      },
    });
    const h = await renderApp({
      seed: { sessions: [session], files: { "/tmp/x.md": "# Hello" } },
    });
    await h.selectSession(session.id);

    expect(h.$('[data-testid="eng-file-md-formatted"]')).not.toBeNull();
    expect(h.$('[data-testid="eng-file-md-monaco"]')).toBeNull();
    h.unmount();
  });

  it("FR-MD-FLIP: double-click on the formatted markdown container flips to Monaco edit", async () => {
    const session = makeSession({
      id: "fe-md-flip",
      working_mode: "file_editing",
      working_mode_meta: {
        file_path: "/tmp/y.md",
        original_content: "",
        persistent: true,
      },
    });
    const h = await renderApp({
      seed: { sessions: [session], files: { "/tmp/y.md": "# Hi" } },
    });
    await h.selectSession(session.id);

    const formatted = h.$('[data-testid="eng-file-md-formatted"]');
    expect(formatted).not.toBeNull();
    formatted!.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
    await h.flush();

    expect(h.$('[data-testid="eng-file-md-monaco"]')).not.toBeNull();
    expect(h.$('[data-testid="eng-file-md-formatted"]')).toBeNull();
    h.unmount();
  });

  it("FR-MD-FLIP: single-click does NOT flip the formatted view to edit mode", async () => {
    const session = makeSession({
      id: "fe-md-single",
      working_mode: "file_editing",
      working_mode_meta: {
        file_path: "/tmp/single.md",
        original_content: "",
        persistent: true,
      },
    });
    const h = await renderApp({
      seed: { sessions: [session], files: { "/tmp/single.md": "# nope" } },
    });
    await h.selectSession(session.id);

    const formatted = h.$('[data-testid="eng-file-md-formatted"]');
    expect(formatted).not.toBeNull();
    formatted!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await h.flush();

    expect(h.$('[data-testid="eng-file-md-formatted"]')).not.toBeNull();
    expect(h.$('[data-testid="eng-file-md-monaco"]')).toBeNull();
    h.unmount();
  });

  it("Layout: FileEditor defaults to 'file' view mode (not 'diff') on first open", async () => {
    const session = makeSession({
      id: "fe-default-file",
      working_mode: "file_editing",
      working_mode_meta: {
        file_path: "/tmp/z.md",
        original_content: "",
        persistent: true,
      },
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

  it("Layout: sidebar trims to 200px and resizer hides while file-edit overlay is active", async () => {
    const fe = makeSession({
      id: "fe-trim",
      working_mode: "file_editing",
      working_mode_meta: {
        file_path: "/tmp/w.md",
        original_content: "",
        persistent: true,
      },
    });
    const h = await renderApp({
      seed: { sessions: [fe], files: { "/tmp/w.md": "" } },
    });
    await h.selectSession(fe.id);

    const sidebar = h.$(".sidebar") as HTMLElement | null;
    expect(sidebar?.style.width).toBe("200px");
    expect(h.$(".sidebar-resizer")).toBeNull();
    h.unmount();
  });

  it("Layout: sidebar restored to its persisted width after leaving file-edit overlay", async () => {
    const fe = makeSession({
      id: "fe-back",
      working_mode: "file_editing",
      working_mode_meta: {
        file_path: "/tmp/back.md",
        original_content: "",
        persistent: true,
      },
    });
    const regular = makeSession({ id: "regular-back", name: "regular" });
    const h = await renderApp({
      seed: { sessions: [fe, regular], files: { "/tmp/back.md": "" } },
    });
    await h.selectSession(fe.id);
    expect((h.$(".sidebar") as HTMLElement).style.width).toBe("200px");

    await h.selectSession(regular.id);
    expect((h.$(".sidebar") as HTMLElement).style.width).not.toBe("200px");
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

  it("Layout: inner chat-vs-file divider defaults to ~50% of (innerWidth - 200) on first open", async () => {
    // Pin window.innerWidth deterministically; happy-dom defaults to 1024.
    Object.defineProperty(window, "innerWidth", {
      value: 1400,
      configurable: true,
    });
    const fe = makeSession({
      id: "fe-divider",
      working_mode: "file_editing",
      working_mode_meta: {
        file_path: "/tmp/div.md",
        original_content: "",
        persistent: true,
      },
    });
    const h = await renderApp({
      seed: { sessions: [fe], files: { "/tmp/div.md": "" } },
    });
    await h.selectSession(fe.id);

    const expected = Math.max(500, Math.floor((1400 - 200) / 2)); // 600
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
