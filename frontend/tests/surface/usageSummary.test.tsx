// Native coverage for the token-usage summary gap (phase2-inventory.md,
// mb-family: "TurnEntry.usage is captured from turn_lifecycle frames but
// never read by any component"). Legacy equivalent:
// tests/messagebubble-completeevent.test.tsx (deleted, superseded by
// tests/surface/failureChip.test.tsx for the error-block half — this file
// covers the success/token-usage half).
//
// Cache-token coverage: `UsageWire` (adapter/wire.ts) now carries
// `cache_creation_input_tokens`/`cache_read_input_tokens`
// (backend/surface_contract/nodes.py's `Usage`) — the "Cache: Z" segment
// mirrors legacy's `CompleteEvent` exactly (only `cache_read_input_tokens`
// feeds it, shown only when > 0; `cache_creation_input_tokens` is never
// displayed, same as legacy).

import { describe, it, expect } from "vitest";
import { renderApp } from "../harness";
import { makeSession } from "../fixtures";
import { turnNode, compactTurn, promptNode, turnLifecycleFrame } from "./fixtures";

describe("native surface — per-turn token-usage summary", () => {
  it("a completed turn's usage from turn_lifecycle renders In/Out token counts", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-usage", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-usage", {
          turns: [compactTurn(turnNode("t1"), promptNode("t1", "hi"), [])],
        });
      },
    });
    await h.selectSession("sess-usage");
    await h.waitFor(() => h.$('[data-testid="surface-turn"]') !== null);

    h.emitSurface(
      "sess-usage",
      turnLifecycleFrame("t1", "completed", {
        usage: {
          input_tokens: 1234,
          output_tokens: 567,
          total_tokens: 1801,
          cache_creation_input_tokens: null,
          cache_read_input_tokens: null,
        },
      }),
    );

    await h.waitFor(() => h.$('[data-testid="surface-usage-summary"]') !== null);
    const summary = h.$('[data-testid="surface-usage-summary"]')!;
    expect(summary.textContent).toContain("In: 1.2k");
    expect(summary.textContent).toContain("Out: 567");
    h.unmount();
  });

  it("zero input+output tokens renders no summary", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-usage-zero", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-usage-zero", {
          turns: [compactTurn(turnNode("t1"), promptNode("t1", "hi"), [])],
        });
      },
    });
    await h.selectSession("sess-usage-zero");
    await h.waitFor(() => h.$('[data-testid="surface-turn"]') !== null);

    h.emitSurface(
      "sess-usage-zero",
      turnLifecycleFrame("t1", "completed", {
        usage: {
          input_tokens: 0,
          output_tokens: 0,
          total_tokens: 0,
          cache_creation_input_tokens: null,
          cache_read_input_tokens: null,
        },
      }),
    );
    await h.waitFor(() => h.$('[data-testid="surface-turn"]')?.getAttribute("data-phase") === "completed");

    expect(h.$('[data-testid="surface-usage-summary"]')).toBeNull();
    h.unmount();
  });

  it("cache_read_input_tokens > 0 renders a Cache segment, at parity with legacy", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-usage-cache", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-usage-cache", {
          turns: [compactTurn(turnNode("t1"), promptNode("t1", "hi"), [])],
        });
      },
    });
    await h.selectSession("sess-usage-cache");
    await h.waitFor(() => h.$('[data-testid="surface-turn"]') !== null);

    h.emitSurface(
      "sess-usage-cache",
      turnLifecycleFrame("t1", "completed", {
        usage: {
          input_tokens: 10,
          output_tokens: 20,
          total_tokens: 30,
          cache_creation_input_tokens: 50,
          cache_read_input_tokens: 999,
        },
      }),
    );

    await h.waitFor(() => h.$('[data-testid="surface-usage-summary"]') !== null);
    const summary = h.$('[data-testid="surface-usage-summary"]')!;
    expect(summary.textContent).toContain("Cache: 999");
    h.unmount();
  });

  it("cache_creation_input_tokens alone (no cache_read) renders no Cache segment", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-usage-cache-creation-only", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-usage-cache-creation-only", {
          turns: [compactTurn(turnNode("t1"), promptNode("t1", "hi"), [])],
        });
      },
    });
    await h.selectSession("sess-usage-cache-creation-only");
    await h.waitFor(() => h.$('[data-testid="surface-turn"]') !== null);

    h.emitSurface(
      "sess-usage-cache-creation-only",
      turnLifecycleFrame("t1", "completed", {
        usage: {
          input_tokens: 10,
          output_tokens: 20,
          total_tokens: 30,
          cache_creation_input_tokens: 500,
          cache_read_input_tokens: 0,
        },
      }),
    );

    await h.waitFor(() => h.$('[data-testid="surface-usage-summary"]') !== null);
    const summary = h.$('[data-testid="surface-usage-summary"]')!;
    expect(summary.textContent).not.toContain("Cache:");
    h.unmount();
  });

  it("no lifecycle usage at all renders no summary", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-usage-none", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-usage-none", {
          turns: [compactTurn(turnNode("t1"), promptNode("t1", "hi"), [])],
        });
      },
    });
    await h.selectSession("sess-usage-none");
    await h.waitFor(() => h.$('[data-testid="surface-turn"]') !== null);

    expect(h.$('[data-testid="surface-usage-summary"]')).toBeNull();
    h.unmount();
  });
});
