import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  offlineEntryIsEditing,
  useOfflineQueue,
  type OfflinePromptEntry,
} from "../src/hooks/useOfflineQueue";
import {
  deleteOfflineAction,
  loadOfflineActions,
  offlineActionKey,
  putOfflineAction,
  updateOfflineAction,
} from "../src/lib/offlineQueueStore";

const entry = (sessionId: string, clientId: string, prompt = clientId): OfflinePromptEntry => ({
  sessionId,
  clientId,
  prompt,
  model: "sonnet",
  cwd: "/tmp/project",
});

describe("useOfflineQueue — IndexedDB persistence integrity", () => {
  it("commits concurrent tab writes without clobbering", async () => {
    await Promise.all([
      putOfflineAction(entry("a", "a1")),
      putOfflineAction(entry("b", "b1")),
      putOfflineAction(entry("a", "a2")),
    ]);
    expect((await loadOfflineActions()).map((item) => item.clientId).sort()).toEqual([
      "a1", "a2", "b1",
    ]);
  });

  it("serializes remove and enqueue without resurrecting or losing actions", async () => {
    const first = entry("a", "same", "first");
    await putOfflineAction(first);
    await Promise.all([
      deleteOfflineAction(offlineActionKey(first)),
      putOfflineAction(entry("b", "same", "second")),
    ]);
    expect(await loadOfflineActions()).toEqual([
      expect.objectContaining({ sessionId: "b", clientId: "same", prompt: "second" }),
    ]);
  });

  it("keeps attachment payloads separate from editable action metadata", async () => {
    const queued: OfflinePromptEntry = {
      ...entry("a", "a1", "original"),
      images: [{ data: "large-base64", media_type: "image/png" }],
    };
    await putOfflineAction(queued);
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("better-agent-offline-actions", 1);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    const action = await new Promise<Record<string, unknown>>((resolve, reject) => {
      const request = db.transaction("actions").objectStore("actions").get(offlineActionKey(queued));
      request.onsuccess = () => resolve(request.result as Record<string, unknown>);
      request.onerror = () => reject(request.error);
    });
    db.close();
    expect(action).not.toHaveProperty("images");
    expect((await loadOfflineActions())[0]).toEqual(queued);
  });

  it("persists edit hold across reload and saves without rewriting attachments", async () => {
    const { result, unmount } = renderHook(() => useOfflineQueue());
    await waitFor(() => expect(result.current.ready).toBe(true));
    await act(() => result.current.enqueue(entry("a", "a1", "original")));
    await act(() => result.current.beginEdit(result.current.getAll()[0]));
    await act(() => result.current.updateEditDraft(result.current.getAll()[0], "edited"));
    expect(offlineEntryIsEditing(result.current.getAll()[0])).toBe(true);
    unmount();

    const reloaded = renderHook(() => useOfflineQueue());
    await waitFor(() => expect(reloaded.result.current.ready).toBe(true));
    expect(offlineEntryIsEditing(reloaded.result.current.getAll()[0])).toBe(true);
    await act(() => reloaded.result.current.finishEdit(reloaded.result.current.getAll()[0]));
    expect((reloaded.result.current.getAll()[0] as OfflinePromptEntry).prompt).toBe("edited");
  });

  it("edits attachment-heavy actions without scaling with payload size", async () => {
    const queued: OfflinePromptEntry = {
      ...entry("a", "heavy", "original"),
      images: [{ data: "x".repeat(20_000_000), media_type: "image/png" }],
    };
    await putOfflineAction(queued);
    const key = offlineActionKey(queued);
    const started = performance.now();
    for (let index = 0; index < 25; index += 1) {
      await updateOfflineAction(key, (current) => ({
        ...current,
        editing: { originalPrompt: "original", draftPrompt: `edit-${index}` },
      }));
    }
    expect(performance.now() - started).toBeLessThan(250);
  });

  it("keeps rapid hook edits local and ordered with a 20 MB payload", async () => {
    const { result } = renderHook(() => useOfflineQueue());
    await waitFor(() => expect(result.current.ready).toBe(true));
    await act(() => result.current.enqueue({
      ...entry("a", "hook-heavy", "original"),
      images: [{ data: "x".repeat(20_000_000), media_type: "image/png" }],
    }));
    await act(() => result.current.beginEdit(result.current.getAll()[0]));
    const started = performance.now();
    for (let index = 0; index < 25; index += 1) {
      await act(() => result.current.updateEditDraft(result.current.getAll()[0], `draft-${index}`));
    }
    expect(performance.now() - started).toBeLessThan(250);
    expect(result.current.getAll()[0].editing?.draftPrompt).toBe("draft-24");
  });

  it("imports legacy intent once without overwriting newer composite entries", async () => {
    await putOfflineAction(entry("a", "same", "indexeddb"));
    localStorage.setItem("better_agent_offline_queue", JSON.stringify([
      entry("a", "same", "legacy"),
      entry("b", "other", "legacy-other"),
    ]));
    const { result } = renderHook(() => useOfflineQueue());
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.getAll()).toEqual([
      expect.objectContaining({ sessionId: "a", clientId: "same", prompt: "indexeddb" }),
      expect.objectContaining({ sessionId: "b", clientId: "other", prompt: "legacy-other" }),
    ]);
    expect(localStorage.getItem("better_agent_offline_queue")).toBeNull();
  });

  it("retains legacy intent after a failed import and retries on remount", async () => {
    const legacy = JSON.stringify([entry("a", "retry", "legacy")]);
    localStorage.setItem("better_agent_offline_queue", legacy);
    const originalPut = IDBObjectStore.prototype.put;
    let fail = true;
    IDBObjectStore.prototype.put = function (...args: Parameters<IDBObjectStore["put"]>) {
      if (fail) {
        fail = false;
        throw new DOMException("forced", "DataError");
      }
      return originalPut.apply(this, args);
    };
    const first = renderHook(() => useOfflineQueue());
    await waitFor(() => expect(first.result.current.ready).toBe(true));
    expect(localStorage.getItem("better_agent_offline_queue")).toBe(legacy);
    first.unmount();
    IDBObjectStore.prototype.put = originalPut;

    const retry = renderHook(() => useOfflineQueue());
    await waitFor(() => expect(retry.result.current.ready).toBe(true));
    expect(retry.result.current.getAll()).toEqual([
      expect.objectContaining({ sessionId: "a", clientId: "retry", prompt: "legacy" }),
    ]);
    expect(localStorage.getItem("better_agent_offline_queue")).toBeNull();
  });

  it("deletes only the selected composite identity", async () => {
    const { result } = renderHook(() => useOfflineQueue());
    await waitFor(() => expect(result.current.ready).toBe(true));
    await act(() => result.current.enqueue(entry("a", "same", "first")));
    await act(() => result.current.enqueue(entry("b", "same", "second")));
    await act(() => result.current.removeEntry(result.current.getAll()[0]));
    expect(result.current.getAll()).toEqual([
      expect.objectContaining({ sessionId: "b", clientId: "same" }),
    ]);
  });

  it("serializes a fast backend acknowledgement after its pending enqueue", async () => {
    const { result } = renderHook(() => useOfflineQueue());
    await waitFor(() => expect(result.current.ready).toBe(true));
    const enqueue = result.current.enqueue(entry("a", "fast-ack"));
    const acknowledge = result.current.removeBySessionAndClient("a", "fast-ack");
    await act(() => Promise.all([enqueue, acknowledge]));
    expect(await loadOfflineActions()).toEqual([]);
    expect(result.current.getAll()).toEqual([]);
  });
});
