import { act, renderHook } from "@testing-library/react";
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
  it("persists every mutation synchronously", () => {
    const { result } = renderHook(() => useOfflineQueue());

    act(() => result.current.enqueue(entry("a", "a1")));
    expect(JSON.parse(localStorage.getItem("better_agent_offline_queue") || "[]")).toEqual([
      expect.objectContaining({ clientId: "a1" }),
    ]);

    act(() => result.current.remove("a1"));
    expect(localStorage.getItem("better_agent_offline_queue")).toBeNull();
  });

  it("replaces merged session actions without changing global order", () => {
    const { result } = renderHook(() => useOfflineQueue());

    act(() => {
      result.current.enqueue(entry("a", "a1"));
      result.current.enqueue(entry("b", "b1"));
      result.current.replaceBySession("a", entry("a", "a2", "merged"));
    });

    expect(result.current.getAll().map((item) => item.clientId)).toEqual(["a2", "b1"]);
    expect(
      JSON.parse(localStorage.getItem("better_agent_offline_queue") || "[]").map(
        (item: OfflinePromptEntry) => item.clientId,
      ),
    ).toEqual(["a2", "b1"]);
  });

  it("removes an acked action only when both session and client id match", () => {
    const { result } = renderHook(() => useOfflineQueue());

    act(() => {
      result.current.enqueue(entry("a", "same"));
      result.current.enqueue(entry("b", "same"));
      result.current.removeBySessionAndClient("a", "same");
    });

    expect(result.current.getAll()).toEqual([
      expect.objectContaining({ sessionId: "b", clientId: "same" }),
    ]);
    expect(JSON.parse(localStorage.getItem("better_agent_offline_queue") || "[]")).toEqual([
      expect.objectContaining({ sessionId: "b", clientId: "same" }),
    ]);
  });
});
