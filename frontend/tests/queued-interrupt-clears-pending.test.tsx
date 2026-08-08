import { describe, expect, it, beforeEach } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import type { WSEvent } from "../src/types";

// Regression for "queued prompt appears as Sending". The user_message_queued
// lifecycle handler clears the optimistic "Sending…" bubble only for
// banner-worthy kinds. It previously hardcoded just "queued_behind", so a
// prompt queued as an "interrupt" kept its Sending bubble AND appeared in the
// queue banner simultaneously. The fix routes the decision through
// isBannerQueuedKind (queued_behind + interrupt), matching the unconditional
// clear already in onPromptQueued.
//
// The native surface's own send path renders a provisional turn in the tree,
// not this legacy pendingMessages "Sending…" bubble — so the test forces the
// legacy surface to exercise the bubble path (still reached in production as
// the fallback for native users sending with images/files/supervisor or a
// down socket, and by opted-out users).

const SID = "sess-legacy";
type Harness = Awaited<ReturnType<typeof renderApp>>;

async function legacyHarness(): Promise<Harness> {
  localStorage.setItem("ba.surface_native", "0");
  // A prior assistant turn makes the session non-fresh, skipping the
  // advisory project-suggestion branch in sendPrompt.
  const session = makeSession({
    id: SID,
    messages: [
      {
        id: "m1",
        role: "assistant",
        content: "prior turn",
        events: [],
        timestamp: "2026-01-01T00:00:00Z",
        isStreaming: false,
      },
    ],
  });
  const h = await renderApp({ seed: { sessions: [session] } });
  await h.selectSession(SID);
  await h.flush();
  return h;
}

// The optimistic pending message's id is its client_id, minted as
// "pending-<ts>" in sendPrompt — the most stable signal that the bubble
// exists, independent of how status is rendered.
function pendingBubbleId(h: Harness): string | undefined {
  const msg = h
    .toJSON()
    .chat.messages.find(
      (m) => m.role === "user" && typeof m.id === "string" && m.id.startsWith("pending-"),
    );
  return msg?.id;
}

function emitQueued(h: Harness, kind: string, clientId: string): void {
  h.emit({
    type: "user_message_queued",
    data: {
      app_session_id: SID,
      lifecycle_msg_id: "life-1",
      client_id: clientId,
      kind,
    },
  } as WSEvent);
}

describe("user_message_queued clears the Sending bubble", () => {
  beforeEach(() => {
    localStorage.removeItem("ba.surface_native");
  });

  it("an interrupt-queued prompt clears the optimistic Sending bubble", async () => {
    const h = await legacyHarness();
    await h.typeAndSend("hello");
    await h.waitFor(() => pendingBubbleId(h) !== undefined);
    const clientId = pendingBubbleId(h);
    expect(clientId).toMatch(/^pending-/);

    emitQueued(h, "interrupt", clientId!);
    await h.waitFor(() => pendingBubbleId(h) === undefined);

    expect(pendingBubbleId(h)).toBeUndefined();
    h.unmount();
  });

  it("a send-kind queued event keeps the Sending bubble (no over-clear)", async () => {
    const h = await legacyHarness();
    await h.typeAndSend("hello");
    await h.waitFor(() => pendingBubbleId(h) !== undefined);
    const clientId = pendingBubbleId(h);

    emitQueued(h, "send", clientId!);
    await h.flush();

    expect(pendingBubbleId(h)).toBe(clientId);
    h.unmount();
  });
});
