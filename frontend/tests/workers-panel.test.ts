import React from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { act, fireEvent, render } from "@testing-library/react";
import { Component } from "../../better-agent-private/extensions/team-orchestration/ui/team-sidebar.entry.js";
import { makeSession, makeWorker } from "./fixtures";
import type { WorkerInfo } from "../src/types";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 10));
  });
}

function renderPanel(workers: WorkerInfo[], extra: Record<string, unknown> = {}) {
  const calls: Array<{ method: string; path: string; body: unknown }> = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(typeof input === "string" ? input : input.url, "http://localhost:8000");
    const path = url.pathname.replace("/api/extensions/ofek-dev.team-orchestration/backend", "/api");
    const method = (init?.method ?? "GET").toUpperCase();
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ method, path, body });
    if (method === "GET" && path === "/api/workers") {
      const pools = new Map<string, WorkerInfo[]>();
      for (const worker of workers) {
        for (const tag of worker.tags ?? []) {
          pools.set(tag, [...(pools.get(tag) ?? []), worker]);
        }
      }
      return response({
        workers,
        pools: Array.from(pools.entries()).map(([tag, poolWorkers]) => ({
          tag,
          workers: poolWorkers,
          queued_count: 0,
        })),
        teams: extra.teams ?? [],
      });
    }
    if (method === "POST" && path === "/api/workers") return response({ ok: true });
    if (method === "POST" && path === "/api/workers/from_session") return response({ ok: true });
    if (method === "PUT" && path.includes("/api/sessions/")) return response({ ok: true });
    return response({ ok: true });
  });
  const session = makeSession({ orchestration_mode: "team" });
  const view = render(React.createElement(Component, {
    React,
    context: {
      slot: "team-sidebar",
      apiBaseUrl: "http://localhost:8000",
      cwd: session.cwd,
      sessionId: session.id,
      model: session.model,
      providerId: session.provider_id ?? "",
      reasoningEffort: session.reasoning_effort ?? "",
      nodeId: session.node_id ?? "primary",
      workerCreationPolicy: "ask",
      sessions: [session],
      events: [],
      ...extra,
    },
  }));
  return { ...view, calls };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("workers panel actions", () => {
  it("workers panel shows the seeded worker count", async () => {
    const h = renderPanel([
      makeWorker({ agent_session_id: "w1", name: "Indexer" }),
      makeWorker({ agent_session_id: "w2", name: "Reviewer" }),
    ]);
    await settle();

    expect(h.container.querySelectorAll(".worker-row")).toHaveLength(2);
    expect(h.container.textContent).toContain("Indexer");
    expect(h.container.textContent).toContain("Reviewer");
  });

  it("'New' opens the create form, submitting POSTs /api/workers", async () => {
    const h = renderPanel([]);
    await settle();

    fireEvent.click(Array.from(h.container.querySelectorAll("button")).find((b) => b.textContent === "New")!);
    fireEvent.change(h.container.querySelector("input[type='text']")!, { target: { value: "Codebase explorer" } });
    fireEvent.click(Array.from(h.container.querySelectorAll("button")).find((b) => b.textContent === "Create")!);
    await settle();

    const post = h.calls.find((call) => call.method === "POST" && call.path === "/api/workers");
    expect(post?.body).toMatchObject({
      cwd: "/tmp/proj",
      description: "Codebase explorer",
      orchestration_mode: "native",
    });
  });

  it("'Existing' is disabled when no eligible Better Agent sessions exist", async () => {
    const session = makeSession({ orchestration_mode: "team" });
    const h = renderPanel([makeWorker({ agent_session_id: session.id })], { sessions: [session] });
    await settle();

    const existing = Array.from(h.container.querySelectorAll("button")).find((b) => b.textContent === "Existing")!;
    expect((existing as HTMLButtonElement).disabled).toBe(true);
  });

  it("workers list shows delegation count badge per row", async () => {
    const h = renderPanel([
      makeWorker({ agent_session_id: "w1", name: "A", delegation_count: 7 }),
    ]);
    await settle();

    const row = h.container.querySelector(".worker-row") as HTMLElement;
    fireEvent.click(row.querySelector(".worker-row-header") as HTMLElement);
    expect(row.textContent).toContain("7");
    expect(row.textContent).toContain("delegations");
  });

  it("workers panel renders every worker in one flat list", async () => {
    const h = renderPanel([
      makeWorker({ agent_session_id: "w1", name: "Reviewer A", tags: ["review"] }),
      makeWorker({ agent_session_id: "w2", name: "Reviewer B", tags: ["review"] }),
      makeWorker({ agent_session_id: "w3", name: "Builder", tags: ["build"] }),
    ], {
      teams: [{
        id: "team-1",
        name: "UI team",
        workers: [
          makeWorker({ agent_session_id: "w1", name: "Reviewer A", team_binding: "bound" }),
          makeWorker({ agent_session_id: "w3", name: "Builder", team_binding: "available" }),
        ],
      }],
    });
    await settle();

    expect(h.container.querySelectorAll(".worker-row")).toHaveLength(3);
    expect(h.container.querySelector(".worker-group-header")).toBeNull();
    expect(h.container.textContent).not.toContain("Pool:");
    expect(h.container.textContent).not.toContain("Team:");
    expect(h.container.textContent).toContain("Reviewer A");
    expect(h.container.textContent).toContain("Reviewer B");
    expect(h.container.textContent).toContain("Builder");
  });
});
