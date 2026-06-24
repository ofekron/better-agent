import { describe, expect, it } from "vitest";
import { dedupeWorkerPanels, groupByEntity, isCreationPanelKind, panelKindLabel, tagEvents } from "../src/utils/mergeEvents";
import { Strategy } from "../src/strategies/Strategy";
import type { ChatMessage, WSEvent, WorkerPanel } from "../src/types";

const event = (uuid: string): WSEvent => ({
  type: "agent_message",
  data: { uuid },
});

const baseWorker = (
  over: Partial<WorkerPanel> & { delegation_id: string },
): WorkerPanel => ({
  worker_session_id: "session",
  worker_description: over.delegation_id,
  panel_kind: "worker",
  is_new: false,
  instructions_preview: "",
  events: [],
  ...over,
});

describe("worker panel timeline", () => {
  it("native strategy interleaves sub-session panels by insert_at", () => {
    const message = {
      id: "assistant",
      role: "assistant",
      events: [
        event("manager-before"),
        event("manager-between"),
        event("manager-after"),
      ],
    } as ChatMessage;
    const workers: WorkerPanel[] = [
      baseWorker({
        delegation_id: "sub-a",
        panel_kind: "sub_session",
        insert_at: 1,
        events: [event("sub-a-event")],
      }),
      baseWorker({
        delegation_id: "sub-b",
        panel_kind: "sub_session",
        insert_at: 2,
        events: [event("sub-b-event")],
      }),
    ];

    const blocks = new Strategy("native").buildEntityBlocks(message, workers);

    expect(blocks?.map((b) => b.entityId)).toEqual([
      "manager",
      "sub-a",
      "manager",
      "sub-b",
      "manager",
    ]);
    expect(blocks?.map((b) => b.events.map((e) => e.data?.uuid))).toEqual([
      ["manager-before"],
      ["sub-a-event"],
      ["manager-between"],
      ["sub-b-event"],
      ["manager-after"],
    ]);
  });

  it("native strategy appends legacy sub-session panels without insert_at", () => {
    const message = {
      id: "assistant",
      role: "assistant",
      events: [event("manager-before"), event("manager-after")],
    } as ChatMessage;
    const workers: WorkerPanel[] = [
      baseWorker({
        delegation_id: "legacy-sub",
        panel_kind: "sub_session",
        events: [event("legacy-event")],
      }),
    ];

    const blocks = new Strategy("native").buildEntityBlocks(message, workers);

    expect(blocks?.map((b) => b.entityId)).toEqual(["manager", "legacy-sub"]);
    expect(blocks?.map((b) => b.events.map((e) => e.data?.uuid))).toEqual([
      ["manager-before", "manager-after"],
      ["legacy-event"],
    ]);
  });

  it("keeps creation and later sub-session turn panels separate", () => {
    const message = {
      id: "assistant",
      role: "assistant",
      events: [event("manager-before"), event("manager-after")],
    } as ChatMessage;
    const workers: WorkerPanel[] = [
      baseWorker({
        delegation_id: "created-sub",
        worker_description: "Review session created",
        panel_kind: "sub_session_created",
        insert_at: 1,
      }),
      baseWorker({
        delegation_id: "team_message-sub",
        worker_description: "Review session",
        panel_kind: "sub_session",
        insert_at: 2,
        events: [event("sub-turn-event")],
      }),
    ];

    const blocks = new Strategy("native").buildEntityBlocks(message, workers);

    expect(blocks?.map((block) => block.entityId)).toEqual([
      "manager",
      "created-sub",
      "manager",
      "team_message-sub",
    ]);
    expect(blocks?.[1].panelKind).toBe("sub_session_created");
    expect(blocks?.[3].panelKind).toBe("sub_session");
  });

  it("interleaves panels at their delegation point (insert_at)", () => {
    const managerEvents = [
      event("manager-before"),
      event("manager-between"),
      event("manager-after"),
    ];
    const workers: WorkerPanel[] = [
      baseWorker({
        delegation_id: "first",
        insert_at: 1,
        events: [event("first-a"), event("first-b")],
      }),
      baseWorker({
        delegation_id: "second",
        insert_at: 2,
        events: [event("second-a")],
      }),
    ];

    const blocks = groupByEntity(tagEvents(managerEvents, workers));

    expect(blocks.map((b) => b.entityId)).toEqual([
      "manager",
      "first",
      "manager",
      "second",
      "manager",
    ]);
    expect(blocks.map((b) => b.events.map((e) => e.data?.uuid))).toEqual([
      ["manager-before"],
      ["first-a", "first-b"],
      ["manager-between"],
      ["second-a"],
      ["manager-after"],
    ]);
  });

  it("renders a panel after the visible trigger counted by insert_at", () => {
    const managerEvents = [
      event("manager-before"),
      event("visible-trigger"),
      event("manager-after"),
    ];
    const workers: WorkerPanel[] = [
      baseWorker({
        delegation_id: "triggered-panel",
        insert_at: 2,
        events: [event("worker-event")],
      }),
    ];

    const blocks = groupByEntity(tagEvents(managerEvents, workers));

    expect(blocks.map((b) => b.entityId)).toEqual([
      "manager",
      "triggered-panel",
      "manager",
    ]);
    expect(blocks[0].events.map((e) => e.data?.uuid)).toEqual([
      "manager-before",
      "visible-trigger",
    ]);
    expect(blocks[1].events.map((e) => e.data?.uuid)).toEqual(["worker-event"]);
    expect(blocks[2].events.map((e) => e.data?.uuid)).toEqual(["manager-after"]);
  });

  it("regression: a panel with insert_at never sticks to the bottom", () => {
    // Pre-fix, tagEvents positioned panels by string-comparing started_at
    // against manager-event timestamps. With no started_at and timestamp-less
    // manager events the panel was pushed after every manager event (the
    // bug). insert_at must place it inline regardless of timestamps.
    const managerEvents = [
      event("m1"), event("m2"), event("m3"), event("m4"), event("m5"),
    ];
    const workers: WorkerPanel[] = [
      baseWorker({ delegation_id: "w", insert_at: 2, events: [event("w-a")] }),
    ];

    const blocks = groupByEntity(tagEvents(managerEvents, workers));

    expect(blocks.map((b) => b.entityId)).toEqual(["manager", "w", "manager"]);
    expect(blocks[0].events.map((e) => e.data?.uuid)).toEqual(["m1", "m2"]);
    expect(blocks[1].events.map((e) => e.data?.uuid)).toEqual(["w-a"]);
    expect(blocks[2].events.map((e) => e.data?.uuid)).toEqual(["m3", "m4", "m5"]);
  });

  it("anchors a panel at insert_at before its first event arrives", () => {
    const managerEvents = [event("manager-before"), event("manager-after")];
    const workers: WorkerPanel[] = [
      baseWorker({
        delegation_id: "sub-session",
        panel_kind: "sub_session",
        started_at: "2026-06-17T10:01:00",
        insert_at: 1,
        events: [],
      }),
    ];

    const blocks = groupByEntity(tagEvents(managerEvents, workers));

    expect(blocks.map((b) => b.entityId)).toEqual([
      "manager",
      "sub-session",
      "manager",
    ]);
    expect(blocks[1]).toMatchObject({
      entityType: "worker",
      panelKind: "sub_session",
      startedAt: "2026-06-17T10:01:00",
    });
    expect(blocks[1].events).toEqual([
      { type: "worker_start", data: { timestamp: "2026-06-17T10:01:00" } },
    ]);
  });

  it("keeps panels at the same delegation point in creation order", () => {
    const managerEvents = [event("m1"), event("m2")];
    const workers: WorkerPanel[] = [
      baseWorker({ delegation_id: "a", insert_at: 1, events: [event("a1")] }),
      baseWorker({ delegation_id: "b", insert_at: 1, events: [event("b1")] }),
    ];

    const blocks = groupByEntity(tagEvents(managerEvents, workers));

    expect(blocks.map((b) => b.entityId)).toEqual(["manager", "a", "b", "manager"]);
  });

  it("clamps insert_at past the end of the manager stream", () => {
    // insert_at is a snapshot count and may exceed the events the frontend
    // currently holds (stale reload); the panel must still land at the end
    // of the known stream, not past it.
    const managerEvents = [event("m1"), event("m2")];
    const workers: WorkerPanel[] = [
      baseWorker({ delegation_id: "w", insert_at: 99, events: [event("w-a")] }),
    ];

    const blocks = groupByEntity(tagEvents(managerEvents, workers));

    expect(blocks.map((b) => b.entityId)).toEqual(["manager", "w"]);
  });

  it("legacy panels without insert_at append after the manager stream", () => {
    const managerEvents = [event("m1"), event("m2")];
    const workers: WorkerPanel[] = [
      baseWorker({ delegation_id: "legacy", events: [event("l1")] }),
    ];

    const blocks = groupByEntity(tagEvents(managerEvents, workers));

    expect(blocks.map((b) => b.entityId)).toEqual(["manager", "legacy"]);
  });

  it("dedupes duplicate panel ids keeping first occurrence", () => {
    const workers: WorkerPanel[] = [
      baseWorker({ delegation_id: "same", worker_description: "Original", panel_kind: "sub_session" }),
      baseWorker({ delegation_id: "other", worker_description: "Other", panel_kind: "worker" }),
      baseWorker({ delegation_id: "same", worker_description: "Duplicate", panel_kind: "sub_session" }),
    ];

    expect(dedupeWorkerPanels(workers).map((w) => w.delegation_id)).toEqual([
      "same",
      "other",
    ]);
  });

  it("panelKindLabel maps kinds", () => {
    expect(panelKindLabel("sub_session_created")).toBe("Sub Session Created");
    expect(panelKindLabel("session_created")).toBe("Session Created");
    expect(panelKindLabel("sub_session")).toBe("Sub Session");
    expect(panelKindLabel("session")).toBe("Session");
    expect(panelKindLabel("worker")).toBe("Worker");
    expect(panelKindLabel(undefined)).toBe("Worker");
  });

  it("identifies creation-only panel kinds", () => {
    expect(isCreationPanelKind("sub_session_created")).toBe(true);
    expect(isCreationPanelKind("session_created")).toBe(true);
    expect(isCreationPanelKind("sub_session")).toBe(false);
    expect(isCreationPanelKind("session")).toBe(false);
    expect(isCreationPanelKind("worker")).toBe(false);
    expect(isCreationPanelKind(undefined)).toBe(false);
  });
});
