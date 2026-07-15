import { describe, it, expect } from "vitest";
import { runMetaMarkedMessageIds } from "src/chat/runMetaMarkers";

/**
 * Spec (chat-panel.md, "Turn identity and state"): the provider/model/effort
 * marker renders once per contiguous provider/model/effort run — on the
 * run's LAST visible assistant message — plus always on the last visible
 * message of the panel. Messages inside a run must not repeat the chips.
 * Placement is decided by src/chat/decorateModelRuns; Chat.tsx feeds the
 * per-turn assistant messages through runMetaMarkedMessageIds and gates
 * AssistantRunMeta on membership.
 */
describe("provider/model/effort run markers", () => {
  const claude = { provider_id: "claude", model: "sonnet", reasoning_effort: "high" };
  const codex = { provider_id: "codex", model: "gpt-5-codex", reasoning_effort: "medium" };

  it("marks only the last message of each contiguous run", () => {
    const marked = runMetaMarkedMessageIds(
      [
        { responseMessage: { id: "a1", run_meta: claude }, isLatest: false },
        { responseMessage: { id: "a2", run_meta: claude }, isLatest: false },
        { responseMessage: { id: "a3", run_meta: codex }, isLatest: true },
      ],
      null,
    );
    expect(marked).toEqual(new Set(["a2", "a3"]));
  });

  it("always marks the panel's last visible assistant message", () => {
    const marked = runMetaMarkedMessageIds(
      [{ responseMessage: { id: "a1", run_meta: claude }, isLatest: true }],
      null,
    );
    expect(marked).toEqual(new Set(["a1"]));
  });

  it("falls back to session settings only for the latest turn", () => {
    const session = { provider_id: "claude", model: "sonnet", reasoning_effort: "high" };
    const marked = runMetaMarkedMessageIds(
      [
        { responseMessage: { id: "a1", run_meta: claude }, isLatest: false },
        // Latest turn predates run_meta — session fallback makes it the
        // same run as a1, so only the latest carries the marker.
        { responseMessage: { id: "a2" }, isLatest: true },
      ],
      session,
    );
    expect(marked).toEqual(new Set(["a2"]));
  });

  it("skips turns without an assistant message", () => {
    const marked = runMetaMarkedMessageIds(
      [
        { responseMessage: null, isLatest: false },
        { responseMessage: { id: "a1", run_meta: codex }, isLatest: true },
      ],
      null,
    );
    expect(marked).toEqual(new Set(["a1"]));
  });
});
