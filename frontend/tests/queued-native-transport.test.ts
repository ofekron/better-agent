import { afterEach, describe, expect, it } from "vitest";
import { fireEvent } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import type { QueuedPrompt } from "../src/types";

/**
 * The queued-banner UI (InputArea.tsx, driven by App.tsx's legacy
 * `queuedBySession`/`persistedQueuedPrompts` projection) stays legacy-owned
 * regardless of `ba.surface_native` — there is no native projection of a
 * queued prompt (see surface/state.ts's `editQueued`/`deleteQueued`
 * docstring). Only the WIRE the edit/delete command goes out on should
 * switch to the native `EditQueued`/`DeleteQueued` intent (over
 * `/ws/v2/surface`) when eligible (native store exists AND its socket is
 * open — same gate `sendPrompt` uses), falling back to the legacy
 * `cancel_queued`/`update_queued` `/ws/chat` frame otherwise. These drive
 * the REAL App through `tests/harness` (real WS handlers, real Chat.tsx/
 * InputArea, real SurfaceStore) — no reducer/dispatch logic is
 * re-implemented here, matching queued-banner-masked.test.ts's approach.
 */

const SID = "sess-native-queue";

function queued(id: string, content: string): QueuedPrompt {
  return {
    id,
    lifecycle_msg_id: `life-${id}`,
    content,
    kind: "queued_behind",
    queue_position: 0,
    images_count: 0,
    files_count: 0,
  };
}

interface OutboundFrame {
  type?: string;
  queued_id?: string;
  content?: string;
  intent?: { kind?: string; node_id?: string; text?: string; session_id?: string | null };
}

afterEach(() => {
  localStorage.removeItem("ba.surface_native");
});

describe("queued-banner edit/delete — native transport when eligible", () => {
  it("delete routes over the native delete_queued intent, not the legacy cancel_queued frame, when native + socket open", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: SID, queued_prompts: [queued("q-1", "prompt one")] });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(SID);
    await h.waitFor(() => h.$('[data-testid="queued-prompt-banner"]') !== null);

    await h.click('[data-testid="queued-prompt-banner"] .queued-cancel-btn');
    await h.flush();
    // Cancel is gated behind a confirm modal (App.tsx handleCancelQueued).
    await h.click(".modal-footer button:last-child");
    await h.flush();

    const outbound = h.outbound as OutboundFrame[];
    const nativeDelete = outbound.find((f) => f.intent?.kind === "delete_queued");
    expect(nativeDelete).toMatchObject({
      intent: { kind: "delete_queued", node_id: "q-1", session_id: SID },
    });
    expect(outbound.find((f) => f.type === "cancel_queued")).toBeUndefined();

    // Local optimistic removal still happens identically, regardless of
    // which transport carried the command.
    expect(h.$('[data-testid="queued-prompt-banner"]')).toBeNull();
    h.unmount();
  });

  it("edit routes over the native edit_queued intent, not the legacy update_queued frame, when native + socket open", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: SID, queued_prompts: [queued("q-2", "original text")] });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(SID);
    await h.waitFor(() => h.$('[data-testid="queued-prompt-banner"]') !== null);

    await h.click('[data-testid="queued-prompt-banner"] .queued-prompt-preview');
    await h.flush();
    const textarea = h.$(".queued-prompt-edit-input") as HTMLTextAreaElement | null;
    expect(textarea).not.toBeNull();
    fireEvent.change(textarea!, { target: { value: "edited text" } });
    await h.click(".queued-edit-modal .promote-btn");

    const outbound = h.outbound as OutboundFrame[];
    const nativeEdit = outbound.find((f) => f.intent?.kind === "edit_queued");
    expect(nativeEdit).toMatchObject({
      intent: { kind: "edit_queued", node_id: "q-2", text: "edited text", session_id: SID },
    });
    expect(outbound.find((f) => f.type === "update_queued")).toBeUndefined();

    expect(h.$('[data-testid="queued-prompt-banner"]')?.textContent).toContain("edited text");
    h.unmount();
  });

  it("falls back to the legacy cancel_queued transport when the session is not native-eligible", async () => {
    // ba.surface_native flag left OFF — Chat.tsx's `nativeQueuedTransportRef`
    // is never populated (nativeSessionId stays null), so App.tsx's
    // `performCancelQueued` must fall back to the legacy transport.
    const session = makeSession({ id: SID, queued_prompts: [queued("q-3", "legacy path")] });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(SID);
    await h.waitFor(() => h.$('[data-testid="queued-prompt-banner"]') !== null);

    await h.click('[data-testid="queued-prompt-banner"] .queued-cancel-btn');
    await h.flush();
    await h.click(".modal-footer button:last-child");
    await h.flush();

    const outbound = h.outbound as OutboundFrame[];
    expect(outbound.find((f) => f.type === "cancel_queued")).toMatchObject({
      type: "cancel_queued",
      queued_id: "q-3",
    });
    expect(outbound.find((f) => f.intent?.kind === "delete_queued")).toBeUndefined();
    expect(h.$('[data-testid="queued-prompt-banner"]')).toBeNull();
    h.unmount();
  });
});
