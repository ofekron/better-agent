// @vitest-environment happy-dom

import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";

beforeEach(() => {
  vi.resetModules();
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(null, { status: 204 }))));
  vi.stubGlobal("crypto", { randomUUID: vi.fn(() => "00000000-0000-4000-8000-000000000123") });
});

describe("three-state sync", () => {
  it("routes batch-2 organization and note mutations through the canonical sync runner", () => {
    const sessionList = readFileSync("src/components/SessionList.tsx", "utf8");
    const app = readFileSync("src/App.tsx", "utf8");

    expect(sessionList).toContain("runThreeStateSync({");
    expect(sessionList).toContain("session:organization:folder:${sessionId}");
    expect(sessionList).toContain("session:organization:tags:${sessionId}");
    expect(sessionList).toContain("reconcile: refreshOrganization");
    expect(app).toContain("session:notes:add:${sessionId}");
    expect(app).toContain("session:notes:remove:${sessionId}:${noteId}");
    expect(app).toContain("session:notes:update:${sessionId}:${noteId}");
    expect(app).toContain("reconcile: () => applySessionMetadata(sessionId, { notes: previousNotes })");
    expect(app).toContain("session:right-panel:${sessionId}");
    expect(app).toContain("filePanel:add:${currentSession.id}:${id}");
    expect(app).toContain("configPanel:remove:${currentSession.id}:${id}");
    expect(app).toContain("session:forkAndSend:${parentId}");
    expect(app).toContain("session:closeFork:${forkSessionId}");
    expect(app).toContain("session:reopenFork:${forkSessionId}");
    expect(app).toContain("reconcile: refreshSessions");

    const selector = readFileSync("src/components/SessionSelectorControls.tsx", "utf8");
    expect(selector).toContain("runThreeStateSync({");
    expect(selector).toContain("reconcile: () => onChange(prev)");

    const registry = readFileSync("src/sync/frontendBackendMutationCoverage.ts", "utf8");
    expect(registry).toContain('routeIncludes: "topbar-pin", reason: "explicit-ack-backlog"');
    expect(registry).toContain('routeIncludes: "/api/session-folders", reason: "canonical-caller"');
    expect(registry).toContain('file: "src/components/SessionTabsSettings.tsx"');
  });

  it("proves schedule deletes are controlled by canonical caller sync", () => {
    const schedulesPage = readFileSync("src/components/SchedulesPage.tsx", "utf8");
    const backgroundStrip = readFileSync(
      "src/components/SessionBackgroundStrip.tsx",
      "utf8",
    );
    const communications = readFileSync("src/components/CommunicationsView.tsx", "utf8");
    const chat = readFileSync("src/components/Chat.tsx", "utf8");
    const registry = readFileSync("src/sync/frontendBackendMutationCoverage.ts", "utf8");

    expect(schedulesPage).toContain("runThreeStateSync({");
    expect(schedulesPage).toContain("schedule:delete:${id}");
    expect(schedulesPage).toContain("reconcile: load");
    expect(backgroundStrip).toContain("runThreeStateSync({");
    expect(backgroundStrip).toContain("schedule:cancel:${schedule.id}");
    expect(backgroundStrip).toContain("fetchSessionSchedules(schedule.app_session_id)");
    expect(communications).toContain("communications:chat:${item.chat_id}");
    expect(communications).toContain("reconcile: onPosted");
    expect(chat).toContain("schedule:create:${session.id}");
    expect(chat).toContain("reconcile: async () => { await fetchSessionSchedules(session.id); }");
    expect(registry).toContain(
      'method: "POST", routeIncludes: "/api/chats/", reason: "canonical-caller"',
    );
    expect(registry).toContain(
      'method: "POST", routeIncludes: "/backend/sessions/", reason: "canonical-caller"',
    );
    expect(registry).toContain(
      'method: "DELETE", routeIncludes: "/api/schedules/", reason: "canonical-caller"',
    );
    expect(registry).toContain(
      'method: "DELETE", routeIncludes: "/backend/schedules/", reason: "canonical-caller"',
    );
  });

  it("stays pending until the expected authoritative state arrives", async () => {
    const { beginThreeStateSync, isInflight } = await import("../../src/progress/store");
    const controller = beginThreeStateSync<{ name: string }>({
      operationId: "session:rename:1",
      action: "Rename session",
      expectedAuthoritativeState: (state) => state.name === "New name",
      reconcile: vi.fn(),
    });

    expect(isInflight("session:rename:1")).toBe(true);
    expect(controller.observeAuthoritativeState({ name: "Old name" })).toBe(false);
    expect(isInflight("session:rename:1")).toBe(true);
    expect(controller.observeAuthoritativeState({ name: "New name" })).toBe(true);
    expect(isInflight("session:rename:1")).toBe(false);
  });

  it("confirms explicit acknowledgements and does not require a snapshot", async () => {
    const { runThreeStateSync, isInflight } = await import("../../src/progress/store");
    const run = runThreeStateSync({
      operationId: "project:add:1",
      action: "Add project",
      mutate: async () => ({ accepted: true }),
      isAcknowledged: (result) => result.accepted,
      reconcile: vi.fn(),
    });
    const { controller } = await run;

    expect(controller.correlationId).toBe("00000000-0000-4000-8000-000000000123");
    expect(isInflight("project:add:1")).toBe(false);
  });

  it("exposes the controller before HTTP resolves and requires authoritative confirmation", async () => {
    const { runThreeStateSync, isInflight } = await import("../../src/progress/store");
    let resolveMutation!: (value: { ok: boolean }) => void;
    const run = runThreeStateSync({
      operationId: "session:rename:observer",
      action: "Rename session",
      mutate: () => new Promise((resolve) => { resolveMutation = resolve; }),
      expectedAuthoritativeState: (state: { name: string }) => state.name === "saved",
      isAcknowledged: () => true,
      reconcile: vi.fn(),
    });

    expect(run.controller.observeAuthoritativeState({ name: "old" })).toBe(false);
    resolveMutation({ ok: true });
    await run;
    expect(isInflight("session:rename:observer")).toBe(true);
    expect(run.controller.observeAuthoritativeState({ name: "saved" })).toBe(true);
    expect(isInflight("session:rename:observer")).toBe(false);
  });

  it("does not let an older acknowledgement clear a newer correlated mutation", async () => {
    const { beginThreeStateSync, isInflight } = await import("../../src/progress/store");
    const first = beginThreeStateSync<never>({
      operationId: "session:rename:overlap",
      action: "Rename session",
      reconcile: vi.fn(),
    });
    vi.mocked(crypto.randomUUID).mockReturnValue("00000000-0000-4000-8000-000000000456");
    const second = beginThreeStateSync<never>({
      operationId: "session:rename:overlap",
      action: "Rename session",
      reconcile: vi.fn(),
    });

    first.confirmAcknowledgement();
    expect(isInflight("session:rename:overlap")).toBe(true);
    second.confirmAcknowledgement();
    expect(isInflight("session:rename:overlap")).toBe(false);
  });

  it("reconciles and logs superseded failures without settling the current operation", async () => {
    const { beginThreeStateSync, isInflight } = await import("../../src/progress/store");
    const firstReconcile = vi.fn();
    const first = beginThreeStateSync<never>({
      operationId: "session:rename:superseded-failure",
      action: "Rename session",
      reconcile: firstReconcile,
    });
    vi.mocked(crypto.randomUUID).mockReturnValue("00000000-0000-4000-8000-000000000457");
    const second = beginThreeStateSync<never>({
      operationId: "session:rename:superseded-failure",
      action: "Rename session",
      reconcile: vi.fn(),
    });

    await first.fail(new Error("older request failed"));
    expect(firstReconcile).toHaveBeenCalledOnce();
    expect(isInflight("session:rename:superseded-failure")).toBe(true);
    second.confirmAcknowledgement();
    expect(isInflight("session:rename:superseded-failure")).toBe(false);
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(vi.mocked(fetch).mock.calls.some((call) =>
      String(call[1]?.body).includes("00000000-0000-4000-8000-000000000123"),
    )).toBe(true);
  });

  it("keeps the newest failure when a superseded failure arrives afterward", async () => {
    await import("../../src/i18n");
    const { beginThreeStateSync } = await import("../../src/progress/store");
    const { SyncFailureToast } = await import("../../src/components/SyncFailureToast");
    const firstReconcile = vi.fn();
    const first = beginThreeStateSync<never>({
      operationId: "session:delete:failure-order",
      action: "First delete",
      reconcile: firstReconcile,
    });
    vi.mocked(crypto.randomUUID).mockReturnValue("00000000-0000-4000-8000-000000000458");
    const second = beginThreeStateSync<never>({
      operationId: "session:delete:failure-order",
      action: "Second delete",
      reconcile: vi.fn(),
    });
    render(<SyncFailureToast />);

    await act(async () => {
      await second.fail(new Error("new failure"));
      await first.fail(new Error("old failure"));
    });

    expect(firstReconcile).toHaveBeenCalledOnce();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getByRole("alert").textContent).toContain("Second delete failed");
  });

  it("replaces an existing failure when a newer operation starts", async () => {
    await import("../../src/i18n");
    const { beginThreeStateSync, isInflight } = await import("../../src/progress/store");
    const { SyncFailureToast } = await import("../../src/components/SyncFailureToast");
    const first = beginThreeStateSync<never>({
      operationId: "session:delete:failure-then-new",
      action: "First delete",
      reconcile: vi.fn(),
    });
    render(<SyncFailureToast />);
    await act(() => first.fail(new Error("first failure")));
    expect(screen.getByRole("alert")).toBeTruthy();

    vi.mocked(crypto.randomUUID).mockReturnValue("00000000-0000-4000-8000-000000000459");
    act(() => {
      beginThreeStateSync<never>({
        operationId: "session:delete:failure-then-new",
        action: "Second delete",
        reconcile: vi.fn(),
      });
    });

    expect(screen.queryByRole("alert")).toBeNull();
    expect(isInflight("session:delete:failure-then-new")).toBe(true);
  });

  it("reconciles before publishing a detailed correlated failure", async () => {
    const reconciled: string[] = [];
    await import("../../src/i18n");
    const { beginThreeStateSync } = await import("../../src/progress/store");
    const { SyncFailureToast } = await import("../../src/components/SyncFailureToast");
    const controller = beginThreeStateSync<never>({
      operationId: "session:delete:1",
      action: "Delete session",
      info: "The session remains available.",
      reconcile: async () => { reconciled.push("done"); },
    });
    render(<SyncFailureToast />);

    await act(() => controller.fail(new Error("backend rejected"), "Conflict in session revision"));

    expect(reconciled).toEqual(["done"]);
    expect(screen.getByRole("alert").textContent).toContain("Delete session failed");
    expect(screen.getByText("The session remains available.")).toBeTruthy();
    fireEvent.click(screen.getByText("Show details"));
    expect(screen.queryByText("Conflict in session revision")).toBeNull();
    expect(screen.getByText(/00000000-0000-4000-8000-000000000123/)).toBeTruthy();

    await new Promise((resolve) => window.setTimeout(resolve, 0));
    const body = String(vi.mocked(fetch).mock.calls.at(-1)?.[1]?.body);
    expect(body).toContain("00000000-0000-4000-8000-000000000123");
    expect(body).toContain('"action_key":"session.delete"');
    expect(body).not.toContain("session:delete:1");

    fireEvent.click(screen.getByLabelText("Dismiss"));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("settles and logs mutation plus reconciliation failures", async () => {
    await import("../../src/i18n");
    const { beginThreeStateSync, isInflight } = await import("../../src/progress/store");
    const controller = beginThreeStateSync<never>({
      operationId: "session:delete:reconcile-failure",
      action: "Delete session",
      reconcile: () => { throw new Error("reconcile exploded"); },
    });

    await expect(controller.fail(new Error("mutation exploded"))).resolves.toBeUndefined();
    expect(isInflight("session:delete:reconcile-failure")).toBe(false);
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    const bodies = vi.mocked(fetch).mock.calls.map((call) => String(call[1]?.body));
    expect(bodies.some((body) => body.includes("mutation_failed"))).toBe(true);
    expect(bodies.some((body) => body.includes("reconcile.failed"))).toBe(true);
  });

  it("renders all concurrent failures without exposing raw backend text", async () => {
    await import("../../src/i18n");
    const { beginThreeStateSync } = await import("../../src/progress/store");
    const { SyncFailureToast } = await import("../../src/components/SyncFailureToast");
    const first = beginThreeStateSync<never>({
      operationId: "session:delete:first",
      action: "Delete session",
      info: "Session remains available.",
      reconcile: vi.fn(),
    });
    vi.mocked(crypto.randomUUID).mockReturnValue("00000000-0000-4000-8000-000000000789");
    const second = beginThreeStateSync<never>({
      operationId: "project:remove:second",
      action: "Remove project",
      reconcile: vi.fn(),
    });
    render(<SyncFailureToast />);

    await act(async () => {
      await first.fail(new Error("Bearer RAW_SECRET"), "x".repeat(10_000));
      await second.fail(new Error("raw backend database failure"), "raw backend database failure");
    });

    expect(screen.getAllByRole("alert")).toHaveLength(2);
    fireEvent.click(screen.getAllByText("Show details")[0]);
    expect(document.body.textContent).not.toContain("RAW_SECRET");
    expect(document.body.textContent).not.toContain("raw backend database failure");
    expect(document.body.textContent).not.toContain("x".repeat(100));
  });
});
