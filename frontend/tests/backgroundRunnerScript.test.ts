import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import vm from "node:vm";
import { describe, expect, it, vi } from "vitest";

type Handler = (
  resolve: (value?: unknown) => void,
  reject: (error?: unknown) => void,
  args?: Record<string, unknown>,
) => void;

function createRunner(fetchImpl: typeof fetch) {
  const handlers = new Map<string, Handler>();
  const values = new Map<string, string>();
  const context = vm.createContext({
    addEventListener: (name: string, handler: Handler) => handlers.set(name, handler),
    CapacitorKV: {
      get: (key: string) => {
        const value = values.get(key);
        return value === undefined ? null : { value };
      },
      set: (key: string, value: string) => values.set(key, value),
      remove: (key: string) => values.delete(key),
    },
    CapacitorDevice: { getNetworkStatus: () => ({ connected: true }) },
    fetch: fetchImpl,
    console,
    Error,
    JSON,
    Set,
  });
  new vm.Script(
    readFileSync(resolve("public/runners/offline-sync.js"), "utf8"),
  ).runInContext(context);
  const dispatch = (name: string, args?: Record<string, unknown>) =>
    new Promise<unknown>((resolve, reject) => handlers.get(name)!(resolve, reject, args));
  return { dispatch, values };
}

describe("offline background runner", () => {
  it("accepts every empty-state operation when native storage returns null", async () => {
    const fetchMock = vi.fn<typeof fetch>();
    const runner = createRunner(fetchMock);

    await expect(runner.dispatch("getOfflineAcknowledgements")).resolves.toEqual({
      acknowledged: [],
    });
    await expect(runner.dispatch("clearOfflineAcknowledgements")).resolves.toBeUndefined();
    await expect(runner.dispatch("syncOfflineActions")).resolves.toBeUndefined();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("retains rejected actions and durably acknowledges accepted actions", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      results: [
        { index: 0, accepted: true },
        { index: 1, accepted: false, status: 400, error: "invalid" },
      ],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const runner = createRunner(fetchMock);
    const accepted = { sessionId: "session-1", clientId: "client-1", prompt: "one" };
    const rejected = { sessionId: "session-2", clientId: "client-2", prompt: "two" };

    await runner.dispatch("updateOfflineState", {
      state: JSON.stringify({
        serverUrl: "https://agent.example",
        accessToken: "token",
        actions: [accepted, rejected],
      }),
    });
    await runner.dispatch("syncOfflineActions");

    const stored = JSON.parse(runner.values.get("better_agent_offline_sync_state")!);
    expect(stored.actions).toEqual([rejected]);
    expect(stored.acknowledged).toEqual([
      { sessionId: "session-1", clientId: "client-1" },
    ]);
  });

  it("does not resurrect acknowledged actions from a stale WebView mirror", async () => {
    const runner = createRunner(vi.fn<typeof fetch>());
    const action = { sessionId: "session-1", clientId: "client-1", prompt: "one" };
    runner.values.set("better_agent_offline_sync_state", JSON.stringify({
      actions: [],
      acknowledged: [{ sessionId: "session-1", clientId: "client-1" }],
    }));

    await runner.dispatch("updateOfflineState", {
      state: JSON.stringify({
        serverUrl: "https://agent.example",
        accessToken: "token",
        actions: [action],
      }),
    });

    const stored = JSON.parse(runner.values.get("better_agent_offline_sync_state")!);
    expect(stored.actions).toEqual([]);
  });
});
