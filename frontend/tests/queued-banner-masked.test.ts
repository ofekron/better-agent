import { describe, expect, it } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import type { QueuedPrompt } from "../src/types";

/**
 * The queued-prompt banner is masked when App's local `queuedBySession`
 * projection holds an entry for the session that is emptier than the
 * backend-owned `session.queued_prompts`. Any entry (including `null`
 * and `[]`) shadows the persisted snapshot:
 *
 *   currentSession.id in queuedBySession
 *     ? queuedBySession[currentSession.id] ?? []
 *     : persistedQueuedPrompts
 *
 * Seam note: that resolution lives inline in App.tsx and is not
 * exported, so these tests drive the REAL App through `tests/harness`
 * (real WS handlers, real reducer, real InputArea) and assert on the
 * rendered banner. No reducer logic is re-implemented here.
 */

const SID = "sess-1";

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

type Harness = Awaited<ReturnType<typeof renderApp>>;

async function renderWithSession(queuedPrompts?: QueuedPrompt[]): Promise<Harness> {
  const session = makeSession({
    id: SID,
    orchestration_mode: "native",
    ...(queuedPrompts ? { queued_prompts: queuedPrompts } : {}),
  });
  const h = await renderApp({ seed: { sessions: [session] } });
  await h.selectSession(SID);
  await h.flush();
  return h;
}

function bannerText(h: Harness): string {
  const el = h.$('[data-testid="queued-prompt-banner"]');
  return (el?.textContent ?? "").replace(/\s+/g, " ").trim();
}

describe("queued banner masking by the local queue projection", () => {
  it("survives a stale subscribe-time queue_consumed(queued_id=null)", async () => {
    const h = await renderWithSession();

    // Backend queues prompt B mid-turn: session_manager's
    // queued_prompts_updated projection plus the orchestrator's
    // prompt_queued ack.
    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: SID,
        patch: { queued_prompts: [queued("q-B", "prompt B")] },
      },
    } as never);
    h.emit({
      type: "prompt_queued",
      data: {
        app_session_id: SID,
        queued_id: "q-B",
        prompt_preview: "prompt B",
        send_mode: "queue",
        queue_position: 1,
      },
    } as never);
    await h.flush();
    expect(bannerText(h)).toContain("prompt B");

    // main.py's WS `subscribe` handler registers the subscriber first
    // and only later (after messages_replay / run_state / approvals
    // re-emits and a thread hop) reads the queue to decide whether to
    // send the stale-queue cleanup frame. A prompt queued inside that
    // window is acked to the already-registered subscriber BEFORE the
    // handler's now-stale queue_consumed(queued_id=null) arrives.
    h.emit({
      type: "queue_consumed",
      data: { app_session_id: SID, queued_id: null },
    } as never);
    await h.flush();

    // Backend truth (session.queued_prompts) still holds q-B, so the
    // banner must still be shown.
    expect(bannerText(h)).toContain("prompt B");
    h.unmount();
  });

  it("re-shows the banner after a cancel-all null when the backend re-reports the queue", async () => {
    const h = await renderWithSession([queued("q-A", "prompt A")]);
    expect(bannerText(h)).toContain("prompt A");

    // Cancelling a queued prompt is gated behind a confirmation modal
    // (App.tsx handleCancelQueued); confirming it is what actually sets
    // queuedBySession[sid] = null.
    await h.click('[data-testid="queued-prompt-banner"] .queued-cancel-btn');
    await h.flush();
    await h.click(".modal-footer button:last-child");
    await h.flush();
    expect(bannerText(h)).not.toContain("prompt A");

    // The backend rejected/ignored the cancel and re-broadcasts the
    // queue: the local null must not outlive that snapshot.
    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: SID,
        patch: { queued_prompts: [queued("q-A", "prompt A")] },
      },
    } as never);
    await h.flush();

    expect(bannerText(h)).toContain("prompt A");
    h.unmount();
  });
});
