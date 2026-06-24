import { describe, expect, it, vi } from "vitest";
import {
  clearHardRefreshMarker,
  hardRefreshCurrentPage,
} from "../src/lib/hardRefresh";

describe("hard refresh", () => {
  it("unregisters the service worker and replaces the current URL with a cache-buster", async () => {
    const unregister = vi.fn(async () => true);
    const clearCaches = vi.fn(async () => {});
    const replace = vi.fn();

    await hardRefreshCurrentPage(
      "request-123",
      {
        href: "http://localhost:8000/s/session-1?panel=files#message-2",
        replace,
      },
      async () => ({ unregister }),
      clearCaches,
    );

    expect(unregister).toHaveBeenCalledOnce();
    expect(clearCaches).toHaveBeenCalledOnce();
    expect(replace).toHaveBeenCalledWith(
      "http://localhost:8000/s/session-1?panel=files&_better_agent_refresh=request-123#message-2",
    );
  });

  it("still navigates when service-worker removal fails", async () => {
    const clearCaches = vi.fn(async () => {});
    const replace = vi.fn();

    await hardRefreshCurrentPage(
      "request-456",
      { href: "http://localhost:8000/current", replace },
      async () => { throw new Error("unregister failed"); },
      clearCaches,
    );

    expect(clearCaches).toHaveBeenCalledOnce();
    expect(replace).toHaveBeenCalledWith(
      "http://localhost:8000/current?_better_agent_refresh=request-456",
    );
  });

  it("removes only the hard-refresh marker after the new page loads", () => {
    const replaceState = vi.fn();

    clearHardRefreshMarker(
      "http://localhost:8000/s/session-1?panel=files&_better_agent_refresh=request-123#message-2",
      replaceState,
    );

    expect(replaceState).toHaveBeenCalledWith(
      "/s/session-1?panel=files#message-2",
    );
  });
});
