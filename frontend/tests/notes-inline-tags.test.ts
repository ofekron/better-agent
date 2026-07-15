import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import "../src/i18n";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import { getMobileHandlers } from "../src/contexts/MobileHandlersContext";
import type { InlineTag } from "../src/types/inlineTag";
import type { Note } from "../src/types";

// Requires fake timers (see engageFakeTimers) to be active in the test.
async function waitDraftDebounce(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(350);
  });
}

// Fake setTimeout so the App.tsx draft debounce can be advanced instantly.
// shouldAdvanceTime keeps the harness's real-time setTimeout(0) flushes alive.
function engageFakeTimers(): void {
  vi.useFakeTimers({
    shouldAdvanceTime: true,
    advanceTimeDelta: 1,
    toFake: ["setTimeout", "clearTimeout"],
  });
}

describe("notes and inline comments", () => {
  const defaultViewport = {
    width: window.innerWidth,
    height: window.innerHeight,
  };

  function setViewport(width: number, height: number): void {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: height });
    window.dispatchEvent(new Event("resize"));
  }

  beforeEach(() => {
    setViewport(1280, 900);
  });

  afterEach(() => {
    setViewport(defaultViewport.width, defaultViewport.height);
    vi.useRealTimers();
  });

  it("keeps newly added comments in the comments panel while a prompt is queued", async () => {
    const session = makeSession({
      messages: [{
        id: "u1",
        role: "user",
        content: "selected text",
        events: [],
        timestamp: "2026-06-30T00:00:00.000Z",
      }],
      queued_prompts: [{
        id: "q1",
        content: "queued work",
        kind: "queued_behind",
      }],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    act(() => {
      getMobileHandlers().addTag?.("selected text", "queued comment", "u1");
    });
    await h.flush();

    expect(h.$(".comments-panel-card-comment")?.textContent).toContain("queued comment");
    expect(h.$('[data-testid="queued-prompt-banner"]')?.textContent).toContain("queued work");
    expect(h.$('[data-testid="queued-prompt-banner"]')?.textContent).not.toContain("queued comment");
    expect(h.outbound.some((frame) => frame.type === "update_queued")).toBe(false);
    expect(
      h.restCalls.some(
        (call) =>
          call.method === "POST" &&
          call.path === `/api/sessions/${session.id}/tags` &&
          (call.body as { comment?: string } | undefined)?.comment === "queued comment",
      ),
    ).toBe(true);

    await h.click('[data-testid="send-btn"]');
    const sent = h.outbound.find((frame) => frame.type === "send_message");
    expect(sent?.send_mode).toBe("queue");
    expect(String(sent?.prompt ?? "")).not.toContain("queued work");
    expect((String(sent?.prompt ?? "").match(/queued comment/g) ?? [])).toHaveLength(1);

    h.unmount();
  });

  it("keeps a newly queued prompt visible when a stale queue snapshot arrives", async () => {
    const session = makeSession({
      messages: [{
        id: "u1",
        role: "user",
        content: "selected text",
        events: [],
        timestamp: "2026-07-09T00:00:00.000Z",
      }],
      queued_prompts: [{
        id: "q1",
        client_id: "c1",
        content: "first queued work",
        kind: "queued_behind",
      }],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "prompt_queued",
      data: {
        app_session_id: session.id,
        queued_id: "q2",
        prompt_preview: "second queued work",
        send_mode: "queue",
        queue_position: 1,
        client_id: "c2",
      },
    });
    await h.flush();

    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: session.id,
        patch: {
          queued_prompts: [{
            id: "q1",
            client_id: "c1",
            content: "first queued work",
            kind: "queued_behind",
          }],
        },
      },
    });
    await h.flush();

    const banners = h.$$('[data-testid="queued-prompt-banner"]');
    expect(banners).toHaveLength(2);
    expect(banners[0]?.textContent).toContain("first queued work");
    expect(banners[1]?.textContent).toContain("second queued work");

    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: session.id,
        patch: {
          queued_prompts: [
            {
              id: "q1",
              client_id: "c1",
              content: "first queued work",
              kind: "queued_behind",
            },
            {
              id: "q2",
              client_id: "c2",
              content: "second queued work",
              kind: "queued_behind",
            },
          ],
        },
      },
    });
    await h.flush();
    expect(h.$$('[data-testid="queued-prompt-banner"]')).toHaveLength(2);

    act(() => {
      getMobileHandlers().addTag?.("selected text", "queued comment", "u1");
    });
    await h.flush();
    await h.click('[data-testid="send-btn"]');

    const sent = h.outbound.findLast((frame) => frame.type === "send_message");
    expect(sent?.send_mode).toBe("queue");
    expect(String(sent?.prompt ?? "")).not.toContain("second queued work");
    expect(String(sent?.prompt ?? "")).not.toContain("first queued work");
    expect((String(sent?.prompt ?? "").match(/queued comment/g) ?? [])).toHaveLength(1);

    h.unmount();
  });

  it("removes a preserved queued prompt after the next authoritative snapshot", async () => {
    const session = makeSession({
      queued_prompts: [{
        id: "q1",
        client_id: "c1",
        content: "first queued work",
        kind: "queued_behind",
      }],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "prompt_queued",
      data: {
        app_session_id: session.id,
        queued_id: "q2",
        prompt_preview: "second queued work",
        send_mode: "queue",
        queue_position: 1,
        client_id: "c2",
      },
    });
    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: session.id,
        patch: {
          queued_prompts: [{
            id: "q1",
            client_id: "c1",
            content: "first queued work",
            kind: "queued_behind",
          }],
        },
      },
    });
    await h.flush();
    expect(h.$$('[data-testid="queued-prompt-banner"]')).toHaveLength(2);

    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: session.id,
        patch: { queued_prompts: [] },
      },
    });
    await h.flush();

    expect(h.$$('[data-testid="queued-prompt-banner"]')).toHaveLength(0);

    h.unmount();
  });

  it("keeps the right panel open when the first comment starts in edit mode", async () => {
    const session = makeSession({
      messages: [{
        id: "u1",
        role: "user",
        content: "selected text",
        events: [],
        timestamp: "2026-07-05T00:00:00.000Z",
      }],
      right_panel_open: false,
      right_panel_active_tab: null,
      right_panel_auto_opened_by: [],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    const releaseTagPost = h.backend.holdNext("POST", `/api/sessions/${session.id}/tags`);
    const releasePanelPatch = h.backend.holdNext("PATCH", `/api/sessions/${session.id}/right-panel`);

    act(() => {
      getMobileHandlers().addTag?.("selected text", "", "u1");
    });
    await h.flush();

    expect(h.$(".right-panel:not(.right-panel-collapsed)")).toBeTruthy();
    expect(h.$(".right-panel-tab.active")?.textContent).toContain("Comments");
    expect(h.$(".comments-panel-card-textarea")).toBeTruthy();

    const tagPostIndex = h.restCalls.findIndex(
      (call) => call.method === "POST" && call.path === `/api/sessions/${session.id}/tags`,
    );
    expect(tagPostIndex).toBeGreaterThanOrEqual(0);
    expect(
      h.restCalls.some(
        (call) => call.method === "PATCH" && call.path === `/api/sessions/${session.id}/right-panel`,
      ),
    ).toBe(false);

    h.emit({ type: "session_reconciled", data: { root_id: session.id } });
    await h.flush();
    expect(h.$(".right-panel:not(.right-panel-collapsed)")).toBeTruthy();
    expect(h.$(".comments-panel-card-textarea")).toBeTruthy();
    expect(
      h.restCalls.some(
        (call) => call.method === "PATCH" && call.path === `/api/sessions/${session.id}/right-panel`,
      ),
    ).toBe(false);

    releaseTagPost();
    await h.flush();

    const panelPatchIndex = h.restCalls.findIndex(
      (call) => call.method === "PATCH" && call.path === `/api/sessions/${session.id}/right-panel`,
    );
    expect(panelPatchIndex).toBeGreaterThan(tagPostIndex);
    h.emit({ type: "session_reconciled", data: { root_id: session.id } });
    await h.flush();
    expect(h.$$(".comments-panel-card")).toHaveLength(1);
    expect(h.$(".right-panel:not(.right-panel-collapsed)")).toBeTruthy();

    releasePanelPatch();
    await h.flush();

    expect(h.backend.state.sessions[0].inline_tags).toHaveLength(1);
    expect(h.backend.state.sessions[0].right_panel_open).toBe(true);

    h.unmount();
  });

  it("loads inline comments into the new-session modal prompt", async () => {
    const tag: InlineTag = {
      id: "tag-1",
      messageId: "u1",
      selectedText: "selected text",
      comment: "carry this comment",
      timestamp: "2026-07-01T00:00:00.000Z",
    };
    const session = makeSession({
      messages: [{
        id: "u1",
        role: "user",
        content: "selected text",
        events: [],
        timestamp: "2026-07-01T00:00:00.000Z",
      }],
      inline_tags: [tag],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    fireEvent.change(h.$('[data-testid="input-textarea"]')!, {
      target: { value: "start this separately" },
    });
    await h.flush();
    await h.click('[aria-label="More actions"]');
    await h.click('[data-testid="send-to-new-session-btn"]');

    const modalPrompt = h.$(".ns-investigation-textarea") as HTMLTextAreaElement | null;
    expect(modalPrompt?.value).toContain("<inline-tags>");
    expect(modalPrompt?.value).toContain("carry this comment");
    expect(modalPrompt?.value).toContain("start this separately");

    h.unmount();
  });

  it("clears inline comments when the final applied note is deleted before send", async () => {
    engageFakeTimers();
    const note: Note = {
      id: "note-1",
      text: "apply this note",
      created_at: "2026-06-22T00:00:00.000Z",
    };
    const tag: InlineTag = {
      id: "tag-1",
      messageId: "a1",
      selectedText: "selected text",
      comment: "stale inline comment",
      timestamp: "2026-06-22T00:00:00.000Z",
    };
    const session = makeSession({
      notes: [note],
      inline_tags: [tag],
      right_panel_open: true,
      right_panel_active_tab: "notes",
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    fireEvent.mouseDown(h.$(".note-send-btn")!);
    await h.flush();
    await waitDraftDebounce();
    expect(h.toJSON().input.text).toBe(note.text);

    fireEvent.mouseDown(h.$(".note-remove-btn")!);
    await h.flush();
    await h.click('[data-testid="send-btn"]');

    const sent = h.outbound.find((frame) => frame.type === "send_message");
    expect(sent).toMatchObject({
      type: "send_message",
      prompt: note.text,
    });
    expect(String(sent?.prompt ?? "")).not.toContain("<inline-tags>");
    expect(String(sent?.prompt ?? "")).not.toContain(tag.comment);
    expect(
      h.restCalls.some(
        (call) =>
          call.method === "DELETE" &&
          call.path === `/api/sessions/${session.id}/tags`,
      ),
    ).toBe(true);

    h.unmount();
  });
});
