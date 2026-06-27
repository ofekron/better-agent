import { describe, it, expect } from "vitest";
import { userMessageHeader } from "../src/lib/userMessageHeader";

describe("userMessageHeader", () => {
  it("labels a real (source-less) user prompt as User", () => {
    expect(userMessageHeader(undefined).label).toBe("User");
    expect(userMessageHeader("").label).toBe("User");
  });

  it("never labels an injected (source-bearing) prompt as User", () => {
    const injected = [
      "team_message",
      "team_ask",
      "supervisor",
      "worker",
      "schedule",
      "agent-board",
      "adv_sync",
      "subprocess_agent",
      "assistant",
      "some_future_unknown_source",
    ];
    for (const s of injected) {
      expect(userMessageHeader(s).label).not.toBe("User");
      expect(userMessageHeader(s).label.length).toBeGreaterThan(0);
    }
  });

  it("maps known sources to friendly titles", () => {
    expect(userMessageHeader("team_message").label).toBe("Message");
    expect(userMessageHeader("team_ask").label).toBe("Ask");
    expect(userMessageHeader("assistant").label).toBe("Assistant");
  });

  it("groups source families under their base label", () => {
    expect(userMessageHeader("supervisor.await_user").label).toBe("Supervisor");
    expect(userMessageHeader("supervisor.verdict_failed").label).toBe("Supervisor");
  });

  it("humanizes an unknown injected source instead of falling back to User", () => {
    expect(userMessageHeader("custom-bridge_source").label).toBe("Custom Bridge Source");
  });
});
