import { describe, expect, it } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import type { InlineTag } from "../src/types/inlineTag";
import type { Note } from "../src/types";

async function waitDraftDebounce(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 350));
  });
}

describe("notes and inline comments", () => {
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
