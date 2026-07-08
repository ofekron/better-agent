import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// Regression: the Routines/Workers tabs are hidden whenever the one-shot
// bootstrap `loadBuiltinExtensionIds()` fails. That fetch (/api/extensions/
// builtin-ids) is auth-gated and fires in main.tsx BEFORE login, so it 401s
// on every fresh login (and "Failed to fetch" during a backend restart).
// With the id map empty, extId("routines") === "" → no /api/extensions record
// matches → builtinExtensions.routines === false → tab never renders.
//
// The fix: useBuiltinExtensionFlags re-loads the builtin-ids map once
// authenticated, before reading extId(). The discriminator below is the
// number of builtin-ids fetches: pre-fix the hook never re-loads (1 call,
// tab stays hidden); post-fix it re-loads after auth (>=2 calls, flag true).

afterEach(() => {
  vi.restoreAllMocks();
  vi.resetModules();
});

type FetchState = { idsOk: { value: boolean }; idsCalls: { n: number } };

function mockFetch(state: FetchState) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url.endsWith("/api/extensions/builtin-ids")) {
      state.idsCalls.n += 1;
      if (!state.idsOk.value) {
        return { ok: false, status: 401, json: async () => ({}) } as Response;
      }
      return {
        ok: true,
        json: async () => ({ ids: { routines: "ofek-dev.routines" } }),
      } as Response;
    }
    if (url.endsWith("/api/extensions")) {
      return {
        ok: true,
        json: async () => ({
          extensions: [{ manifest: { id: "ofek-dev.routines" }, enabled: true }],
        }),
      } as Response;
    }
    throw new Error(`unexpected fetch ${url}`);
  });
}

describe("builtin-ids recovery drives the Routines flag", () => {
  it("re-loads ids after auth when bootstrap failed, flipping routines flag true", async () => {
    vi.resetModules();
    const state: FetchState = { idsOk: { value: false }, idsCalls: { n: 0 } };
    mockFetch(state);

    // 1) Bootstrap fires pre-login / while backend is unreachable → fails.
    const extIds = await import("../src/extensionIds");
    await extIds.loadBuiltinExtensionIds();
    expect(state.idsCalls.n).toBeGreaterThanOrEqual(1);
    expect(extIds.builtinIdsLoaded()).toBe(false);
    expect(extIds.extId("routines")).toBe("");

    // 2) User is now authenticated and the backend is reachable.
    state.idsOk.value = true;
    const bootstrapCalls = state.idsCalls.n;

    const { useBuiltinExtensionFlags } = await import(
      "../src/hooks/useBuiltinExtensionFlags"
    );
    function Probe() {
      const flags = useBuiltinExtensionFlags("authed");
      return <div data-testid="routines-flag">{String(flags.routines)}</div>;
    }
    render(<Probe />);

    // The hook must re-load the id map now that auth is present (pre-fix it
    // never does — this is the assertion that fails before the fix).
    await waitFor(() => expect(state.idsCalls.n).toBeGreaterThan(bootstrapCalls));
    // And with ids populated, the flag resolves true so the tab can render.
    await waitFor(() =>
      expect(screen.getByTestId("routines-flag").textContent).toBe("true"),
    );
  });
});
