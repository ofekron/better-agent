import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import { promptNode, turnNode, compactTurn, resultNode } from "./surface/fixtures";
import { SingleFlight } from "../src/lib/singleFlight";

/**
 * Native surface pagination. The legacy `applyOlderMessagePage`/
 * `parseOlderMessagePage` merge-function unit tests and the
 * `useSession.loadOlderMessages` hook-coalescing tests that used to live
 * in this file are DELETED, not ported: the native path has no equivalent
 * pure merge function to test the same way — `SurfaceStore.loadOlder()`
 * (src/surface/state.ts) prepends the fetched page's turns directly onto
 * `turnOrder`/`turnsById`, with no separate "apply a page onto prior
 * state" step whose boundary-matching/dedup logic could be unit-tested in
 * isolation the way the legacy `before_seq`-keyed merge was. The
 * `SingleFlight` tests are left in place untouched — that class is a
 * generic utility (src/lib/singleFlight.ts, also used by
 * src/lib/backendAccess.ts) with no chat-content coupling at all; it just
 * happened to share this file with the legacy pagination tests.
 *
 * The harness's `/api/v2/surface/sessions/:id/older` mock route needed a
 * seeded-page extension (`MockBackend.seedSurface`'s new `olderPage`
 * field) to migrate the surviving harness-integration case below — see
 * tests/harness/mockBackend.ts.
 */
describe("native surface pagination", () => {
  it("removes the load-older control after an authoritative empty page", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "s-page", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("s-page", {
          turns: [
            compactTurn(turnNode("t10"), promptNode("t10", "u-10"), [resultNode("t10", "a-11")]),
          ],
          olderCursor: "cursor-8",
          olderPage: { turns: [], runs: [], olderCursor: null },
        });
      },
    });

    await h.selectSession("s-page");
    await h.waitFor(() => h.$('[data-testid="surface-chat-view"]') !== null);
    expect(h.$(".load-older-link")).not.toBeNull();
    const turnCount = h.$$('[data-testid="surface-turn"]').length;

    await h.click(".load-older-link");
    await h.waitFor(() => h.$(".load-older-link") === null);

    expect(h.$$('[data-testid="surface-turn"]')).toHaveLength(turnCount);
    h.unmount();
  });

  it("prepends an older page's turns without dropping the ones already loaded", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "s-page-2", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("s-page-2", {
          turns: [
            compactTurn(turnNode("t-new"), promptNode("t-new", "newer prompt"), [resultNode("t-new", "newer answer")]),
          ],
          olderCursor: "cursor-old",
          olderPage: {
            turns: [
              compactTurn(turnNode("t-old"), promptNode("t-old", "older prompt"), [resultNode("t-old", "older answer")]),
            ],
            runs: [],
            olderCursor: null,
          },
        });
      },
    });

    await h.selectSession("s-page-2");
    await h.waitFor(() => h.$('[data-testid="surface-chat-view"]') !== null);
    await h.click(".load-older-link");
    await h.waitFor(() => h.$$('[data-testid="surface-turn"]').length === 2);

    const turns = h.$$('[data-testid="surface-turn"]').map((el) => el.getAttribute("data-turn-id"));
    // Older page prepends BEFORE the already-loaded turn.
    expect(turns).toEqual(["t-old", "t-new"]);
    expect(h.$(".load-older-link")).toBeNull();
    h.unmount();
  });

  it("renders a retryable error when loading older messages fails, clears it on successful retry", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "s-page-err", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("s-page-err", {
          turns: [
            compactTurn(turnNode("t20"), promptNode("t20", "u-20"), [resultNode("t20", "a-21")]),
          ],
          olderCursor: "cursor-9",
          olderPage: { turns: [], runs: [], olderCursor: null },
        });
      },
    });

    await h.selectSession("s-page-err");
    await h.waitFor(() => h.$('[data-testid="surface-chat-view"]') !== null);
    expect(h.$(".load-older-link")).not.toBeNull();

    h.backend.failNextWithStatus(500, "/api/v2/surface/sessions/s-page-err/older");
    await h.click(".load-older-link");
    await h.waitFor(() => h.$(".load-older-error") !== null);
    expect(h.$(".load-older-error")?.getAttribute("role")).toBe("alert");
    // The failed page's cursor is preserved (not dropped) — retry reuses it.
    expect(h.$(".load-older-error .load-older-link")).not.toBeNull();

    await h.click(".load-older-error .load-older-link");
    // Authoritative empty page on the successful retry removes the whole
    // control — proves the error state cleared AND the retry re-used the
    // same cursor to reach the seeded olderPage.
    await h.waitFor(() => h.$(".load-older-wrapper") === null);
    h.unmount();
  });

  it("coalesces identical work but keeps different cursors independent", async () => {
    const singleFlight = new SingleFlight<string>();
    let resolveFirst!: () => void;
    let resolveSecond!: () => void;
    const firstWork = () => new Promise<void>((resolve) => { resolveFirst = resolve; });
    const secondWork = () => new Promise<void>((resolve) => { resolveSecond = resolve; });

    const first = singleFlight.run("s:10", firstWork);
    const duplicate = singleFlight.run("s:10", firstWork);
    const second = singleFlight.run("s:8", secondWork);
    await Promise.resolve();

    resolveSecond();
    resolveFirst();
    await Promise.all([first, duplicate, second]);
  });

  it("clears rejected work so the same cursor can retry", async () => {
    const singleFlight = new SingleFlight<string>();
    const failed = () => Promise.reject(new Error("offline"));
    await expect(singleFlight.run("s:10", failed)).rejects.toThrow("offline");

    const retry = () => Promise.resolve(undefined);
    await expect(singleFlight.run("s:10", retry)).resolves.toBeUndefined();
  });
});
