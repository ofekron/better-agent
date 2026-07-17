import { describe, expect, it } from "vitest";
import { fetchChatTree } from "src/chat/chatTreeClient";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";
import { mockPageCursor } from "./harness/mockBackend";
import type { ChatMessage } from "src/types";

function turnPair(n: number): ChatMessage[] {
  return [
    makeUserMsg({ id: `u${n}`, seq: n * 2, content: `prompt ${n}` }),
    makeAssistantMsg({ id: `a${n}`, seq: n * 2 + 1, content: `answer ${n}` }),
  ];
}

function seededSession(pairs: number) {
  return makeSession({
    id: "paged-1",
    messages: Array.from({ length: pairs }, (_, i) => turnPair(i + 1)).flat(),
  });
}

describe("bound load-more page cursor", () => {
  it("sends the opaque cursor as the `cursor` query param and reads page_cursor", async () => {
    const captured: string[] = [];
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      captured.push(String(input));
      return new Response(
        JSON.stringify({
          session: { id: "s1" },
          items: [],
          lookup: {},
          page: { turns: 5, pane: "s1", page_cursor: "opaque-abc", has_older: true },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }) as typeof fetch;
    try {
      const tree = await fetchChatTree("s1", { turns: 5, cursor: "signed:cursor/1+2" });
      expect(tree.page.page_cursor).toBe("opaque-abc");
      expect(tree.page.has_older).toBe(true);
      const url = new URL(captured[0], "http://localhost");
      expect(url.searchParams.get("cursor")).toBe("signed:cursor/1+2");
      expect(url.searchParams.get("before_turn")).toBeNull();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("load-more echoes the served page_cursor and renders the older page", async () => {
    const h = await renderApp({ seed: { sessions: [seededSession(8)] } });
    await h.selectSession("paged-1");
    // Initial window: last 5 turns only.
    expect(h.$$('[data-message-id="u8"]').length).toBeGreaterThan(0);
    expect(h.$('[data-message-id="u1"]')).toBeNull();

    await h.click(".load-older-link");

    const loadMore = h.restCalls.filter(
      (c) => c.method === "GET" && c.path.startsWith("/api/chat-tree/paged-1") && c.query.cursor !== undefined,
    );
    expect(loadMore.length).toBe(1);
    expect(loadMore[0].query.cursor).toBe(mockPageCursor("paged-1", "u4"));
    expect(loadMore[0].query.before_turn).toBeUndefined();
    // Older turns prepended.
    expect(h.$$('[data-message-id="u1"]').length).toBeGreaterThan(0);
    h.unmount();
  });

  it("discards a stale cursor on typed 409 and refetches exactly one fresh snapshot", async () => {
    const h = await renderApp({ seed: { sessions: [seededSession(8)] } });
    await h.selectSession("paged-1");

    // Simulate a projection rebuild: the turn bound into the served
    // cursor no longer exists, so the mock (like the backend) answers
    // the echoed cursor with the typed 409.
    const root = h.backend.state.sessions.find((s) => s.id === "paged-1")!;
    root.messages = root.messages!.filter(
      (m) => !["u4", "a4"].includes(m.id),
    );

    const callsBefore = h.restCalls.length;
    await h.click(".load-older-link");
    await h.flush();

    const after = h.restCalls.slice(callsBefore).filter(
      (c) => c.method === "GET" && c.path.startsWith("/api/chat-tree/paged-1"),
    );
    // One stale load-more (with cursor) + exactly one fresh snapshot
    // refetch (no cursor) — never a merge of mixed revisions.
    expect(after.length).toBe(2);
    expect(after[0].query.cursor).toBeDefined();
    expect(after[1].query.cursor).toBeUndefined();
    // The chat still renders the current window.
    expect(h.$$('[data-message-id="u8"]').length).toBeGreaterThan(0);
    h.unmount();
  });

  it("stops paging when page_cursor is null", async () => {
    const h = await renderApp({ seed: { sessions: [seededSession(3)] } });
    await h.selectSession("paged-1");
    // 3 turns fit one window: no older page, no load-older affordance.
    expect(h.$(".load-older-link")).toBeNull();
    const cursorCalls = h.restCalls.filter((c) => c.query.cursor !== undefined);
    expect(cursorCalls.length).toBe(0);
    h.unmount();
  });
});
