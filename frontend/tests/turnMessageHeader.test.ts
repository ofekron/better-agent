import { describe, it, expect } from "vitest";
import { turnMessageHeader } from "../src/lib/turnMessageHeader";

describe("turnMessageHeader", () => {
  it("labels a real source-less prompt with the configured user label", () => {
    expect(turnMessageHeader(undefined, "ofek").label).toBe("ofek");
    expect(turnMessageHeader("", "Ofek Ron").label).toBe("Ofek Ron");
    expect(turnMessageHeader(undefined, "   ").label).toBe("User");
  });

  it("never labels an injected (source-bearing) prompt as User", () => {
    const injected = [
      "mssg",
      "team_ask",
      "supervisor",
      "worker",
      "schedule",
      "agent-board",
      "adv_sync",
      "subprocess_agent",
      "assistant",
      "file_editor",
      "operator",
      "some_future_unknown_source",
    ];
    for (const s of injected) {
      expect(turnMessageHeader(s).label).not.toBe("User");
      expect(turnMessageHeader(s).label.length).toBeGreaterThan(0);
    }
  });

  it("maps known sources to friendly titles", () => {
    expect(turnMessageHeader("mssg").label).toBe("Message");
    expect(turnMessageHeader("team_ask").label).toBe("Ask");
    expect(turnMessageHeader("assistant").label).toBe("Assistant");
    expect(turnMessageHeader("file_editor").label).toBe("Operator");
    expect(turnMessageHeader("operator").label).toBe("Operator");
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
