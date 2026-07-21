import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const app = readFileSync("src/App.tsx", "utf8");
const chat = readFileSync("src/components/Chat.tsx", "utf8");
const inputArea = readFileSync("src/components/InputArea.tsx", "utf8");
const component = readFileSync("src/components/SessionSelectorControls.tsx", "utf8");
const modal = readFileSync("src/components/ModelPickerModal.tsx", "utf8");

describe("existing-session provider/model selectors", () => {
  it("renders the per-session selector controls in the prompt overflow menu", () => {
    expect(app).toContain("SessionSelectorControls");
    expect(app).toContain("composerOverflowNode");
    expect(chat).toContain("overflowPanelNode={composerOverflowNode}");
    expect(inputArea).toContain("input-overflow-panel");
    expect(app).toContain("applySessionMetadata(currentSession.id, updates)");
  });

  it("persists provider/model changes through the session selectors endpoint on OK", () => {
    expect(component).toContain("/api/sessions/${encodeURIComponent(session.id)}/selectors");
    expect(component).toContain("openPicker");
    expect(component).toContain("onConfirm={(updates) => void save(updates)}");
    expect(modal).toContain("changedUpdates(session, draft)");
    expect(modal).toContain("draftProvider.runner_options.length > 1");
    expect(component).toContain("runner: session.runner");
    expect(modal).toContain('t("newSession.cancel", "Cancel")');
    expect(modal).toContain('t("common.ok", "OK")');
    expect(modal).not.toContain("onChange={(e) => changeModel(e.target.value)}");
  });

  it("documents lazy continuation semantics for selector changes", () => {
    expect(component).toContain("fresh provider subprocess");
  });
});
