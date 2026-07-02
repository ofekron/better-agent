import { describe, it, expect, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeRun, makeSession, makeUserMsg } from "./fixtures";

vi.setConfig({ testTimeout: 20_000 });

let nextSessionId = 0;

function makeRunsSession(overrides: Parameters<typeof makeSession>[0] = {}) {
  nextSessionId += 1;
  return makeSession({ id: `runs-${nextSessionId}`, ...overrides });
}

function makeRunsSessionWithUser(
  overrides: Parameters<typeof makeSession>[0] = {},
) {
  return makeRunsSession({
    messages: [makeUserMsg({ id: "u", content: "go", seq: 0 })],
    ...overrides,
  });
}

describe("run_state badges (backend-owned run mirroring)", () => {
  it("an unanchored manager run renders 'manager running' under the user bubble", async () => {
    const session = makeRunsSessionWithUser();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "run_state",
      data: {
        app_session_id: session.id,
        runs: [makeRun({ kind: "manager", target_message_id: null })],
      },
    });
    await h.flush();

    const view = await waitFor(() => {
      const snapshot = h.toJSON();
      expect(snapshot.chat.runs).toHaveLength(1);
      return snapshot;
    });
    expect(view.chat.runs).toHaveLength(1);
    expect(view.chat.runs[0]).toMatchObject({ kind: "manager" });
    expect(view.chat.runs[0].label).toContain("manager");
    expect(view.chat.runs[0].label).toContain("running");
    h.unmount();
  });

  it("an unanchored manager run with a missing target renders under the user bubble", async () => {
    const session = makeRunsSessionWithUser();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const { target_message_id: _targetMessageId, ...run } = makeRun({
      kind: "manager",
      target_message_id: null,
    });
    void _targetMessageId;
    h.emit({
      type: "run_state",
      data: {
        app_session_id: session.id,
        runs: [run],
      },
    });
    await h.flush();

    const view = await waitFor(() => {
      const snapshot = h.toJSON();
      expect(snapshot.chat.runs).toHaveLength(1);
      return snapshot;
    });
    expect(view.chat.running).toBe(true);
    expect(view.chat.runs).toHaveLength(1);
    expect(view.chat.runs[0]).toMatchObject({ kind: "manager" });
    expect(view.chat.runs[0].label).toContain("manager");
    expect(view.chat.runs[0].label).toContain("running");
    h.unmount();
  });

  it("an anchored run renders inside its target assistant bubble", async () => {
    const session = makeRunsSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const userMsg = makeUserMsg({ id: "u", content: "go", seq: 0 });
    const assistantMsg = makeAssistantMsg({
      id: "a",
      content: "",
      seq: 1,
      isStreaming: true,
    });
    h.emit({
      type: "messages_replay",
      data: { app_session_id: session.id, messages: [userMsg, assistantMsg] },
    });
    h.emit({
      type: "run_state",
      data: {
        app_session_id: session.id,
        runs: [makeRun({ kind: "manager", target_message_id: "a" })],
      },
    });
    await h.flush();

    await waitFor(() => {
      expect(h.toJSON().chat.running).toBe(true);
      const assistantEl = h.$('[data-testid="assistant-message"][data-message-id="a"]');
      expect(assistantEl?.querySelector(".run-badge")).not.toBeNull();
    });
    h.unmount();
  });

  it("a worker run renders with kind='worker' and the worker description", async () => {
    const session = makeRunsSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    // Seed an assistant with a worker panel so RunBadge can resolve the
    // worker description via workerLabelByDelegation.
    const userMsg = makeUserMsg({ id: "u", content: "delegate", seq: 0 });
    const assistantMsg = makeAssistantMsg({
      id: "a",
      content: "",
      seq: 1,
      isStreaming: true,
      workers: [
        {
          delegation_id: "d1",
          worker_session_id: "w-bc",
          worker_description: "Researcher",
          is_new: false,
          instructions_preview: "",
          events: [],
        },
      ],
    });
    h.emit({
      type: "messages_replay",
      data: { app_session_id: session.id, messages: [userMsg, assistantMsg] },
    });
    h.emit({
      type: "run_state",
      data: {
        app_session_id: session.id,
        runs: [
          makeRun({
            kind: "worker",
            target_message_id: "a",
            delegation_id: "d1",
          }),
        ],
      },
    });
    await h.flush();

    const view = await waitFor(() => {
      const snapshot = h.toJSON();
      expect(snapshot.chat.runs.some((r) => r.kind === "worker")).toBe(true);
      return snapshot;
    });
    const workerBadge = view.chat.runs.find((r) => r.kind === "worker");
    expect(workerBadge?.label).toContain("Researcher");
    h.unmount();
  });

  it("multiple runs (manager + worker) render simultaneously", async () => {
    const session = makeRunsSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "messages_replay",
      data: {
        app_session_id: session.id,
        messages: [
          makeUserMsg({ id: "u", seq: 0 }),
          makeAssistantMsg({ id: "a", seq: 1, isStreaming: true }),
        ],
      },
    });
    h.emit({
      type: "run_state",
      data: {
        app_session_id: session.id,
        runs: [
          makeRun({ run_id: "r1", kind: "manager", target_message_id: "a" }),
          makeRun({
            run_id: "r2",
            kind: "worker",
            target_message_id: "a",
            delegation_id: "d1",
          }),
        ],
      },
    });
    await h.flush();

    const kinds = await waitFor(() => {
      const next = h.toJSON().chat.runs.map((r) => r.kind).sort();
      expect(next).toEqual(["manager", "worker"]);
      return next;
    });
    expect(kinds).toEqual(["manager", "worker"]);
    h.unmount();
  });

  it("run_state for a non-current session does NOT render badges in the current view", async () => {
    const a = makeRunsSession({ id: "runs-a" });
    const b = makeRunsSession({ id: "runs-b", name: "B" });
    const h = await renderApp({ seed: { sessions: [a, b] } });
    await h.selectSession(a.id);

    // Push runs for B while viewing A.
    h.emit({
      type: "run_state",
      data: {
        app_session_id: b.id,
        runs: [makeRun({ target_message_id: null })],
      },
    });
    await h.flush();

    expect(h.toJSON().chat.running).toBe(false);
    h.unmount();
  });

  it("native run kind renders only 'Running...' under the user bubble", async () => {
    const session = makeRunsSessionWithUser({ orchestration_mode: "native" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "run_state",
      data: {
        app_session_id: session.id,
        runs: [makeRun({ kind: "native", target_message_id: null })],
      },
    });
    await h.flush();

    const view = await waitFor(() => {
      const snapshot = h.toJSON();
      expect(snapshot.chat.runs[0]?.kind).toBe("native");
      return snapshot;
    });
    expect(view.chat.runs[0]?.kind).toBe("native");
    expect(view.chat.runs[0]?.label).toBe("Running...");
    h.unmount();
  });

  it("chat running follows session monitoring when run_state detail is absent", async () => {
    const session = makeRunsSessionWithUser({ orchestration_mode: "native" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "session_monitoring_changed",
      data: {
        session_id: session.id,
        monitoring_state: "active",
        cwd: session.cwd,
        node_id: session.node_id ?? "primary",
      },
    });
    await h.flush();

    expect(h.toJSON().chat.running).toBe(true);
    expect(h.$(".running-indicator-inline")).toBeNull();
    expect(h.toJSON().chat.runs).toHaveLength(0);

    h.emit({
      type: "session_monitoring_changed",
      data: {
        session_id: session.id,
        monitoring_state: "stopped",
        cwd: session.cwd,
        node_id: session.node_id ?? "primary",
      },
    });
    await h.flush();

    expect(h.toJSON().chat.running).toBe(false);
    expect(h.$(".running-indicator-inline")).toBeNull();
    h.unmount();
  });

  it("chat run badge disappears when monitoring stops before run_state clears", async () => {
    const session = makeRunsSessionWithUser({ orchestration_mode: "native" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "run_state",
      data: {
        app_session_id: session.id,
        runs: [makeRun({ kind: "native", target_message_id: null })],
      },
    });
    await h.flush();

    await waitFor(() => {
      expect(h.toJSON().chat.running).toBe(true);
      expect(h.toJSON().chat.runs).toHaveLength(1);
    });

    h.emit({
      type: "session_monitoring_changed",
      data: {
        session_id: session.id,
        monitoring_state: "stopped",
        cwd: session.cwd,
        node_id: session.node_id ?? "primary",
      },
    });
    await h.flush();

    await waitFor(() => {
      expect(h.toJSON().chat.running).toBe(false);
      expect(h.toJSON().chat.runs).toHaveLength(0);
    });
    h.unmount();
  });
});
