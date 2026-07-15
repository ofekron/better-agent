import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useOfflineQueue, type OfflinePromptEntry } from "../src/hooks/useOfflineQueue";

const entry = (sessionId: string, clientId: string, prompt = clientId): OfflinePromptEntry => ({
  sessionId,
  clientId,
  prompt,
  model: "sonnet",
  cwd: "/tmp/project",
});

describe("useOfflineQueue", () => {
  it("preserves insertion order and dedupes only the same composite identity", async () => {
    const { result } = renderHook(() => useOfflineQueue());
    await waitFor(() => expect(result.current.ready).toBe(true));
    await act(() => result.current.enqueue(entry("a", "same", "first")));
    await act(() => result.current.enqueue(entry("b", "same", "second")));
    await act(() => result.current.enqueue(entry("a", "same", "edited")));
    expect(result.current.getAll()).toEqual([
      expect.objectContaining({ sessionId: "a", clientId: "same", prompt: "edited" }),
      expect.objectContaining({ sessionId: "b", clientId: "same", prompt: "second" }),
    ]);
  });
});
