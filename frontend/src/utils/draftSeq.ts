import type { SessionMetadataPatch } from "../hooks/useSession";

// Monotonic per-client sequence for draft writes. The backend stores the
// latest accepted `draft_input_seq` and rejects any REST write whose
// `client_seq` is <= it; the frontend mirrors that rule to drop stale WS
// echoes. Date.now() alone can collide within the same millisecond (two
// fast writers, e.g. a debounced keystroke flush immediately followed by
// the clear-on-send), which would make the backend reject the second,
// legitimate write and leave the frontend guard unable to order them.
// Forcing strict increase removes that ambiguity.
let lastDraftSeq = 0;

export function nextDraftSeq(now: number): number {
  const seq = now > lastDraftSeq ? now : lastDraftSeq + 1;
  lastDraftSeq = seq;
  return seq;
}

const DRAFT_FIELDS = ["draft_input", "draft_images", "draft_input_seq"] as const;

function stripDraftFields(patch: SessionMetadataPatch): SessionMetadataPatch {
  const rest = { ...patch } as Record<string, unknown>;
  for (const f of DRAFT_FIELDS) delete rest[f];
  return rest as SessionMetadataPatch;
}

// Decide what of an incoming `session_metadata_updated` patch to apply
// locally. Draft fields are dropped when either:
//   - the user is actively typing here (a debounce timer is pending), so a
//     remote/echoed value would clobber unsynced local text, or
//   - the incoming `draft_input_seq` is not newer than the stored one — a
//     stale/out-of-order echo (e.g. the pre-send text broadcast arriving
//     after the clear-on-send), which would otherwise resurrect sent text.
// Non-draft fields always pass through.
export function filterStaleDraftPatch(
  patch: SessionMetadataPatch,
  storedSeq: number | undefined,
  hasPendingDebounce: boolean,
): SessionMetadataPatch {
  if (patch.draft_input === undefined && patch.draft_images === undefined) {
    return patch;
  }
  if (hasPendingDebounce) return stripDraftFields(patch);
  const incoming = patch.draft_input_seq;
  if (incoming !== undefined && storedSeq !== undefined && incoming <= storedSeq) {
    return stripDraftFields(patch);
  }
  return patch;
}
