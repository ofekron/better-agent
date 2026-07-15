import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { act, fireEvent } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import type { RestCall } from "./harness/mockBackend";

const DEBOUNCE_MS = 300;

// Fake setTimeout so the App.tsx draft debounce can be advanced instantly.
// shouldAdvanceTime keeps the harness's real-time setTimeout(0) flushes alive.
beforeEach(() => {
  vi.useFakeTimers({
    shouldAdvanceTime: true,
    advanceTimeDelta: 1,
    toFake: ["setTimeout", "clearTimeout"],
  });
});

afterEach(() => {
  vi.useRealTimers();
});

/** Advance past the App.tsx debounce timer, then drain effects. */
async function waitDebounce(ms = DEBOUNCE_MS + 50): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** Type into the chat input WITHOUT sending. Fires a single onChange. */
function typeDraft(container: HTMLElement, value: string): void {
  const ta = container.querySelector(
    '[data-testid="input-textarea"]',
  ) as HTMLTextAreaElement | null;
  if (!ta) throw new Error("draft-sync: textarea not present");
  fireEvent.change(ta, { target: { value } });
}

function draftCalls(calls: RestCall[], sessionId: string): RestCall[] {
  return calls.filter(
    (c) => c.method === "PATCH" && c.path === `/api/sessions/${sessionId}/draft`,
  );
}

describe("draft input — backend-backed sync", () => {
  it("typing updates the textarea optimistically before the debounced PATCH fires", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    typeDraft(h.raw.container as HTMLElement, "hello");
    await h.flush();

    expect(h.toJSON().input.text).toBe("hello");
    // No PATCH yet — still inside the debounce window.
    expect(draftCalls(h.backend.calls, session.id)).toHaveLength(0);
    h.unmount();
  });

  it("after the debounce window, exactly one PATCH /draft fires with the latest value", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    typeDraft(h.raw.container as HTMLElement, "h");
    typeDraft(h.raw.container as HTMLElement, "he");
    typeDraft(h.raw.container as HTMLElement, "hel");
    typeDraft(h.raw.container as HTMLElement, "hell");
    typeDraft(h.raw.container as HTMLElement, "hello");
    await h.flush();
    await waitDebounce();

    const patches = draftCalls(h.backend.calls, session.id);
    expect(patches).toHaveLength(1);
    const body = patches[0].body as {
      draft_input: string;
      client_seq: number;
      client_id: string;
    };
    expect(body.draft_input).toBe("hello");
    expect(typeof body.client_seq).toBe("number");
    expect(typeof body.client_id).toBe("string");
    expect(body.client_id.length).toBeGreaterThan(0);
    h.unmount();
  });

  it("the debounced draft PATCH carries the session's attachments", async () => {
    // Regression: a text-only draft PATCH with a higher seq wins the
    // backend stale-write guard, so a slower image PATCH gets dropped
    // and the attachment is lost. The draft save must be a COMPLETE
    // snapshot — text + attachments.
    const images = [{ dataUrl: "d", base64: "imgA", mediaType: "image/png" }];
    const session = makeSession({ draft_images: images });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    typeDraft(h.raw.container as HTMLElement, "with attachment");
    await h.flush();
    await waitDebounce();

    const patches = draftCalls(h.backend.calls, session.id);
    expect(patches.length).toBeGreaterThanOrEqual(1);
    const body = patches[patches.length - 1].body as {
      draft_input: string;
      draft_images?: { base64: string }[];
    };
    expect(body.draft_input).toBe("with attachment");
    expect(body.draft_images?.map((i) => i.base64)).toEqual(["imgA"]);
    h.unmount();
  });

  it("seeded draft_input is shown in the textarea on session select", async () => {
    const session = makeSession({ draft_input: "previous draft" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    expect(h.toJSON().input.text).toBe("previous draft");
    h.unmount();
  });

  it("a session_metadata_updated WS event from another tab updates the textarea", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: session.id,
        patch: { draft_input: "from another tab" },
        originated_by: "tab-other",
      },
    });
    await h.flush();

    expect(h.toJSON().input.text).toBe("from another tab");
    h.unmount();
  });

  it("the originating tab ignores its own session_metadata_updated echo", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    typeDraft(h.raw.container as HTMLElement, "local typing");
    await h.flush();
    await waitDebounce();

    const body = draftCalls(h.backend.calls, session.id)[0].body as {
      client_id: string;
    };
    const ourClientId = body.client_id;

    // Type more locally so the broadcast value is "older" than what's
    // on screen. If the originator-skip is broken, the broadcast would
    // clobber the textarea back to "local typing".
    typeDraft(h.raw.container as HTMLElement, "newer keystrokes");
    await h.flush();

    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: session.id,
        patch: { draft_input: "local typing" },
        originated_by: ourClientId,
      },
    });
    await h.flush();

    // Local edit survives — originator filter dropped the echo.
    expect(h.toJSON().input.text).toBe("newer keystrokes");
    h.unmount();
  });

  it("clicking Send clears the draft and immediately PATCHes draft=''", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    await h.typeAndSend("ship it");
    await h.flush();

    // Textarea is empty.
    expect(h.toJSON().input.text).toBe("");
    // A PATCH /draft fired with empty body, immediate (not waiting on debounce).
    const patches = draftCalls(h.backend.calls, session.id);
    expect(patches.length).toBeGreaterThanOrEqual(1);
    const last = patches[patches.length - 1].body as { draft_input: string };
    expect(last.draft_input).toBe("");
    h.unmount();
  });

  it("send clears the on-disk draft via the empty PATCH (mock backend reflects it)", async () => {
    const session = makeSession({ draft_input: "stale" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    await h.typeAndSend("ship it");
    await h.flush();

    const stored = h.backend.state.sessions.find((s) => s.id === session.id);
    expect(stored?.draft_input).toBe("");
    h.unmount();
  });

  it("the backend rejects a PATCH whose client_seq is older than the stored seq", async () => {
    // We exercise the seq guard directly through fetch — independent
    // of App's debounce/clearing logic — so the contract is pinned
    // even if App stops sending out-of-order PATCHes naturally.
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });

    const fresh = await fetch(`/api/sessions/${session.id}/draft`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        draft_input: "fresh",
        client_seq: 200,
        client_id: "tab-x",
      }),
    }).then((r) => r.json());
    expect(fresh.draft_input).toBe("fresh");
    expect(fresh.draft_input_seq).toBe(200);

    const stale = await fetch(`/api/sessions/${session.id}/draft`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        draft_input: "stale-late-arrival",
        client_seq: 150,
        client_id: "tab-x",
      }),
    }).then((r) => r.json());
    expect(stale.rejected).toBe(true);

    // Persisted state still reflects the FRESH value, not the rejected one.
    const stored = h.backend.state.sessions.find((s) => s.id === session.id);
    expect(stored?.draft_input).toBe("fresh");
    h.unmount();
  });

  it("deleting a session cancels its pending draft PATCH timer", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    typeDraft(h.raw.container as HTMLElement, "soon to be gone");
    await h.flush();
    // Pending timer is scheduled; do not flush past the debounce.

    await h.deleteSession(session.id);
    await waitDebounce();

    expect(draftCalls(h.backend.calls, session.id)).toHaveLength(0);
    h.unmount();
  });

  it("session switch does NOT cancel the previous session's pending PATCH", async () => {
    const a = makeSession({ id: "a" });
    const b = makeSession({ id: "b", name: "B" });
    const h = await renderApp({ seed: { sessions: [a, b] } });

    await h.selectSession("a");
    typeDraft(h.raw.container as HTMLElement, "draft for A");
    await h.flush();

    await h.selectSession("b");
    await waitDebounce();

    const patchesA = draftCalls(h.backend.calls, "a");
    expect(patchesA).toHaveLength(1);
    const body = patchesA[0].body as { draft_input: string };
    expect(body.draft_input).toBe("draft for A");
    h.unmount();
  });

  it("each tab's clientId is stable across renders (one PATCH body per tab)", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    typeDraft(h.raw.container as HTMLElement, "first");
    await h.flush();
    await waitDebounce();

    typeDraft(h.raw.container as HTMLElement, "second");
    await h.flush();
    await waitDebounce();

    const patches = draftCalls(h.backend.calls, session.id);
    expect(patches.length).toBeGreaterThanOrEqual(2);
    const ids = new Set(
      patches.map((p) => (p.body as { client_id: string }).client_id),
    );
    expect(ids.size).toBe(1);
    // Lazy useState init means the entropy call ran once — value
    // shouldn't be empty.
    const onlyId = [...ids][0];
    expect(typeof onlyId).toBe("string");
    expect(onlyId.length).toBeGreaterThan(0);
    h.unmount();
  });
});
