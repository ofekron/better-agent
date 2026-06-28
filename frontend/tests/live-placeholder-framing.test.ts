import { describe, expect, it } from "vitest";
import { applyLiveEventToMessages } from "../src/hooks/useSession";
import { makeAssistantMsg } from "./fixtures";
import type { WSEvent } from "../src/types";

const turnStart: WSEvent = {
  type: "turn_start",
  data: { manager_session_id: null },
};
const turnComplete: WSEvent = {
  type: "turn_complete",
  data: { session_id: "agent-sid-1" },
};
function agentText(uuid: string, text: string): WSEvent {
  return {
    type: "agent_message",
    data: {
      type: "assistant",
      uuid,
      message: {
        model: "claude-opus-4-8",
        role: "assistant",
        content: [{ type: "text", text }],
      },
    },
  };
}

describe("applyLiveEventToMessages — phantom 'No output' placeholder guard", () => {
  it("does NOT spawn a placeholder for a content-less turn_start", () => {
    const next = applyLiveEventToMessages([], turnStart, null, "native", "live-1");
    // No render content => no placeholder => unchanged (same ref).
    expect(next).toEqual([]);
    expect(next.length).toBe(0);
  });

  it("does NOT spawn a placeholder for a content-less turn_complete", () => {
    const next = applyLiveEventToMessages([], turnComplete, null, "native", "live-2");
    expect(next.length).toBe(0);
  });

  it("does NOT spawn two phantom placeholders from a turn_start + turn_complete pair", () => {
    const afterStart = applyLiveEventToMessages([], turnStart, null, "native", "live-1");
    const afterComplete = applyLiveEventToMessages(
      afterStart,
      turnComplete,
      null,
      "native",
      "live-2",
    );
    expect(afterComplete.length).toBe(0);
  });

  it("DOES spawn a placeholder for a render-bearing agent_message", () => {
    const next = applyLiveEventToMessages([], agentText("u1", "hello"), null, "native", "live-3");
    expect(next.length).toBe(1);
    expect(next[0].id).toBe("live-3");
    expect(next[0].isStreaming).toBe(true);
    expect(next[0].events?.length).toBe(1);
  });

  it("DOES append live model switch events to the active assistant", () => {
    const streaming = makeAssistantMsg({ id: "real-model", isStreaming: true, events: [] });
    const event: WSEvent = {
      type: "model_switched",
      data: {
        uuid: "model-switch-live",
        msg_id: "real-model",
        previous_provider_id: "claude",
        previous_model: "sonnet",
        provider_id: "codex",
        model: "gpt-5-codex",
        changed: ["provider_id", "model"],
      },
    };
    const next = applyLiveEventToMessages(
      [streaming],
      event,
      "real-model",
      "native",
      "live-model",
    );
    expect(next).toHaveLength(1);
    expect(next[0].events).toEqual([event]);
  });

  it("DOES spawn a placeholder for a worker_start first-frame (worker panel, no events/content)", () => {
    const workerStart: WSEvent = {
      type: "worker_start",
      data: {
        delegation_id: "d1",
        worker_session_id: null,
        worker_description: "do thing",
        is_new: true,
        instructions_preview: "go",
      },
    };
    const next = applyLiveEventToMessages([], workerStart, null, "manager", "live-w");
    expect(next.length).toBe(1);
    expect(next[0].workers?.length).toBe(1);
    expect(next[0].workers?.[0].delegation_id).toBe("d1");
  });

  it("routes a content-bearing event onto the existing streaming assistant (no new turn)", () => {
    const streaming = makeAssistantMsg({ id: "real-1", isStreaming: true, events: [] });
    const next = applyLiveEventToMessages(
      [streaming],
      agentText("u2", "world"),
      "real-1",
      "native",
      "live-4",
    );
    expect(next.length).toBe(1);
    expect(next[0].id).toBe("real-1");
    expect(next[0].events?.length).toBe(1);
  });

  it("still applies turn_start metadata onto an existing streaming assistant", () => {
    const streaming = makeAssistantMsg({ id: "real-2", isStreaming: true, events: [] });
    const next = applyLiveEventToMessages(
      [streaming],
      { type: "turn_start", data: { manager_session_id: "mgr-9" } },
      "real-2",
      "native",
      "live-5",
    );
    expect(next.length).toBe(1);
    expect(next[0].agent_session_id).toBe("mgr-9");
  });
});
