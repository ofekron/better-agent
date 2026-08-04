import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { eventBus } from "../src/lib/eventBus";
import {
  ackSessionSeen,
  markSessionUnread,
  sessionRegistry,
  statusRankForRow,
  useProjectAggregate,
  useSessionMeta,
} from "../src/lib/sessionRegistry";
import type { ProjectAggregateRecord } from "../src/types";

type SessionRow = {
  id: string;
  cwd?: string;
  node_id?: string;
  is_running?: boolean;
  monitoring_state?: string;
  unread_count?: number;
  status_key?: "error" | "needs_decision" | "unread" | "open_work" | "running" | "all_done" | "idle";
  status_rank?: number;
};

const aggregate = (
  path: string,
  counts: Partial<Omit<ProjectAggregateRecord, "path" | "node_id">> = {},
  node_id = "primary",
): ProjectAggregateRecord => ({
  path,
  node_id,
  running_count: 0,
  unread_session_count: 0,
  waiting_for_user_count: 0,
  errored_count: 0,
  ...counts,
});

function stubSessions(sessions: SessionRow[]) {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ sessions }))));
}

async function resetRegistry(sessions: SessionRow[] = []) {
  sessionRegistry.unbind();
  sessionRegistry.__resetForTests();
  sessionRegistry.bind();
  stubSessions(sessions);
  await sessionRegistry.bootstrap();
}

function applySnapshot(epoch: string, revision: number, projects: ProjectAggregateRecord[]) {
  const token = sessionRegistry.beginProjectSnapshotFetch();
  return sessionRegistry.applyProjectSnapshot({ epoch, revision, projects }, token);
}

describe("sessionRegistry session projection", () => {
  beforeEach(async () => {
    await resetRegistry();
  });

  it("keeps visual status dimensions independent from canonical sort status", () => {
    eventBus.publish("session_created", {
      session: { id: "s", cwd: "/p", node_id: "primary" },
    });
    eventBus.publish("session_monitoring_changed", {
      session_id: "s",
      monitoring_state: "active",
      cwd: "/p",
      node_id: "primary",
    });

    expect(sessionRegistry.getSession("s")).toMatchObject({
      is_running: true,
      status_key: "idle",
      status_rank: 0,
    });

    eventBus.publish("session_status_changed", {
      session_id: "s",
      status_key: "running",
      status_rank: 2,
    });

    expect(sessionRegistry.getSession("s")).toMatchObject({
      is_running: true,
      status_key: "running",
      status_rank: 2,
    });
    expect(statusRankForRow({ id: "s", status_rank: 0 })).toBe(2);
  });

  it("uses authoritative row rank when no live entry exists", () => {
    expect(statusRankForRow({ id: "deep-page", status_rank: 5 })).toBe(5);
    expect(statusRankForRow({ id: "unranked" })).toBe(0);
  });

  it("does not materialize an unknown session from a status-only event", () => {
    eventBus.publish("session_status_changed", {
      session_id: "unknown",
      status_key: "error",
      status_rank: 6,
    });
    expect(sessionRegistry.peekMeta("unknown")).toBeNull();
  });

  it("preserves stable hook snapshots across reads", () => {
    eventBus.publish("session_created", { session: { id: "s", cwd: "/p" } });
    eventBus.publish("session_unread_changed", {
      session_id: "s",
      unread_count: 3,
      cwd: "/p",
    });
    expect(sessionRegistry.getSession("s")).toBe(sessionRegistry.getSession("s"));
  });

  it("returns one stable empty session value", () => {
    expect(sessionRegistry.getSession("a")).toBe(sessionRegistry.getSession("b"));
    expect(sessionRegistry.getSession("a")).toMatchObject({
      is_running: false,
      status_key: "idle",
      status_rank: 0,
    });
  });
});

describe("sessionRegistry project aggregate projection", () => {
  beforeEach(async () => {
    await resetRegistry();
  });

  it("replaces the full projection from a REST snapshot", () => {
    expect(applySnapshot("epoch-a", 2, [
      aggregate("/p", { running_count: 2, errored_count: 1 }),
      aggregate("/q", { unread_session_count: 3 }),
    ])).toBe(true);

    expect(sessionRegistry.getProject("/p", "primary")).toEqual({
      running_count: 2,
      unread_session_count: 0,
      waiting_for_user_count: 0,
      errored_count: 1,
    });
    expect(sessionRegistry.getProject("/q", "primary").unread_session_count).toBe(3);
  });

  it("applies the next revision atomically and deletes tombstones", () => {
    applySnapshot("epoch-a", 4, [
      aggregate("/p", { running_count: 1 }),
      aggregate("/q", { unread_session_count: 1 }),
    ]);

    eventBus.publish("project_aggregates_changed", {
      epoch: "epoch-a",
      revision: 5,
      upserts: [aggregate("/p", { waiting_for_user_count: 2 })],
      tombstones: [aggregate("/q")],
    });

    expect(sessionRegistry.getProject("/p", "primary").waiting_for_user_count).toBe(2);
    expect(sessionRegistry.getProject("/q", "primary")).toEqual({
      running_count: 0,
      unread_session_count: 0,
      waiting_for_user_count: 0,
      errored_count: 0,
    });
  });

  it("ignores duplicate and stale same-epoch revisions", () => {
    applySnapshot("epoch-a", 5, [aggregate("/p", { running_count: 2 })]);
    for (const revision of [5, 4]) {
      eventBus.publish("project_aggregates_changed", {
        epoch: "epoch-a",
        revision,
        upserts: [aggregate("/p", { running_count: 99 })],
        tombstones: [],
      });
    }
    expect(sessionRegistry.getProject("/p", "primary").running_count).toBe(2);
  });

  it("refetches a same-epoch gap instead of applying it", async () => {
    applySnapshot("epoch-a", 1, [aggregate("/p", { running_count: 1 })]);
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      epoch: "epoch-a",
      revision: 3,
      projects: [aggregate("/p", { running_count: 3 })],
    })));
    vi.stubGlobal("fetch", fetchMock);

    eventBus.publish("project_aggregates_changed", {
      epoch: "epoch-a",
      revision: 3,
      upserts: [aggregate("/p", { running_count: 99 })],
      tombstones: [],
    });

    expect(sessionRegistry.getProject("/p", "primary").running_count).toBe(1);
    await waitFor(() => {
      expect(sessionRegistry.getProject("/p", "primary").running_count).toBe(3);
    });
    expect(fetchMock).toHaveBeenCalledWith("/api/projects");
  });

  it("accepts a new epoch by clearing stale data and hydrating its full snapshot", async () => {
    applySnapshot("epoch-a", 8, [aggregate("/old", { running_count: 1 })]);
    let resolveFetch!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    })));

    eventBus.publish("project_aggregates_changed", {
      epoch: "epoch-b",
      revision: 1,
      upserts: [aggregate("/partial", { running_count: 1 })],
      tombstones: [],
    });

    expect(sessionRegistry.getProject("/old", "primary").running_count).toBe(0);
    expect(sessionRegistry.getProject("/partial", "primary").running_count).toBe(0);

    resolveFetch(new Response(JSON.stringify({
      epoch: "epoch-b",
      revision: 1,
      projects: [aggregate("/complete", { errored_count: 2 })],
    })));
    await waitFor(() => {
      expect(sessionRegistry.getProject("/complete", "primary").errored_count).toBe(2);
    });
  });

  it("ignores a stale same-epoch REST snapshot", () => {
    applySnapshot("epoch-a", 4, [aggregate("/p", { running_count: 4 })]);
    const token = sessionRegistry.beginProjectSnapshotFetch();
    expect(sessionRegistry.applyProjectSnapshot({
      epoch: "epoch-a",
      revision: 3,
      projects: [aggregate("/p", { running_count: 3 })],
    }, token)).toBe(false);
    expect(sessionRegistry.getProject("/p", "primary").running_count).toBe(4);
  });

  it("ignores an old-epoch response dispatched before an epoch change", async () => {
    applySnapshot("epoch-a", 1, [aggregate("/p", { running_count: 1 })]);
    const staleToken = sessionRegistry.beginProjectSnapshotFetch();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      epoch: "epoch-b",
      revision: 1,
      projects: [],
    }))));
    eventBus.publish("project_aggregates_changed", {
      epoch: "epoch-b",
      revision: 1,
      upserts: [],
      tombstones: [],
    });

    expect(sessionRegistry.applyProjectSnapshot({
      epoch: "epoch-a",
      revision: 2,
      projects: [aggregate("/p", { running_count: 9 })],
    }, staleToken)).toBe(false);
    await waitFor(() => {
      expect(sessionRegistry.getProject("/p", "primary").running_count).toBe(0);
    });
  });

  it("notifies only changed project keys", () => {
    applySnapshot("epoch-a", 0, [aggregate("/p"), aggregate("/q")]);
    let p = 0;
    let q = 0;
    const offP = sessionRegistry.subscribeProject("/p", "primary", () => p++);
    const offQ = sessionRegistry.subscribeProject("/q", "primary", () => q++);
    eventBus.publish("project_aggregates_changed", {
      epoch: "epoch-a",
      revision: 1,
      upserts: [aggregate("/p", { unread_session_count: 1 })],
      tombstones: [],
    });
    expect({ p, q }).toEqual({ p: 1, q: 0 });
    offP();
    offQ();
  });
});

describe("sessionRegistry hooks and commands", () => {
  beforeEach(async () => {
    await resetRegistry([{ id: "s", cwd: "/p", status_key: "idle", status_rank: 0 }]);
  });

  it("updates session and project hooks from authoritative events", () => {
    applySnapshot("epoch-a", 0, [aggregate("/p")]);
    const session = renderHook(() => useSessionMeta("s"));
    const project = renderHook(() => useProjectAggregate("/p"));

    act(() => {
      eventBus.publish("session_status_changed", {
        session_id: "s",
        status_key: "error",
        status_rank: 6,
      });
      eventBus.publish("project_aggregates_changed", {
        epoch: "epoch-a",
        revision: 1,
        upserts: [aggregate("/p", { errored_count: 1 })],
        tombstones: [],
      });
    });

    expect(session.result.current.status_key).toBe("error");
    expect(project.result.current.errored_count).toBe(1);
    session.unmount();
    project.unmount();
  });

  it("posts seen and unread commands", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", fetchMock);
    await ackSessionSeen("s", "uid");
    await markSessionUnread("s");
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/sessions/s/seen",
      "/api/sessions/s/unread",
    ]);
  });
});
