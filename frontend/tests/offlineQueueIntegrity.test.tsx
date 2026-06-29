import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useOfflineQueue, type OfflinePromptEntry } from "../src/hooks/useOfflineQueue";

const STORAGE_KEY = "better_agent_offline_queue";

const entry = (sessionId: string, clientId: string, prompt = clientId): OfflinePromptEntry => ({
  sessionId,
  clientId,
  prompt,
  model: "sonnet",
  cwd: "/tmp/project",
});

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("useOfflineQueue — persistence integrity", () => {
  it("flags persistFailed and fails closed when the write throws (quota / private mode)", () => {
    const { result } = renderHook(() => useOfflineQueue());
    // Simulate QuotaExceededError on the next setItem only. The setup
    // polyfill installs a MemoryStorage INSTANCE, so spy the instance
    // method rather than Storage.prototype.
    const setItem = vi
      .spyOn(globalThis.localStorage, "setItem")
      .mockImplementationOnce(() => {
        throw new DOMException("quota", "QuotaExceededError");
      });

    let ok: boolean | undefined;
    act(() => {
      ok = result.current.enqueue(entry("a", "a1"));
    });

    // The write failed...
    expect(ok).toBe(false);
    expect(result.current.persistFailed).toBe(true);
    // ...and it is not advertised as queued/replayable because it is not
    // durable. The caller restores/keeps the user's draft instead.
    expect(result.current.getAll()).toEqual([]);
    setItem.mockRestore();
  });

  it("recovers persistFailed back to false on the next successful write", () => {
    const { result } = renderHook(() => useOfflineQueue());
    vi.spyOn(globalThis.localStorage, "setItem").mockImplementationOnce(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    act(() => result.current.enqueue(entry("a", "a1")));
    expect(result.current.persistFailed).toBe(true);

    // Next write succeeds (mock was once-only).
    act(() => result.current.enqueue(entry("a", "a2")));
    expect(result.current.persistFailed).toBe(false);
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]").map((e: OfflinePromptEntry) => e.clientId)).toEqual([
      "a2",
    ]);
  });

  it("merges concurrent tabs instead of clobbering (read-modify-write against fresh disk)", () => {
    // Tab A mounts and enqueues a1.
    const tabA = renderHook(() => useOfflineQueue());
    act(() => tabA.result.current.enqueue(entry("a", "a1")));

    // Tab B mounts with the same disk (sees a1), then a write from ANOTHER
    // context (simulating Tab A's later write) lands b1 on disk while Tab B
    // still holds its older snapshot.
    const tabB = renderHook(() => useOfflineQueue());
    const onDisk = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    localStorage.setItem(STORAGE_KEY, JSON.stringify([...onDisk, entry("b", "b1")]));

    // Tab B enqueues a2. A naive snapshot-overwrite would drop b1.
    act(() => tabB.result.current.enqueue(entry("a", "a2")));

    const persisted = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]").map(
      (e: OfflinePromptEntry) => e.clientId,
    );
    expect(persisted).toContain("a1");
    expect(persisted).toContain("b1"); // <-- the other tab's entry survived
    expect(persisted).toContain("a2");
  });

  it("tolerates a corrupt (non-JSON) blob without throwing — starts clean", () => {
    localStorage.setItem(STORAGE_KEY, "{not valid json");
    const { result } = renderHook(() => useOfflineQueue());
    expect(result.current.getAll()).toEqual([]);
    act(() => result.current.enqueue(entry("a", "a1")));
    expect(result.current.getAll().map((e) => e.clientId)).toEqual(["a1"]);
  });

  it("drops only unusable entries from a partially-corrupt array, salvaging the rest", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify([
        entry("a", "a1"),
        { clientId: "broken-no-session", prompt: "x", model: "m", cwd: "/" }, // missing sessionId
        { sessionId: "b", clientId: "b1", prompt: "", model: "m", cwd: "/" }, // no prompt or payload
        entry("c", "c1"),
      ]),
    );
    const { result } = renderHook(() => useOfflineQueue());
    expect(result.current.getAll().map((e) => e.clientId)).toEqual(["a1", "c1"]);
  });

  it("keeps attachment-only prompt entries (empty text with image/file payloads)", () => {
    const attachmentOnly: OfflinePromptEntry = {
      ...entry("a", "a1", ""),
      images: [{ data: "base64", media_type: "image/png" }],
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify([attachmentOnly]));
    const { result } = renderHook(() => useOfflineQueue());
    expect(result.current.getAll()).toEqual([attachmentOnly]);
  });

  it("dedupes a re-enqueue of the same (session, client) identity, keeping the latest content", () => {
    const { result } = renderHook(() => useOfflineQueue());
    act(() => {
      result.current.enqueue(entry("a", "a1", "first"));
      result.current.enqueue(entry("a", "a1", "edited"));
    });
    const all = result.current.getAll();
    expect(all).toHaveLength(1);
    expect((all[0] as OfflinePromptEntry).prompt).toBe("edited");
  });

  it("converges on a cross-tab `storage` event (re-reads authoritative disk state)", () => {
    const { result } = renderHook(() => useOfflineQueue());
    act(() => result.current.enqueue(entry("a", "a1")));
    expect(result.current.queue).toHaveLength(1);

    // Another tab appends b1 to disk and the browser fires `storage`.
    const onDisk = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    localStorage.setItem(STORAGE_KEY, JSON.stringify([...onDisk, entry("b", "b1")]));
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY }));
    });

    expect(result.current.queue.map((e) => e.clientId).sort()).toEqual(["a1", "b1"]);
  });

  it("clears the storage key entirely when the last entry is removed", () => {
    const { result } = renderHook(() => useOfflineQueue());
    act(() => result.current.enqueue(entry("a", "a1")));
    act(() => result.current.remove("a1"));
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });
});
