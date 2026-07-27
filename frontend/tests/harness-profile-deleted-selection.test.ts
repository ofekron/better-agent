import { afterEach, describe, expect, it, vi } from "vitest";

// The editor reacts to a global profiles-changed event by refetching whatever
// is selected. Deleting the selected profile makes that refetch chase an id
// the backend no longer has; a raw 404 there surfaced as
// "Failed to update harness profile: Not Found" against a dead selection.
// These lock the 404 → PROFILE_NOT_FOUND mapping the editor keys its
// fall-back-to-Default on.

const trackedFetch = vi.fn();
const plainFetch = vi.fn();

vi.mock("../src/api", () => ({ API: "" }));
vi.mock("../src/progress/store", () => ({
  trackedFetch: (...args: unknown[]) => trackedFetch(...args),
}));

vi.stubGlobal("fetch", (...args: unknown[]) => plainFetch(...args));

const { PROFILE_NOT_FOUND, REVISION_MISMATCH, deleteProfile, fetchProfile, writeFields } =
  await import("../src/components/harness/api");

function response(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

afterEach(() => {
  trackedFetch.mockReset();
  plainFetch.mockReset();
});

describe("deleted-profile selection", () => {
  it("maps a 404 fetch to PROFILE_NOT_FOUND", async () => {
    trackedFetch.mockResolvedValue(response(404, { detail: "Not Found" }));
    await expect(fetchProfile("gone")).rejects.toThrow(PROFILE_NOT_FOUND);
  });

  it("maps a 404 write to PROFILE_NOT_FOUND", async () => {
    plainFetch.mockResolvedValue(response(404, { detail: "Not Found" }));
    await expect(writeFields("gone", [], "3")).rejects.toThrow(PROFILE_NOT_FOUND);
  });

  it("maps a 404 delete to PROFILE_NOT_FOUND", async () => {
    plainFetch.mockResolvedValue(response(404, { detail: "Not Found" }));
    await expect(deleteProfile("gone", "3")).rejects.toThrow(PROFILE_NOT_FOUND);
  });

  it("keeps 409 distinct so stale revisions still refetch", async () => {
    plainFetch.mockResolvedValue(response(409, { detail: "stale" }));
    await expect(writeFields("live", [], "1")).rejects.toThrow(REVISION_MISMATCH);
  });

  it("leaves other failures reported verbatim", async () => {
    trackedFetch.mockResolvedValue(response(500, { detail: "boom" }));
    await expect(fetchProfile("live")).rejects.toThrow("boom");
  });
});
