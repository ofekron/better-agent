import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The open-session tab list is backend-owned. `applyBackendSnapshot`'s
 * `seedUp` merge exists only to migrate a browser that predates the
 * backend store — it must run at most once per browser. Otherwise a tab
 * the backend closed (e.g. because its session was deleted while this
 * client was offline) gets re-pushed from the stale localStorage cache on
 * the next mount and the deleted session's tab comes back.
 */

const queued: Array<{ url: string; body: unknown }> = [];

vi.mock("../src/utils/writeBacklog", () => ({
  queueWrite: (write: { url: string; body: unknown }) => {
    queued.push({ url: write.url, body: write.body });
  },
}));

async function loadModule() {
  vi.resetModules();
  return await import("../src/utils/uiSelection");
}

const EMPTY_SNAPSHOT = {
  selected_project: null,
  remembered_session_by_project: {},
  open_session_tab_ids: [] as string[],
  open_session_tab_joined_at: {} as Record<string, string>,
};

describe("uiSelection open tabs", () => {
  beforeEach(() => {
    localStorage.clear();
    queued.length = 0;
  });

  it("seeds a legacy localStorage-only tab list up to the backend once", async () => {
    localStorage.setItem(
      "better-agent-open-session-ids",
      JSON.stringify(["legacy-a"]),
    );
    const mod = await loadModule();

    mod.applyBackendSnapshot(EMPTY_SNAPSHOT, true);

    expect(mod.getOpenSessionTabIds()).toEqual(["legacy-a"]);
    expect(
      queued.filter((w) => (w.body as Record<string, unknown>).open_session_tab_ids),
    ).toHaveLength(1);
  });

  it("does not resurrect a tab the backend closed after the one-time seed", async () => {
    localStorage.setItem(
      "better-agent-open-session-ids",
      JSON.stringify(["kept", "deleted-sid"]),
    );
    const mod = await loadModule();

    // First mount migrates the legacy list up.
    mod.applyBackendSnapshot(EMPTY_SNAPSHOT, true);
    expect(mod.getOpenSessionTabIds()).toEqual(["kept", "deleted-sid"]);
    queued.length = 0;

    // The session behind `deleted-sid` is deleted while this client is
    // offline, so the backend snapshot on the next mount omits its tab.
    const afterDelete = await loadModule();
    afterDelete.applyBackendSnapshot(
      { ...EMPTY_SNAPSHOT, open_session_tab_ids: ["kept"] },
      true,
    );

    expect(afterDelete.getOpenSessionTabIds()).toEqual(["kept"]);
    expect(
      queued.filter((w) => (w.body as Record<string, unknown>).open_session_tab_ids),
    ).toEqual([]);
  });

  it("lets a live backend push close a tab", async () => {
    const mod = await loadModule();
    mod.setOpenSessionTabIds(["kept", "deleted-sid"]);
    queued.length = 0;

    mod.applyBackendSnapshot(
      { ...EMPTY_SNAPSHOT, open_session_tab_ids: ["kept"] },
      false,
    );

    expect(mod.getOpenSessionTabIds()).toEqual(["kept"]);
    expect(queued).toEqual([]);
  });
});
