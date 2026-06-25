import { describe, expect, it } from "vitest";
import { Strategy } from "../src/strategies/Strategy";
import { makeAssistantMsg } from "./fixtures";
import type { WSEvent } from "../src/types";

function workerStart(delegationId: string): WSEvent {
  return {
    type: "worker_start",
    data: {
      delegation_id: delegationId,
      worker_session_id: "w1",
      worker_description: "worker",
      is_new: true,
      instructions_preview: "do the thing",
    },
  };
}

function workerEvent(delegationId: string, uuid: string, text: string): WSEvent {
  return {
    type: "worker_event",
    data: {
      delegation_id: delegationId,
      event: {
        type: "agent_message",
        data: {
          type: "assistant",
          uuid,
          message: { role: "assistant", content: [{ type: "text", text }] },
        },
      },
    },
  };
}

function workerEventWithoutUuid(delegationId: string, text: string): WSEvent {
  return {
    type: "worker_event",
    data: {
      delegation_id: delegationId,
      event: {
        type: "agent_message",
        data: {
          type: "assistant",
          message: { role: "assistant", content: [{ type: "text", text }] },
        },
      },
    },
  };
}

describe("Strategy worker_event uuid dedup", () => {
  it("upserts worker events sharing a uuid instead of appending duplicates", () => {
    const strategy = new Strategy("native");
    let msg = makeAssistantMsg({ id: "a1", isStreaming: true, events: [] });

    msg = strategy.applyLiveEvent(msg, workerStart("d1"));
    msg = strategy.applyLiveEvent(msg, workerEvent("d1", "u1", "partial"));
    msg = strategy.applyLiveEvent(msg, workerEvent("d1", "u1", "final"));

    expect(msg.workers).toHaveLength(1);
    expect(msg.workers![0].events).toHaveLength(1);
    const inner = msg.workers![0].events[0].data as {
      message: { content: Array<{ text: string }> };
    };
    expect(inner.message.content[0].text).toBe("final");
  });

  it("appends worker events with distinct uuids", () => {
    const strategy = new Strategy("native");
    let msg = makeAssistantMsg({ id: "a1", isStreaming: true, events: [] });

    msg = strategy.applyLiveEvent(msg, workerStart("d1"));
    msg = strategy.applyLiveEvent(msg, workerEvent("d1", "u1", "one"));
    msg = strategy.applyLiveEvent(msg, workerEvent("d1", "u2", "two"));

    expect(msg.workers![0].events).toHaveLength(2);
  });

  it("appends worker events without uuids", () => {
    const strategy = new Strategy("native");
    let msg = makeAssistantMsg({ id: "a1", isStreaming: true, events: [] });

    msg = strategy.applyLiveEvent(msg, workerStart("d1"));
    msg = strategy.applyLiveEvent(msg, workerEventWithoutUuid("d1", "one"));
    msg = strategy.applyLiveEvent(msg, workerEventWithoutUuid("d1", "two"));

    expect(msg.workers![0].events).toHaveLength(2);
  });
});
