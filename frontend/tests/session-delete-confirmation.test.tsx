import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useRef } from "react";
import { useSessionDeleteConfirmation } from "../src/hooks/useSessionDeleteConfirmation";
import type { Session } from "../src/types";

function makeSession(id: string): Session {
  return {
    id,
    name: id,
    model: "gpt-5-codex",
    cwd: "/tmp/project",
    orchestration_mode: "native",
    created_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z",
    messages: [],
  };
}

function useHarness(deleteSession: (id: string) => Promise<void>) {
  const draftDebounceRef = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  return useSessionDeleteConfirmation({
    sessions: [makeSession("alpha"), makeSession("gamma")],
    getNode: () => null,
    deleteSession,
    draftDebounceRef,
  });
}

describe("useSessionDeleteConfirmation", () => {
  it("confirms every bulk-selected session from one pending delete", async () => {
    const deleteSession = vi.fn(async () => {});
    const { result } = renderHook(() => useHarness(deleteSession));

    act(() => {
      result.current.requestDeleteSessions(["alpha", "gamma"]);
    });

    expect(result.current.sessionsToDelete).toEqual(["alpha", "gamma"]);
    expect(result.current.sessionBeingDeleted).toBeNull();

    await act(async () => {
      await result.current.confirmDeleteSessions();
    });

    expect(deleteSession).toHaveBeenCalledTimes(2);
    expect(deleteSession).toHaveBeenNthCalledWith(1, "alpha");
    expect(deleteSession).toHaveBeenNthCalledWith(2, "gamma");
    expect(result.current.sessionsToDelete).toEqual([]);
  });
});
