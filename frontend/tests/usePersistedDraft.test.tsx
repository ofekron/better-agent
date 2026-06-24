import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { usePersistedDraft } from "../src/hooks/usePersistedDraft";

describe("usePersistedDraft", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  it("auto-saves the draft to localStorage so an unmount does not lose it", () => {
    // Regression: typed comment text was held only in component state and
    // vanished on unmount / file switch.
    const { result, unmount } = renderHook(() => usePersistedDraft("file:a"));
    act(() => result.current[1]("half-written comment"));
    expect(localStorage.getItem("file:a")).toBe("half-written comment");

    unmount();
    const { result: remounted } = renderHook(() =>
      usePersistedDraft("file:a"),
    );
    expect(remounted.current[0]).toBe("half-written comment");
  });

  it("re-hydrates from the new key when the key changes", () => {
    localStorage.setItem("file:b", "draft for B");
    const { result, rerender } = renderHook(({ k }) => usePersistedDraft(k), {
      initialProps: { k: "file:a" as string | null },
    });
    act(() => result.current[1]("draft for A"));
    expect(result.current[0]).toBe("draft for A");

    rerender({ k: "file:b" });
    expect(result.current[0]).toBe("draft for B");
    // Switching away did not clobber A's saved draft.
    expect(localStorage.getItem("file:a")).toBe("draft for A");
  });

  it("clear() removes the stored draft (called after submit/cancel)", () => {
    const { result } = renderHook(() => usePersistedDraft("file:a"));
    act(() => result.current[1]("temp"));
    act(() => result.current[2]());
    expect(result.current[0]).toBe("");
    expect(localStorage.getItem("file:a")).toBeNull();
  });

  it("setting empty string removes the key rather than storing ''", () => {
    const { result } = renderHook(() => usePersistedDraft("file:a"));
    act(() => result.current[1]("x"));
    act(() => result.current[1](""));
    expect(localStorage.getItem("file:a")).toBeNull();
  });

  it("with a null key the draft stays in memory only", () => {
    const { result } = renderHook(() => usePersistedDraft(null));
    act(() => result.current[1]("ephemeral"));
    expect(result.current[0]).toBe("ephemeral");
    expect(localStorage.length).toBe(0);
  });
});
