/**
 * Regression test — the prompt textarea must clear after a send.
 *
 * Bug: `App.handleSend` (the `onSend` wired into InputArea) called
 * `sendPrompt(...)` WITHOUT returning its result, so it resolved to
 * `undefined`. InputArea.submitDraft awaits that as `sent`; a falsy
 * `sent` is treated as "send failed" and restores the just-cleared
 * draft (`setLocalDraft(trimmed)`) — leaving the user's text sitting
 * in the box after a successful send. The sibling steer/interrupt
 * handlers are arrow bodies that implicitly return the promise, so
 * only the main send path regressed.
 *
 * Invariant: after a successful WS send, the textarea is empty.
 */
import { describe, expect, it } from "vitest";
import "../src/i18n";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";

describe("prompt input clears on send", () => {
  it("empties the textarea after a successful send", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    await h.typeAndSend("hello world");

    // The send must have actually gone out over the WS...
    const sent = h.outbound.findLast((f) => f.type === "send_message");
    expect(sent?.prompt).toBe("hello world");

    // ...and the box must be empty, not restored to the sent text.
    expect(h.toJSON().input.text).toBe("");
    h.unmount();
  });

  it("warns before sending when the session has notes", async () => {
    const session = makeSession({
      notes: [
        {
          id: "note-1",
          text: "Remember this before sending.",
          created_at: new Date().toISOString(),
        },
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    await h.typeAndSend("hello with notes");

    expect(h.outbound.findLast((f) => f.type === "send_message")).toBeUndefined();
    expect(h.$(".modal-overlay")?.textContent).toContain("Session has notes");
    expect(h.toJSON().input.text).toBe("hello with notes");

    await h.clickByText("Send anyway");

    const sent = h.outbound.findLast((f) => f.type === "send_message");
    expect(sent?.prompt).toBe("hello with notes");
    expect(h.toJSON().input.text).toBe("");
    h.unmount();
  });

  it("opens notes instead of sending when reviewing the warning", async () => {
    const session = makeSession({
      notes: [
        {
          id: "note-1",
          text: "Review me first.",
          created_at: new Date().toISOString(),
        },
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    await h.typeAndSend("hold send");
    await h.clickByText("Review notes");

    expect(h.outbound.findLast((f) => f.type === "send_message")).toBeUndefined();
    expect(h.toJSON().input.text).toBe("hold send");
    expect(h.$(".notes-panel")?.textContent).toContain("Review me first.");
    h.unmount();
  });
});
