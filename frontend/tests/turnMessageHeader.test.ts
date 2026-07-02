import { describe, it, expect } from "vitest";
import { turnMessageHeader } from "../src/lib/turnMessageHeader";

describe("turnMessageHeader", () => {
  it("labels a real (source-less) user prompt as User", () => {
    expect(turnMessageHeader(undefined).label).toBe("User");
    expect(turnMessageHeader("").label).toBe("User");
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
      expect(turnMessageHeader(s).label).not.toBe("User");
      expect(turnMessageHeader(s).label.length).toBeGreaterThan(0);
    }
  });

  it("maps known sources to friendly titles", () => {
    expect(turnMessageHeader("team_message").label).toBe("Message");
    expect(turnMessageHeader("team_ask").label).toBe("Ask");
    expect(turnMessageHeader("assistant").label).toBe("Assistant");
  });

  it("groups source families under their base label", () => {
    expect(turnMessageHeader("supervisor.await_user").label).toBe("Supervisor");
    expect(turnMessageHeader("supervisor.verdict_failed").label).toBe("Supervisor");
  });

  it("humanizes an unknown injected source instead of falling back to User", () => {
    expect(turnMessageHeader("custom-bridge_source").label).toBe("Custom Bridge Source");
  });

  it("never emits a blank label for a delimiter-only source", () => {
    const r = turnMessageHeader("___");
    expect(r.label.length).toBeGreaterThan(0);
    expect(r.label).not.toBe("User");
  });
});
