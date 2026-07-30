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
      runtime_profile_id: "rp-1",
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

  it("projects the session snapshot to the profile-based wire shape", () => {
    const id = "11111111-1111-4111-8111-111111111111";
    const entry = createEntry(id);
    if (entry.type !== "create_session") throw new Error("bad fixture");
    // Stale identity fields from pre-profile persisted entries and local-only
    // flags: the backend batch validator rejects unknown fields, so normalize
    // must strip them while keeping the profile-based identity + overrides.
    entry.session = {
      ...entry.session,
      provider_id: "p",
      runner: "cli",
      pinned: true,
      offline_pending: true,
      last_opened_at: "t",
    } as unknown as typeof entry.session;
    const out = normalizeQueueEntries([entry]);
    const normalized = out[0];
    if (normalized.type !== "create_session") throw new Error("type changed");
    expect(normalized.session.runtime_profile_id).toBe("rp-1");
    expect(normalized.session.model).toBe("m");
    expect(normalized.session).not.toHaveProperty("provider_id");
    expect(normalized.session).not.toHaveProperty("runner");
    expect(normalized.session).not.toHaveProperty("pinned");
    expect(normalized.session).not.toHaveProperty("offline_pending");
    expect(normalized.session).not.toHaveProperty("last_opened_at");
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
