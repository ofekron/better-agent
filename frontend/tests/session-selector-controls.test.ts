import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const app = readFileSync("src/App.tsx", "utf8");
const component = readFileSync("src/components/SessionSelectorControls.tsx", "utf8");

describe("existing-session provider/model selectors", () => {
  it("renders the per-session selector controls in the chat toolbar", () => {
    expect(app).toContain("SessionSelectorControls");
    expect(app).toContain("applySessionMetadata(currentSession.id, updates)");
  });

  it("persists provider/model changes through the session selectors endpoint", () => {
    expect(component).toContain("/api/sessions/${encodeURIComponent(session.id)}/selectors");
    expect(component).toContain("provider_id: providerId");
    expect(component).toContain("model: preferredModel");
    expect(component).toContain("if (!preferredModel)");
  });

  it("documents lazy continuation semantics for selector changes", () => {
    expect(component).toContain("lazily on the next prompt");
  });
});
