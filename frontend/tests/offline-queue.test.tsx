import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  useOfflineQueue,
  type OfflineCreateSessionEntry,
  type OfflinePromptEntry,
} from "../src/hooks/useOfflineQueue";

const entry = (sessionId: string, clientId: string, prompt = clientId): OfflinePromptEntry => ({
  sessionId,
  clientId,
  prompt,
  model: "sonnet",
  cwd: "/tmp/project",
});

const createEntry = (sessionId: string, clientId: string): OfflineCreateSessionEntry => ({
  type: "create_session",
  clientId,
  prompt: "hello",
  session: {
    id: sessionId,
    name: "n",
    model: "override-model",
    reasoning_effort: "high",
    permission: undefined,
    cwd: "/tmp/project",
    orchestration_mode: "native",
    runtime_profile_id: "rp-1",
    node_id: "primary",
    created_at: "t",
    updated_at: "t",
    messages: [],
    capability_contexts: undefined,
    harness_profile_id: "",
    folder_id: null,
  },
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

  it("keeps repeated session actions in send order", () => {
    const { result } = renderHook(() => useOfflineQueue());

    act(() => {
      result.current.enqueue(entry("a", "a1"));
      result.current.enqueue(entry("b", "b1"));
      result.current.enqueue(entry("a", "a2"));
    });

    expect(result.current.getAll().map((item) => item.clientId)).toEqual(["a1", "b1", "a2"]);
    expect(
      JSON.parse(localStorage.getItem("better_agent_offline_queue") || "[]").map(
        (item: OfflinePromptEntry) => item.clientId,
      ),
    ).toEqual(["a1", "b1", "a2"]);
  });

  it("persists queued creates in the profile-based payload shape", () => {
    const { result } = renderHook(() => useOfflineQueue());
    const id = "22222222-2222-4222-8222-222222222222";
    const create = createEntry(id, "c1");
    // Simulate a caller handing over a full Session object: local-only flags
    // and stale raw provider/runner identity must never reach the wire shape.
    create.session = {
      ...create.session,
      provider_id: "claude",
      runner: "cli",
      pinned: false,
      offline_pending: true,
    } as unknown as typeof create.session;

    act(() => result.current.enqueue(create));

    const [stored] = JSON.parse(localStorage.getItem("better_agent_offline_queue")!);
    expect(stored.session).toMatchObject({
      id,
      runtime_profile_id: "rp-1",
      model: "override-model",
      reasoning_effort: "high",
    });
    expect(stored.session).not.toHaveProperty("provider_id");
    expect(stored.session).not.toHaveProperty("runner");
    expect(stored.session).not.toHaveProperty("pinned");
    expect(stored.session).not.toHaveProperty("offline_pending");
  });

  it("acks remove exactly the acknowledged action and keep the rest in order", () => {
    const { result } = renderHook(() => useOfflineQueue());
    const sessionA = "33333333-3333-4333-8333-333333333333";
    const sessionB = "44444444-4444-4444-8444-444444444444";

    act(() => {
      result.current.enqueue(createEntry(sessionA, "cA"));
      result.current.enqueue(entry(sessionA, "pA"));
      result.current.enqueue(createEntry(sessionB, "cB"));
    });
    expect(result.current.getAll().map((e) => e.clientId)).toEqual(["cA", "pA", "cB"]);

    // Backend acks arrive per action; each removes only its own entry while
    // the rest keep their relative order.
    act(() => result.current.removeBySessionAndClient(sessionA, "cA"));
    expect(result.current.getAll().map((e) => e.clientId)).toEqual(["pA", "cB"]);

    act(() => result.current.removeBySessionAndClient(sessionA, "pA"));
    expect(result.current.getAll().map((e) => e.clientId)).toEqual(["cB"]);

    act(() => result.current.removeBySessionAndClient(sessionB, "cB"));
    expect(result.current.getAll()).toEqual([]);
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
