import { describe, expect, it } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import { getMobileHandlers } from "../src/contexts/MobileHandlersContext";
import type { InlineTag } from "../src/types/inlineTag";
import type { Note } from "../src/types";

async function waitDraftDebounce(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 350));
  });
}

describe("notes and inline comments", () => {
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
    expect(sent?.send_mode).toBe("alter");
    expect(String(sent?.prompt ?? "")).toContain("queued work");
    expect((String(sent?.prompt ?? "").match(/queued comment/g) ?? [])).toHaveLength(1);

    h.unmount();
  });

  it("clears inline comments when the final applied note is deleted before send", async () => {
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
