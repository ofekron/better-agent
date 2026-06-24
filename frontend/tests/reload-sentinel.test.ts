import { describe, it, expect, beforeEach } from "vitest";
import { cleanupRestoredModalSentinel } from "../src/hooks/useBackButtonDismiss";

beforeEach(() => {
  window.history.replaceState(null, "", "/test-home");
});

describe("cleanupRestoredModalSentinel", () => {
  it("wipes a phantom __modalId sentinel left by a browser-restored reload", () => {
    window.history.replaceState({ __modalId: 99, __prev: null }, "", "/test-home");
    cleanupRestoredModalSentinel();
    expect(window.history.state).toBeNull();
  });

  it("restores the carried __prev so any future state consumer sees the pre-modal value", () => {
    window.history.replaceState(
      { __modalId: 7, __prev: { route: "session", id: "abc" } },
      "",
      "/test-home",
    );
    cleanupRestoredModalSentinel();
    expect(window.history.state).toEqual({ route: "session", id: "abc" });
  });

  it("leaves non-sentinel state untouched", () => {
    window.history.replaceState({ unrelated: true }, "", "/test-home");
    cleanupRestoredModalSentinel();
    expect(window.history.state).toEqual({ unrelated: true });
  });

  it("no-op when state is null", () => {
    window.history.replaceState(null, "", "/test-home");
    cleanupRestoredModalSentinel();
    expect(window.history.state).toBeNull();
  });

  it("clears a leaked __cancelInFlight absorber", () => {
    window.history.replaceState({ __cancelInFlight: true }, "", "/test-home");
    cleanupRestoredModalSentinel();
    expect(window.history.state).toBeNull();
  });
});
