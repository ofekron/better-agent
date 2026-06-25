import { afterEach, describe, expect, it, vi } from "vitest";
import { sessionRegistry } from "../src/lib/sessionRegistry";

describe("session registry incomplete snapshots", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    sessionRegistry.__resetForTests();
  });

  it("keeps existing registry state and retries an incomplete empty bootstrap", async () => {
    vi.useFakeTimers();
    sessionRegistry.__resetForTests();
    sessionRegistry.replaceFromRows([{
      id: "existing",
      cwd: "/tmp/project",
      node_id: "primary",
      is_running: false,
      unread_count: 2,
    }]);

    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        sessions: [],
        snapshot_complete: false,
        index_warming: true,
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        sessions: [{
          id: "existing",
          cwd: "/tmp/project",
          node_id: "primary",
          is_running: false,
          unread_count: 2,
        }],
        snapshot_complete: true,
        index_warming: false,
      }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await sessionRegistry.bootstrap();
    expect(sessionRegistry.peekMeta("existing")?.unread_count).toBe(2);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(150);
    await Promise.resolve();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(sessionRegistry.peekMeta("existing")?.unread_count).toBe(2);
  });
});
