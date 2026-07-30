import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import type { Session, WSEvent } from "../src/types";
import fixture from "./__fixtures__/chat_contract_wire.json";

async function waitFor(
  h: Awaited<ReturnType<typeof renderApp>>,
  predicate: () => boolean,
) {
  for (let i = 0; i < 20; i++) {
    if (predicate()) return true;
    await h.flush();
  }
  return false;
}

// Real REST + WS payloads captured from the ACTUAL backend by
// backend/scripts/test_chat_contract_wire_capture.py — no hand-built
// fixtures. Proves the wire contract this harness's OTHER tests assume
// (via makeSession/makeAssistantMsg/textEvent in ./fixtures) actually
// matches what the real `GET /api/sessions/{id}` and `/ws/chat` produce,
// closing the gap between the backend's render-tree tests (which never
// call a real route) and the frontend harness tests (which never see
// real backend serialization). Re-run that script (and re-commit this
// fixture) whenever the REST session shape or WS envelope format changes.

const restInitial = fixture.restInitial as unknown as Session;
const wsMessagesReplay = fixture.wsMessagesReplay as unknown as WSEvent;
const wsLivePush = fixture.wsLivePush as unknown as WSEvent;
const { sessionId, assistantMsgId } = fixture;

const managerScenario = fixture.managerWorkerScenario;
const managerRestSnapshot = managerScenario.restSnapshot as unknown as Session;
const managerWsReplay = managerScenario.wsMessagesReplay as unknown as WSEvent;

const paginationScenario = fixture.paginationScenario;
const firstPage = paginationScenario.firstPage as unknown as Session;
const olderPage = paginationScenario.olderPage;

const reconnectScenario = fixture.reconnectScenario;
const reconnectBaselineReplay = reconnectScenario.baselineReplay as unknown as WSEvent;
const reconnectDeltaReplay = reconnectScenario.deltaReplay as unknown as WSEvent;

/** Layer a real captured JSON response on top of the harness's own
 * mocked fetch for one specific URL, falling through to the harness for
 * everything else. Needed where mockBackend's built-in route derives its
 * response from whatever's already seeded (e.g. the lazy events fetch,
 * the `before_seq` pagination fetch) rather than a distinct real payload
 * captured separately — same layering technique as
 * `message-pagination.test.tsx`'s `installPaginationFetchGate`. No
 * manual restore needed: `MockBackend.uninstall()` (run by `h.unmount()`)
 * resets `globalThis.fetch` to the harness's pre-test original
 * regardless of what ran in between.
 */
function overrideFetchOnce(matchUrl: (url: string) => boolean, body: unknown): void {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const raw =
      typeof input === "string" ? input
      : input instanceof URL ? input.toString()
      : input.url;
    if (matchUrl(raw)) {
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return previousFetch(input, init);
  }) as typeof fetch;
}

describe("chat panel wire contract (real captured REST + WS payloads)", () => {
  it("REST GET /api/sessions/{id}'s real (stubbed-events) shape renders correctly", async () => {
    const h = await renderApp({ seed: { sessions: [restInitial] } });
    await h.selectSession(sessionId);

    const view = h.toJSON();
    expect(view.chat.messages.find((m) => m.id === "u1")?.text).toContain(
      "hello from contract test",
    );
    expect(h.raw.container.textContent).toContain("seeded assistant reply");
    h.unmount();
  });

  it("real messages_replay WS frame renders identically to the REST snapshot", async () => {
    const cold: Session = { ...restInitial, messages: [] };
    const h = await renderApp({ seed: { sessions: [cold] } });
    await h.selectSession(sessionId);

    h.emit(wsMessagesReplay);
    await h.flush();

    const view = h.toJSON();
    expect(view.chat.messages.find((m) => m.id === "u1")?.text).toContain(
      "hello from contract test",
    );
    expect(h.raw.container.textContent).toContain("seeded assistant reply");
    h.unmount();
  });

  it("a real live-pushed WS event updates the message the same way the REST snapshot reflects it", async () => {
    const h = await renderApp({ seed: { sessions: [restInitial] } });
    await h.selectSession(sessionId);
    expect(h.raw.container.textContent).toContain("seeded assistant reply");

    h.emit(wsLivePush);
    await waitFor(h, () =>
      h.raw.container.textContent?.includes("live pushed assistant reply") ?? false,
    );

    expect(h.raw.container.textContent).toContain("live pushed assistant reply");
    const view = h.toJSON();
    expect(view.chat.messages.find((m) => m.id === assistantMsgId)?.text).toContain(
      "live pushed assistant reply",
    );
    h.unmount();
  });

  it("manager-mode worker delegation: panel metadata renders immediately, events hydrate via the real lazy fetch", async () => {
    const h = await renderApp({ seed: { sessions: [managerRestSnapshot] } });
    await h.selectSession(managerScenario.sessionId);

    // GET /api/sessions/{id} never inlines worker-panel events (confirmed
    // against the real backend — no stub/preview either, unlike top-level
    // messages), so the panel metadata is visible immediately but its
    // reply text is not, until the lazy full-events fetch resolves.
    expect(h.raw.container.textContent).toContain("delegate this to a worker");
    expect(h.raw.container.textContent).toContain("contract test worker");

    overrideFetchOnce(
      (url) => url.includes(`/messages/${managerScenario.ownerMsgId}/events`),
      managerScenario.restOwnerFullFetch,
    );
    await waitFor(h, () =>
      h.raw.container.textContent?.includes("worker reply text") ?? false,
    );
    expect(h.raw.container.textContent).toContain("worker reply text");
    h.unmount();
  });

  it("real messages_replay carries the worker delegation panel the same way REST does", async () => {
    const cold: Session = { ...managerRestSnapshot, messages: [] };
    const h = await renderApp({ seed: { sessions: [cold] } });
    await h.selectSession(managerScenario.sessionId);

    h.emit(managerWsReplay);
    await h.flush();

    expect(h.raw.container.textContent).toContain("delegate this to a worker");
    expect(h.raw.container.textContent).toContain("contract test worker");
    h.unmount();
  });

  it("real GET .../messages?before_seq page loads real older messages", async () => {
    const h = await renderApp({ seed: { sessions: [firstPage] } });
    await h.selectSession(paginationScenario.sessionId);

    expect(h.$(".load-older-link")).not.toBeNull();
    for (const older of olderPage.messages) {
      expect(h.raw.container.textContent).not.toContain(older.content as string);
    }

    overrideFetchOnce(
      (url) =>
        url.includes(`/${paginationScenario.sessionId}/messages?`) &&
        url.includes("before_seq"),
      olderPage,
    );
    await h.click(".load-older-link");
    await waitFor(h, () =>
      h.raw.container.textContent?.includes("turn 0 answer") ?? false,
    );

    expect(h.raw.container.textContent).toContain("turn 0 question");
    expect(h.raw.container.textContent).toContain("turn 0 answer");
    h.unmount();
  });

  it("real WS reconnect delta replay merges new content onto existing state instead of replacing it", async () => {
    // `useSession.ts`'s getSinceSeq() sends the highest SEEN seq (not
    // next_seq) on reconnect — the backend's delta query then returns
    // ONLY the content added since, per session_detail_api's
    // since_seq+1 semantics when there's no in-flight message. See the
    // capture script's `_capture_reconnect_scenario` for the full
    // contract this fixture proves.
    const cold: Session = { ...restInitial, id: reconnectScenario.sessionId, messages: [] };
    const h = await renderApp({ seed: { sessions: [cold] } });
    await h.selectSession(reconnectScenario.sessionId);

    h.emit(reconnectBaselineReplay);
    await h.flush();
    expect(h.raw.container.textContent).toContain("reconnect turn1 answer");

    h.emit(reconnectDeltaReplay);
    await waitFor(h, () =>
      h.raw.container.textContent?.includes("reconnect turn2 answer") ?? false,
    );

    // Both turns present — the delta must MERGE onto existing state,
    // not replace it.
    expect(h.raw.container.textContent).toContain("reconnect turn1 answer");
    expect(h.raw.container.textContent).toContain("reconnect turn2 question");
    expect(h.raw.container.textContent).toContain("reconnect turn2 answer");
    h.unmount();
  });
});
