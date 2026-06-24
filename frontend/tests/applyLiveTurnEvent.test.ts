import { describe, expect, it } from "vitest";
import { applyLiveTurnEvent } from "../src/utils/applyLiveTurnEvent";
import { makeAssistantMsg } from "./fixtures";
import type { WSEvent } from "../src/types";

function syntheticMarker(): WSEvent {
  return {
    type: "manager_event",
    data: {
      event: {
        type: "agent_message",
        data: {
          type: "assistant",
          uuid: "synthetic-1",
          message: {
            model: "<synthetic>",
            role: "assistant",
            content: [{ type: "text", text: "No response requested." }],
          },
        },
      },
    },
  };
}

describe("applyLiveTurnEvent", () => {
  it("drops wrapped synthetic no-response assistant markers", () => {
    const msg = makeAssistantMsg({ id: "a1", isStreaming: true, events: [] });

    const next = applyLiveTurnEvent(msg, syntheticMarker(), "team");

    expect(next).toBe(msg);
    expect(next.events).toEqual([]);
  });

  it("keeps real wrapped assistant events", () => {
    const msg = makeAssistantMsg({ id: "a1", isStreaming: true, events: [] });
    const real: WSEvent = {
      type: "manager_event",
      data: {
        event: {
          type: "agent_message",
          data: {
            type: "assistant",
            uuid: "real-1",
            message: {
              model: "claude-sonnet-4-6",
              role: "assistant",
              content: [{ type: "text", text: "Actual response" }],
            },
          },
        },
      },
    };

    const next = applyLiveTurnEvent(msg, real, "team");

    expect(next).not.toBe(msg);
    expect(next.events).toEqual([(real.data as { event: WSEvent }).event]);
  });
});
