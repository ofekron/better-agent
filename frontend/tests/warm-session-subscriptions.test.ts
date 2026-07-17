import { describe, expect, it } from "vitest";
import { desiredSubscriptions } from "src/hooks/useWebSocket";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";

function seedTwo() {
  return [
    makeSession({
      id: "warm-a",
      messages: [
        makeUserMsg({ id: "ua", seq: 0, content: "prompt a" }),
        makeAssistantMsg({ id: "aa", seq: 1, content: "answer a" }),
      ],
    }),
    makeSession({
      id: "warm-b",
      messages: [
        makeUserMsg({ id: "ub", seq: 0, content: "prompt b" }),
        makeAssistantMsg({ id: "ab", seq: 1, content: "answer b" }),
      ],
    }),
  ];
}

describe("desiredSubscriptions priority map", () => {
  it("opened wins over warm for the same id; warm-only ids stay warm", () => {
    const desired = desiredSubscriptions("s1", ["s2"], ["s1", "s2", "s3"]);
    expect(desired.get("s1")).toBe("opened");
    expect(desired.get("s2")).toBe("opened");
    expect(desired.get("s3")).toBe("warm");
  });

  it("defaults to opened-only when no warm ids exist", () => {
    const desired = desiredSubscriptions("s1", undefined, undefined);
    expect(Array.from(desired)).toEqual([["s1", "opened"]]);
  });
});

describe("opened-vs-warm WS subscriptions", () => {
  it("subscribes the focused session as opened and demotes the cached one to warm on switch", async () => {
    const h = await renderApp({ seed: { sessions: seedTwo() } });
    await h.selectSession("warm-a");

    const subA = h.outbound.filter(
      (f) => f.type === "subscribe" && f.app_session_id === "warm-a",
    );
    expect(subA.length).toBeGreaterThan(0);
    expect(subA[subA.length - 1]).toMatchObject({ priority: "opened" });

    await h.selectSession("warm-b");

    const subB = h.outbound.filter(
      (f) => f.type === "subscribe" && f.app_session_id === "warm-b",
    );
    expect(subB[subB.length - 1]).toMatchObject({ priority: "opened" });
    // The previously focused session stays subscribed — demoted to warm
    // via a priority-upsert subscribe, never unsubscribed while cached.
    const lastA = h.outbound.filter(
      (f) => f.app_session_id === "warm-a" && (f.type === "subscribe" || f.type === "unsubscribe"),
    );
    expect(lastA[lastA.length - 1]).toMatchObject({
      type: "subscribe",
      priority: "warm",
    });
    h.unmount();
  });

  it("applies chat_tree_delta frames to a warm cached session so reopen is fresh without a refetch", async () => {
    const h = await renderApp({ seed: { sessions: seedTwo() } });
    await h.selectSession("warm-a");
    await h.selectSession("warm-b");

    // Warm delta for warm-a: a brand-new completed turn.
    h.emit({
      type: "chat_tree_delta",
      data: {
        app_session_id: "warm-a",
        phase: "settled",
        items: [
          {
            type: "Turn",
            id: "ua2",
            prompt: "ua2",
            body: [],
            result: { type: "ProviderResult", part_ids: ["aa2:final"], text: "fresh warm answer" },
          },
        ],
        lookup: {
          ua2: {
            kind: "message", role: "user", text: "warm follow-up",
            seq: 2, snapshot: { id: "ua2", role: "user", content: "warm follow-up" },
          },
          "aa2:final": {
            kind: "event", type: "assistant_text",
            data: { text: "fresh warm answer" }, message_id: "aa2",
          },
          aa2: {
            kind: "message", role: "assistant", text: "fresh warm answer",
            seq: 3, snapshot: { id: "aa2", role: "assistant", content: "fresh warm answer" },
          },
        },
      },
    });
    await h.flush();

    const fetchesBefore = h.restCalls.filter(
      (c) => c.method === "GET" && c.path.startsWith("/api/chat-tree/warm-a"),
    ).length;
    await h.selectSession("warm-a");
    const fetchesAfter = h.restCalls.filter(
      (c) => c.method === "GET" && c.path.startsWith("/api/chat-tree/warm-a"),
    ).length;

    // Cached reopen: no new snapshot fetch, and the warm delta's turn
    // is already in the rendered tree.
    expect(fetchesAfter).toBe(fetchesBefore);
    expect(h.$$('[data-message-id="ua2"]').length).toBeGreaterThan(0);
    expect(h.raw.container.textContent).toContain("fresh warm answer");
    h.unmount();
  });

  // 21 sequential session opens legitimately exceed the default 5s
  // budget when the suite runs fully parallel.
  it("unsubscribes a session evicted from the LRU cache", { timeout: 30_000 }, async () => {
    // 21 sessions overflow the 20-entry tree cache: opening them all in
    // order evicts the first one.
    const sessions = Array.from({ length: 21 }, (_, i) =>
      makeSession({ id: `lru-${i + 1}`, messages: [] }),
    );
    const h = await renderApp({ seed: { sessions } });
    for (let i = 1; i <= 21; i += 1) {
      await h.selectSession(`lru-${i}`);
    }
    const lru1Frames = h.outbound.filter(
      (f) => f.app_session_id === "lru-1" && (f.type === "subscribe" || f.type === "unsubscribe"),
    );
    expect(lru1Frames[lru1Frames.length - 1]).toMatchObject({ type: "unsubscribe" });
    // The still-cached second session remains warm-subscribed.
    const lru2Frames = h.outbound.filter(
      (f) => f.app_session_id === "lru-2" && (f.type === "subscribe" || f.type === "unsubscribe"),
    );
    expect(lru2Frames[lru2Frames.length - 1]).toMatchObject({
      type: "subscribe",
      priority: "warm",
    });
    h.unmount();
  });
});
