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
});
