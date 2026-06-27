import { describe, expect, it } from "vitest";
import { normalizeQueueEntries, type OfflineQueueEntry } from "../src/hooks/useOfflineQueue";

const CANONICAL_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function createEntry(id: string): OfflineQueueEntry {
  return {
    type: "create_session",
    clientId: `offline-create-${id}`,
    prompt: "do the thing",
    session: {
      id,
      name: "x",
      model: "m",
      reasoning_effort: undefined,
      permission: undefined,
      cwd: "/tmp",
      orchestration_mode: "native",
      provider_id: "p",
      browser_harness_enabled: false,
      browser_harness_headless: true,
      node_id: "primary",
      created_at: "t",
      updated_at: "t",
      messages: [],
      capability_contexts: undefined,
      folder_id: null,
    },
  };
}

describe("normalizeQueueEntries", () => {
  it("re-mints a non-canonical create_session id to a canonical UUID, preserving intent", () => {
    const out = normalizeQueueEntries([createEntry("offline-create-123")]);
    const entry = out[0];
    if (entry.type !== "create_session") throw new Error("type changed");
    expect(entry.session.id).toMatch(CANONICAL_UUID);
    expect(entry.prompt).toBe("do the thing"); // intent preserved
    expect(entry.session.cwd).toBe("/tmp");
  });

  it("leaves a canonical create_session id untouched", () => {
    const id = "11111111-1111-4111-8111-111111111111";
    const out = normalizeQueueEntries([createEntry(id)]);
    const entry = out[0];
    if (entry.type !== "create_session") throw new Error("type changed");
    expect(entry.session.id).toBe(id);
  });

  it("leaves send_message entries untouched", () => {
    const send: OfflineQueueEntry = {
      type: "send_message",
      sessionId: "not-a-uuid",
      clientId: "c1",
      prompt: "hi",
      model: "m",
      cwd: "/tmp",
    };
    expect(normalizeQueueEntries([send])).toEqual([send]);
  });
});
