import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useOfflineQueue, type OfflinePromptEntry } from "../src/hooks/useOfflineQueue";
import { OFFLINE_TAB_ID } from "../src/lib/offlineFlushLock";

/**
 * The durable backlog lives in `localStorage`, which every tab of the origin
 * shares. Each tab runs its own flush over those records, so without a claim
 * that is itself durable, N open tabs re-dispatch the SAME action N times —
 * observed in production as one prompt arriving at the backend nine times
 * while its optimistic "Sending..." bubble sat next to the queued banner.
 *
 * Two hook instances stand in for two tabs over one storage.
 */

const entry = (sessionId: string, clientId: string): OfflinePromptEntry => ({
  sessionId,
  clientId,
  prompt: "hello",
  model: "sonnet",
  cwd: "/tmp/project",
});

beforeEach(() => localStorage.clear());
afterEach(() => localStorage.clear());

describe("useOfflineQueue — cross-tab dispatch claim", () => {
  it("grants the dispatch to exactly one tab", () => {
    const tabA = renderHook(() => useOfflineQueue());
    act(() => {
      tabA.result.current.enqueue(entry("s1", "pending-1"));
    });

    // A second tab boots and reads the same durable backlog.
    const tabB = renderHook(() => useOfflineQueue());
    expect(tabB.result.current.getAll().map((e) => e.clientId)).toEqual(["pending-1"]);

    let claimedByA: boolean | undefined;
    let claimedByB: boolean | undefined;
    act(() => {
      claimedByA = tabA.result.current.claimForDispatch("pending-1");
    });
    act(() => {
      claimedByB = tabB.result.current.claimForDispatch("pending-1");
    });

    expect(claimedByA).toBe(true);
    expect(claimedByB).toBe(false);
  });

  it("marks the claim durably so a tab that boots later sees it in flight", () => {
    const tabA = renderHook(() => useOfflineQueue());
    act(() => {
      tabA.result.current.enqueue(entry("s1", "pending-1"));
      tabA.result.current.claimForDispatch("pending-1");
    });

    const tabB = renderHook(() => useOfflineQueue());
    expect(tabB.result.current.getAll()[0]?.dispatchedBy).toBe(OFFLINE_TAB_ID);
  });

  it("re-arms this tab's claims on connection loss so reconnect redelivers", () => {
    const tabA = renderHook(() => useOfflineQueue());
    act(() => {
      tabA.result.current.enqueue(entry("s1", "pending-1"));
      tabA.result.current.enqueue(entry("s1", "pending-2"));
      tabA.result.current.claimForDispatch("pending-1");
      tabA.result.current.claimForDispatch("pending-2");
    });

    // The socket dropped: those sends are genuinely undelivered.
    act(() => {
      tabA.result.current.releaseOwnDispatched();
    });

    expect(tabA.result.current.getAll().every((e) => !e.dispatchedBy)).toBe(true);
    let reclaimed: boolean | undefined;
    act(() => {
      reclaimed = tabA.result.current.claimForDispatch("pending-1");
    });
    expect(reclaimed).toBe(true);
  });

  it("releases a single claim when the socket refused that one send", () => {
    const tabA = renderHook(() => useOfflineQueue());
    act(() => {
      tabA.result.current.enqueue(entry("s1", "pending-1"));
      tabA.result.current.enqueue(entry("s1", "pending-2"));
      tabA.result.current.claimForDispatch("pending-1");
      tabA.result.current.claimForDispatch("pending-2");
    });

    act(() => {
      tabA.result.current.releaseDispatch("pending-2");
    });

    const byId = Object.fromEntries(
      tabA.result.current.getAll().map((e) => [e.clientId, Boolean(e.dispatchedBy)]),
    );
    expect(byId).toEqual({ "pending-1": true, "pending-2": false });
  });

  it("drops the claim with the entry once the backend acks it", () => {
    const tabA = renderHook(() => useOfflineQueue());
    act(() => {
      tabA.result.current.enqueue(entry("s1", "pending-1"));
      tabA.result.current.claimForDispatch("pending-1");
      tabA.result.current.removeBySessionAndClient("s1", "pending-1");
    });

    expect(tabA.result.current.getAll()).toEqual([]);
    // A never-enqueued (or already acked) action cannot be claimed.
    let claimed: boolean | undefined;
    act(() => {
      claimed = tabA.result.current.claimForDispatch("pending-1");
    });
    expect(claimed).toBe(false);
  });
});
