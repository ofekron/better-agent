import { render, screen, waitFor } from "@testing-library/react";
import * as React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
// The routines sidebar module is served directly from the private extension
// source in local-runtime "source" mode, so we exercise the real bundle.
import { Component } from "../../better-agent-private/extensions/routines/ui/routines-sidebar.entry.js";

// Regression: the panel gated its list fetch on `if (!apiBaseUrl || !cwd)`.
// In same-origin mode (the default) the injected apiBaseUrl is "" — a valid
// relative base — so the truthy check bailed and the list NEVER fetched,
// permanently showing "No routines yet" even when routines exist.

afterEach(() => vi.restoreAllMocks());

describe("routines panel list fetch", () => {
  it("fetches routines when apiBaseUrl is empty (same-origin base)", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => {
        const url = String(input);
        if (url.includes("/api/extensions/ofek-dev.routines/backend/routines")) {
          return {
            ok: true,
            json: async () => ({
              routines: [
                { id: "r1", name: "My Routine", orchestration_mode: "native", run_count: 0 },
              ],
            }),
          } as Response;
        }
        throw new Error(`unexpected fetch ${url}`);
      });

    render(
      React.createElement(Component, {
        context: { apiBaseUrl: "", cwd: "/repo", nodeId: "primary", events: [] },
        React,
      }),
    );

    // Pre-fix this never fires (guard bailed on the empty base) → times out.
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(String(fetchMock.mock.calls[0][0])).toContain(
      "/api/extensions/ofek-dev.routines/backend/routines?cwd=%2Frepo",
    );
    await screen.findByText("My Routine");
  });
});
