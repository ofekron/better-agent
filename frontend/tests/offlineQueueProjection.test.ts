import { describe, expect, it } from "vitest";
import type { OfflinePromptEntry } from "../src/hooks/useOfflineQueue";
import {
  reconcileOfflinePendingMessages,
  reconcileOfflineQueueDrafts,
} from "../src/lib/offlineQueueProjection";
import type { ChatMessage } from "../src/types";

const offlineEntry: OfflinePromptEntry = {
  sessionId: "session-a",
  clientId: "offline-a",
  prompt: "queued offline",
  model: "sonnet",
  cwd: "/tmp/project",
  sendMode: "queue",
};

const message = (id: string, status: ChatMessage["status"]): ChatMessage => ({
  id,
  role: "user",
  content: id,
  events: [],
  isStreaming: false,
  status,
});

describe("offline queue projection reconciliation", () => {
  it("removes absent offline mirrors while preserving online ack bridges", () => {
    const pending = {
      "session-a": [message("offline-a", "offline"), message("sending-a", "sending")],
    };
    const drafts = {
      "session-a": [
        { id: "offline-a", clientId: "offline-a", preview: "offline", offline: true },
        { id: "sending-a", clientId: "sending-a", preview: "sending" },
      ],
    };

    expect(reconcileOfflinePendingMessages(pending, [])).toEqual({
      "session-a": [expect.objectContaining({ id: "sending-a", status: "sending" })],
    });
    expect(reconcileOfflineQueueDrafts(drafts, [])).toEqual({
      "session-a": [expect.objectContaining({ clientId: "sending-a" })],
    });
  });

  it("retains mirrors whose composite identities remain authoritative", () => {
    const pending = { "session-a": [message("offline-a", "offline")] };
    const drafts = {
      "session-a": [{ id: "offline-a", clientId: "offline-a", preview: "offline", offline: true }],
    };
    expect(reconcileOfflinePendingMessages(pending, [offlineEntry])).toBe(pending);
    expect(reconcileOfflineQueueDrafts(drafts, [offlineEntry])).toBe(drafts);
  });

  it("projects durable failure, retry, and removal into an existing bubble", () => {
    const pending = { "session-a": [message("offline-a", "sending")] };
    const failedEntry: OfflinePromptEntry = {
      ...offlineEntry,
      failure: { errorText: "Provider is suspended" },
    };

    const failed = reconcileOfflinePendingMessages(pending, [failedEntry]);
    expect(failed).toEqual({
      "session-a": [expect.objectContaining({
        id: "offline-a",
        status: "error",
        errorText: "Provider is suspended",
      })],
    });

    const retried = reconcileOfflinePendingMessages(failed, [offlineEntry]);
    expect(retried).toEqual({
      "session-a": [expect.objectContaining({
        id: "offline-a",
        status: "offline",
        errorText: undefined,
      })],
    });
    expect(reconcileOfflinePendingMessages(failed, [])).toEqual({});
  });

  it("adopts a matching runtime draft into the authoritative offline projection", () => {
    const runtimeDrafts = {
      "session-a": [{ id: "offline-a", clientId: "offline-a", preview: "offline" }],
    };
    const adopted = reconcileOfflineQueueDrafts(runtimeDrafts, [offlineEntry]);
    expect(adopted).toEqual({
      "session-a": [expect.objectContaining({ clientId: "offline-a", offline: true })],
    });
    expect(reconcileOfflineQueueDrafts(adopted, [])).toEqual({});
  });
});
