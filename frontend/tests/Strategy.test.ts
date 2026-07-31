import { describe, expect, it } from "vitest";
import { Strategy } from "../src/strategies/Strategy";
import type { ChatMessage, WSEvent, WorkerPanel } from "../src/types";

function msg(partial: Partial<ChatMessage> = {}): ChatMessage {
  return { events: [], agent_session_id: null, ...partial } as ChatMessage;
}

function panel(delegationId: string, overrides: Partial<WorkerPanel> = {}): WorkerPanel {
  return {
    delegation_id: delegationId,
    worker_session_id: `ws-${delegationId}`,
    worker_description: "desc",
    panel_kind: undefined,
    started_at: undefined,
    insert_at: undefined,
    is_new: true,
    instructions_preview: "preview",
    orchestration_mode: undefined,
    provider_id: undefined,
    model: undefined,
    reasoning_effort: undefined,
    run_mode: undefined,
    events: [],
    ...overrides,
  };
}

function workerStartData(delegationId: string, overrides: Record<string, unknown> = {}) {
  return {
    delegation_id: delegationId,
    worker_session_id: `ws-${delegationId}`,
    worker_description: "desc",
    is_new: true,
    instructions_preview: "preview",
    ...overrides,
  };
}

describe("Strategy — mode plumbing", () => {
  it("hasScopeWrapper is true only in team mode", () => {
    expect(new Strategy("team").hasScopeWrapper(msg())).toBe(true);
    expect(new Strategy("native").hasScopeWrapper(msg())).toBe(false);
  });

  it("getEvents returns message.events or []", () => {
    const ev = { type: "agent_message", data: { uuid: "u1" } } as WSEvent;
    expect(new Strategy("native").getEvents(msg({ events: [ev] }))).toEqual([ev]);
    expect(new Strategy("native").getEvents(msg({ events: undefined } as unknown as ChatMessage))).toEqual([]);
  });
});

describe("Strategy — buildEntityBlocks", () => {
  it("returns undefined when there are no events and no worker panels", () => {
    expect(new Strategy("team").buildEntityBlocks(msg(), [])).toBeUndefined();
  });

  it("returns undefined in non-team mode when there are events but no panels", () => {
    const ev = { type: "agent_message", data: { uuid: "u1" } } as WSEvent;
    expect(new Strategy("native").buildEntityBlocks(msg({ events: [ev] }), [])).toBeUndefined();
  });

  it("groups events into entity blocks in team mode", () => {
    const ev = { type: "agent_message", data: { uuid: "u1" } } as WSEvent;
    const blocks = new Strategy("team").buildEntityBlocks(msg({ events: [ev] }), []);
    expect(blocks).toBeDefined();
    expect(blocks!.length).toBeGreaterThan(0);
  });

  it("dedupes worker panels and groups in non-team mode when panels exist", () => {
    const p = panel("d1");
    const blocks = new Strategy("native").buildEntityBlocks(msg(), [p, { ...p }]);
    expect(blocks).toBeDefined();
  });
});

describe("Strategy — applyLiveEvent envelope unwrap", () => {
  it("recurses into a typed agent_message envelope wrapping a worker_start", () => {
    const wrapped: WSEvent = {
      type: "agent_message",
      data: { type: "worker_start", data: workerStartData("d-env") },
    };
    const out = new Strategy("team").applyLiveEvent(msg(), wrapped);
    expect(out.workers?.[0].delegation_id).toBe("d-env");
  });
});

describe("Strategy — applyLiveEvent turn framing", () => {
  it("turn_start sets agent_session_id from manager_session_id", () => {
    const out = new Strategy("native").applyLiveEvent(
      msg({ agent_session_id: "old" }),
      { type: "turn_start", data: { manager_session_id: "m1" } } as WSEvent,
    );
    expect(out.agent_session_id).toBe("m1");
  });

  it("turn_start falls back to null when manager_session_id is absent", () => {
    const out = new Strategy("native").applyLiveEvent(
      msg({ agent_session_id: "old" }),
      { type: "turn_start", data: {} } as WSEvent,
    );
    expect(out.agent_session_id).toBeNull();
  });

  it("turn_complete sets agent_session_id when session_id is present", () => {
    const out = new Strategy("native").applyLiveEvent(
      msg(),
      { type: "turn_complete", data: { session_id: "s-done" } } as WSEvent,
    );
    expect(out.agent_session_id).toBe("s-done");
  });

  it("turn_complete is a no-op when session_id is absent", () => {
    const before = msg({ agent_session_id: "keep" });
    const out = new Strategy("native").applyLiveEvent(
      before,
      { type: "turn_complete", data: {} } as WSEvent,
    );
    expect(out).toBe(before);
  });
});

describe("Strategy — applyLiveEvent message events", () => {
  it("appends an agent_message with a new uuid", () => {
    const out = new Strategy("native").applyLiveEvent(
      msg(),
      { type: "agent_message", data: { uuid: "u1", text: "a" } } as WSEvent,
    );
    expect(out.events).toHaveLength(1);
    expect(out.events[0].data).toMatchObject({ uuid: "u1", text: "a" });
  });

  it("replaces an agent_message with an existing uuid in place", () => {
    const base = msg({ events: [
      { type: "agent_message", data: { uuid: "u1", text: "old" } } as WSEvent,
      { type: "agent_message", data: { uuid: "u2", text: "keep" } } as WSEvent,
    ] });
    const out = new Strategy("native").applyLiveEvent(
      base,
      { type: "agent_message", data: { uuid: "u1", text: "new" } } as WSEvent,
    );
    expect(out.events).toHaveLength(2);
    expect(out.events[0].data).toMatchObject({ uuid: "u1", text: "new" });
    expect(out.events[1].data).toMatchObject({ uuid: "u2" });
  });

  it("appends an agent_message with no uuid", () => {
    const out = new Strategy("native").applyLiveEvent(
      msg({ events: [{ type: "agent_message", data: { uuid: "u1" } } as WSEvent] }),
      { type: "agent_message", data: { text: "nouuid" } } as WSEvent,
    );
    expect(out.events).toHaveLength(2);
  });

  it("model_switched and steer_prompt append like canonical payloads", () => {
    for (const t of ["model_switched", "steer_prompt"] as const) {
      const out = new Strategy("native").applyLiveEvent(
        msg(),
        { type: t, data: { uuid: "x" } } as WSEvent,
      );
      expect(out.events[0].type).toBe(t);
    }
  });

  it("manager_event unwraps and appends its inner event", () => {
    const inner = { type: "agent_message", data: { uuid: "inner" } } as WSEvent;
    const out = new Strategy("native").applyLiveEvent(
      msg(),
      { type: "manager_event", data: { event: inner } } as WSEvent,
    );
    expect(out.events[0]).toEqual(inner);
  });

  it("manager_event with no inner event is a no-op", () => {
    const before = msg();
    const out = new Strategy("native").applyLiveEvent(
      before,
      { type: "manager_event", data: {} } as WSEvent,
    );
    expect(out).toBe(before);
  });

  it("todos_snapshot appends a normalized inner event", () => {
    const out = new Strategy("native").applyLiveEvent(
      msg(),
      { type: "todos_snapshot", data: { items: [] } } as WSEvent,
    );
    expect(out.events).toHaveLength(1);
    expect(out.events[0]).toEqual({ type: "todos_snapshot", data: { items: [] } });
  });
});

describe("Strategy — applyLiveEvent worker_prep events", () => {
  it.each(["worker_prep_start", "worker_prep_event", "worker_prep_complete", "worker_prep_cancelled"] as const)(
    "%s appends to events",
    (t) => {
      const out = new Strategy("team").applyLiveEvent(
        msg(),
        { type: t, data: { delegation_id: "d1" } } as WSEvent,
      );
      expect(out.events[0].type).toBe(t);
    },
  );
});

describe("Strategy — applyLiveEvent worker_start", () => {
  it("creates a new worker panel from a worker_start event", () => {
    const out = new Strategy("team").applyLiveEvent(
      msg(),
      { type: "worker_start", data: workerStartData("d1", {
        panel_kind: "task", started_at: "t", insert_at: 3, orchestration_mode: "team",
        provider_id: "claude", model: "m", reasoning_effort: "high", run_mode: "direct",
      }) } as WSEvent,
    );
    expect(out.workers).toHaveLength(1);
    const p = out.workers![0];
    expect(p).toMatchObject({
      delegation_id: "d1", worker_session_id: "ws-d1", panel_kind: "task", started_at: "t",
      insert_at: 3, is_new: true, orchestration_mode: "team", provider_id: "claude",
      model: "m", reasoning_effort: "high", run_mode: "direct", events: [],
    });
  });

  it("ignores a duplicate worker_start for an existing delegation_id", () => {
    const before = msg({ workers: [panel("d1")] });
    const out = new Strategy("team").applyLiveEvent(
      before,
      { type: "worker_start", data: workerStartData("d1") } as WSEvent,
    );
    expect(out).toBe(before);
  });
});

describe("Strategy — applyLiveEvent worker_event", () => {
  it("is a no-op when the delegation is unknown", () => {
    const before = msg({ workers: [panel("d1")] });
    const out = new Strategy("team").applyLiveEvent(
      before,
      { type: "worker_event", data: { delegation_id: "missing", event: { type: "agent_message", data: {} } as WSEvent } } as WSEvent,
    );
    expect(out).toBe(before);
  });

  it("is a no-op when no inner event is present", () => {
    const before = msg({ workers: [panel("d1")] });
    const out = new Strategy("team").applyLiveEvent(
      before,
      { type: "worker_event", data: { delegation_id: "d1" } } as WSEvent,
    );
    expect(out).toBe(before);
  });

  it("appends a worker event with a new uuid", () => {
    const before = msg({ workers: [panel("d1")] });
    const out = new Strategy("team").applyLiveEvent(
      before,
      { type: "worker_event", data: { delegation_id: "d1", event: { type: "agent_message", data: { uuid: "we1" } } as WSEvent } } as WSEvent,
    );
    expect(out.workers![0].events).toHaveLength(1);
    expect(out.workers![0].events[0].data).toMatchObject({ uuid: "we1" });
  });

  it("replaces a worker event with an existing uuid in place", () => {
    const before = msg({ workers: [panel("d1", { events: [
      { type: "agent_message", data: { uuid: "we1", text: "old" } } as WSEvent,
    ] })] });
    const out = new Strategy("team").applyLiveEvent(
      before,
      { type: "worker_event", data: { delegation_id: "d1", event: { type: "agent_message", data: { uuid: "we1", text: "new" } } as WSEvent } } as WSEvent,
    );
    expect(out.workers![0].events).toHaveLength(1);
    expect(out.workers![0].events[0].data).toMatchObject({ uuid: "we1", text: "new" });
  });

  it("appends a worker event with no uuid", () => {
    const before = msg({ workers: [panel("d1", { events: [
      { type: "agent_message", data: { uuid: "we1" } } as WSEvent,
    ] })] });
    const out = new Strategy("team").applyLiveEvent(
      before,
      { type: "worker_event", data: { delegation_id: "d1", event: { type: "agent_message", data: { text: "x" } } as WSEvent } } as WSEvent,
    );
    expect(out.workers![0].events).toHaveLength(2);
  });
});

describe("Strategy — applyLiveEvent worker_complete", () => {
  it("is a no-op when the delegation is unknown", () => {
    const before = msg({ workers: [panel("d1")] });
    const out = new Strategy("team").applyLiveEvent(
      before,
      { type: "worker_complete", data: { delegation_id: "missing" } } as WSEvent,
    );
    expect(out).toBe(before);
  });

  it("merges completion fields onto the panel", () => {
    const before = msg({ workers: [panel("d1")] });
    const out = new Strategy("team").applyLiveEvent(
      before,
      { type: "worker_complete", data: {
        delegation_id: "d1",
        worker_session_id: "ws-final",
        jsonl_path: "/p/log.jsonl",
        new_byte_offset: 4096,
        token_usage: { input: 10 },
        success: true,
        error: null,
        fork_agent_sid: "fork-1",
        run_mode: "direct",
      } } as WSEvent,
    );
    const p = out.workers![0];
    expect(p).toMatchObject({
      worker_session_id: "ws-final",
      jsonl_path: "/p/log.jsonl",
      new_byte_offset: 4096,
      token_usage: { input: 10 },
      success: true,
      error: null,
      fork_agent_sid: "fork-1",
      run_mode: "direct",
    });
  });

  it("defaults jsonl_path and error to null when absent", () => {
    const before = msg({ workers: [panel("d1")] });
    const out = new Strategy("team").applyLiveEvent(
      before,
      { type: "worker_complete", data: { delegation_id: "d1" } } as WSEvent,
    );
    expect(out.workers![0].jsonl_path).toBeNull();
    expect(out.workers![0].error).toBeNull();
  });
});

describe("Strategy — optional-collection coalescing", () => {
  // ChatMessage.events / .workers and WorkerPanel.events are optional; the
  // reducer coalesces each to [] (and worker_session_id to "") before use.
  const noCollections = { events: undefined, workers: undefined } as unknown as ChatMessage;

  it("buildEntityBlocks tolerates undefined events and workers", () => {
    const team = new Strategy("team");
    expect(team.buildEntityBlocks(noCollections, undefined)).toBeUndefined();
    expect(team.buildEntityBlocks({ ...noCollections } as ChatMessage, [panel("d1")])).toBeDefined();
    // events present, workers undefined — exercises the workers ?? [] coalesce.
    const withEvents = { events: [{ type: "agent_message", data: { uuid: "u1" } } as WSEvent], workers: undefined } as unknown as ChatMessage;
    expect(team.buildEntityBlocks(withEvents, undefined)).toBeDefined();
  });

  it("agent_message / todos_snapshot / worker_prep append against undefined events", () => {
    const s = new Strategy("native");
    expect(s.applyLiveEvent(noCollections, { type: "agent_message", data: { uuid: "u1" } } as WSEvent).events).toHaveLength(1);
    expect(s.applyLiveEvent(noCollections, { type: "todos_snapshot", data: {} } as WSEvent).events).toHaveLength(1);
    expect(s.applyLiveEvent(noCollections, { type: "worker_prep_start", data: {} } as WSEvent).events).toHaveLength(1);
  });

  it("worker_start coalesces a missing worker_session_id to empty string", () => {
    const out = new Strategy("team").applyLiveEvent(
      noCollections,
      { type: "worker_start", data: workerStartData("d1", { worker_session_id: undefined }) } as WSEvent,
    );
    expect(out.workers![0].worker_session_id).toBe("");
  });

  it("worker_event coalesces undefined workers and undefined panel events", () => {
    const s = new Strategy("team");
    // message.workers undefined → findIndex on [] → no-op (covers workers ?? []).
    expect(s.applyLiveEvent(noCollections, { type: "worker_event", data: { delegation_id: "d1", event: { type: "agent_message", data: {} } as WSEvent } } as WSEvent)).toBe(noCollections);
    // panel exists but panel.events undefined → events ?? [] coalesce, then append.
    const withPanelNoEvents = { events: [], workers: [panel("d1", { events: undefined })] } as unknown as ChatMessage;
    const out = s.applyLiveEvent(withPanelNoEvents, { type: "worker_event", data: { delegation_id: "d1", event: { type: "agent_message", data: { uuid: "we1" } } as WSEvent } } as WSEvent);
    expect(out.workers![0].events).toHaveLength(1);
  });

  it("worker_complete coalesces undefined workers", () => {
    const s = new Strategy("team");
    expect(s.applyLiveEvent(noCollections, { type: "worker_complete", data: { delegation_id: "d1" } } as WSEvent)).toBe(noCollections);
  });
});

describe("Strategy — applyLiveEvent unknown event", () => {
  it("returns the message unchanged for an unrecognized event type", () => {
    const before = msg({ agent_session_id: "keep" });
    const out = new Strategy("native").applyLiveEvent(
      before,
      { type: "file_history_snapshot", data: {} } as WSEvent,
    );
    expect(out).toBe(before);
  });
});
