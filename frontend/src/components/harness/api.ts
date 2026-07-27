import { API } from "../../api";
import { trackedFetch } from "../../progress/store";
import type { HarnessProfile } from "../../types";
import type { HarnessDescriptor, HarnessFieldWrite } from "./types";

/** Thrown when the backend rejects a write because the caller's revision is
 * stale (another tab or session mutated the profile first). Distinguished
 * from a generic failure so the UI can refetch instead of showing a raw
 * HTTP error. */
export const REVISION_MISMATCH = "revision_mismatch";

/** Thrown when the addressed profile no longer exists — it was deleted here
 * or in another tab while it was still the editor's selection. Distinguished
 * so the UI can follow the backend down to Default instead of showing a raw
 * "Not Found" against a dead id. */
export const PROFILE_NOT_FOUND = "profile_not_found";

/** The per-profile endpoints; a 404 from them means the profile is gone,
 * which is a selection transition rather than a failure. */
async function throwForProfileStatus(res: Response): Promise<never> {
  if (res.status === 404) throw new Error(PROFILE_NOT_FOUND);
  return throwForStatus(res);
}

async function throwForStatus(res: Response): Promise<never> {
  if (res.status === 409) throw new Error(REVISION_MISMATCH);
  let detail = "";
  try {
    detail = String(((await res.json()) as { detail?: unknown })?.detail ?? "");
  } catch {
    detail = "";
  }
  throw new Error(detail || `HTTP ${res.status}`);
}

export async function fetchDescriptor(): Promise<HarnessDescriptor> {
  const res = await trackedFetch("harnessProfiles:descriptor", `${API}/api/harness-profiles/descriptor`);
  if (!res.ok) return throwForStatus(res);
  return res.json();
}

export async function fetchProfile(id: string): Promise<HarnessProfile> {
  const res = await trackedFetch(
    `harnessProfiles:fetch:${id}`,
    `${API}/api/harness-profiles/${encodeURIComponent(id)}`,
  );
  if (!res.ok) return throwForProfileStatus(res);
  return res.json();
}

/** Single write path for every field, on Default and named profiles alike —
 * the backend routes by scope, so the UI never picks an endpoint. */
export async function writeFields(
  id: string,
  writes: HarnessFieldWrite[],
  revision: string,
): Promise<HarnessProfile> {
  const res = await fetch(`${API}/api/harness-profiles/${encodeURIComponent(id)}/fields`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ revision, writes }),
  });
  if (!res.ok) return throwForProfileStatus(res);
  return res.json();
}

export async function createProfile(name: string): Promise<HarnessProfile> {
  const res = await fetch(`${API}/api/harness-profiles`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) return throwForStatus(res);
  return res.json();
}

export async function deleteProfile(id: string, revision: string): Promise<void> {
  const res = await fetch(
    `${API}/api/harness-profiles/${encodeURIComponent(id)}?revision=${encodeURIComponent(revision)}`,
    { method: "DELETE", credentials: "include" },
  );
  if (!res.ok) await throwForProfileStatus(res);
}
