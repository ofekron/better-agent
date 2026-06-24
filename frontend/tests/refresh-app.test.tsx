import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen } from "@testing-library/react";
import { renderApp } from "./harness";
import "../src/i18n";

async function openRefreshModal(h: Awaited<ReturnType<typeof renderApp>>) {
  if (h.$('button[aria-label="Settings"]')) {
    await h.click('button[aria-label="Settings"]');
  }
  await h.clickByText(/Rebuild frontend \+ restart backend/);
}

async function waitForRestartStatus(h: Awaited<ReturnType<typeof renderApp>>) {
  for (let i = 0; i < 5; i += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 550));
    });
    await h.flush();
    const call = h.restCalls.find(
      (item) => item.method === "GET" && item.path.startsWith("/api/admin/restart-status/"),
    );
    if (call) return call;
  }
  return undefined;
}

describe("app refresh", () => {
  beforeEach(() => {
    vi.stubGlobal("__BUILD_HASH__", "test-hash");
    vi.stubGlobal("__BUILD_TIME__", "2026-06-23T00:00:00Z");
  });

  it("opens a restart mode modal and sends the idle mode", async () => {
    const h = await renderApp();
    try {
      await openRefreshModal(h);
      expect(screen.getByRole("dialog", { name: "Refresh Better Agent" })).toBeTruthy();

      await h.clickByText(/Wait for idle/);
      await h.flush();

      expect(h.restCalls).toContainEqual(
        expect.objectContaining({
          method: "POST",
          path: "/api/admin/restart",
          body: expect.objectContaining({ mode: "idle" }),
        }),
      );
    } finally {
      h.unmount();
    }
  });

  it("sends immediate refresh mode from the modal", async () => {
    const h = await renderApp();
    try {
      await openRefreshModal(h);
      await h.clickByText(/Refresh now/);
      await h.flush();

      expect(h.restCalls).toContainEqual(
        expect.objectContaining({
          method: "POST",
          path: "/api/admin/restart",
          body: expect.objectContaining({ mode: "now" }),
        }),
      );
    } finally {
      h.unmount();
    }
  });

  it("stops when a dropped restart POST was not accepted", async () => {
    const h = await renderApp();
    try {
      h.backend.failRestartPost("before-accept");
      await openRefreshModal(h);
      await h.clickByText(/Refresh now/);

      const statusCall = await waitForRestartStatus(h);

      expect(statusCall).toBeDefined();
      expect(h.restCalls.some((item) => item.path === "/api/build-info")).toBe(false);
      expect(await screen.findByText("Backend did not accept the refresh request. Try again.")).toBeTruthy();
    } finally {
      h.unmount();
    }
  });

  it("continues when a dropped restart POST was accepted", async () => {
    const h = await renderApp();
    try {
      h.backend.failRestartPost("after-accept");
      await openRefreshModal(h);
      await h.clickByText(/Refresh now/);

      const statusCall = await waitForRestartStatus(h);

      expect(statusCall).toBeDefined();
      expect(h.restCalls).toContainEqual(
        expect.objectContaining({
          method: "GET",
          path: expect.stringMatching(/^\/api\/admin\/restart-status\//),
        }),
      );
      expect(screen.queryByText("Backend did not accept the refresh request. Try again.")).toBeNull();
    } finally {
      h.unmount();
    }
  });
});
