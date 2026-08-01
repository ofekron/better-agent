import type { RuntimeProfile } from "../types";
import { isStaleSeq } from "./draftSeq";

/**
 * True when `model` is the default runtime profile's mirror value that has
 * leaked onto a session whose own provider is DIFFERENT. The global `model`
 * state doubles as a mirror of the default profile's model (shown when no
 * session is focused); on a default-profile switch it can hold the new
 * profile's default/last-used model. Persisting that onto a session backed
 * by a different provider corrupts its model (and is rejected by the
 * backend). The drift-detector uses this to suppress such a write.
 */
export function isLeakedProfileMirror(
  model: string,
  sessionProviderId: string | null | undefined,
  defaultProfile: RuntimeProfile | null,
  lastModels: Record<string, string>,
): boolean {
  if (!model || !sessionProviderId || !defaultProfile) return false;
  if (sessionProviderId === defaultProfile.provider_id) return false;
  return (
    model === defaultProfile.default_model ||
    model === lastModels[defaultProfile.id]
  );
}

export type ModelDriftAction =
  | { kind: "none" }
  /** The session's selector state advanced remotely (another tab/pane on
   * the same session PATCHed it, or the backend reconciled it). Adopt the
   * new value into the local selector — do NOT persist. */
  | { kind: "adopt"; model: string }
  /** Nothing new arrived from the backend since we last looked, so a local
   * `model` != session-model mismatch means a real, not-yet-persisted user
   * pick. Persist it. */
  | { kind: "patch"; model: string };

/**
 * Decides what the model-drift effect should do on this render, using the
 * backend-authoritative `selectors_seq` (bumped by every accepted model/
 * provider_id/reasoning_effort write, from ANY tab — see
 * `SessionManager._bump_selectors_seq` in backend/session_manager.py) to
 * order events instead of diffing values.
 *
 * Root cause this closes: two tabs/panes on the same session each keep
 * their own local `model` selector state independently of the other's
 * writes. A naive "local !== session -> PATCH local" comparison can't tell
 * "the session moved" from "the user moved the selector" apart, so each
 * tab's own broadcast looks like drift to the other tab, and both PATCH
 * their (now stale) local value back — an unbounded ping-pong. Comparing
 * `sessionSelectorsSeq` (what the session reports right now) against
 * `lastAppliedSelectorsSeq` (the seq value this tab last incorporated)
 * breaks the cycle: if the seq advanced since we last looked, the session's
 * state is authoritative and newer than anything we could push — adopt it,
 * never patch it back. This generalizes past the 2-tab/2-value case (a
 * plain value-diff can't order more than two alternating states or survive
 * out-of-order delivery; a monotonic seq can).
 */
export function resolveModelDriftAction(params: {
  model: string;
  sessionModel: string | null | undefined;
  sessionSelectorsSeq: number;
  lastAppliedSelectorsSeq: number;
}): ModelDriftAction {
  const { model, sessionModel, sessionSelectorsSeq, lastAppliedSelectorsSeq } = params;
  const normalizedSessionModel = sessionModel ?? null;

  if (!isStaleSeq(sessionSelectorsSeq, lastAppliedSelectorsSeq)) {
    // sessionSelectorsSeq > lastAppliedSelectorsSeq: newer selector state
    // landed since we last looked.
    if (normalizedSessionModel && normalizedSessionModel !== model) {
      return { kind: "adopt", model: normalizedSessionModel };
    }
    return { kind: "none" };
  }

  const drift = model && normalizedSessionModel && normalizedSessionModel !== model;
  if (!drift) return { kind: "none" };
  return { kind: "patch", model };
}

/**
 * Drops `model`/`provider_id`/`reasoning_effort` from a
 * `session_metadata_updated` patch when its `selectors_seq` is not newer
 * than the one already applied — a stale/out-of-order echo, mirroring
 * `filterStaleDraftPatch`/`filterStaleQueuedPromptsPatch`.
 */
export function filterStaleSelectorsPatch<
  T extends {
    model?: string;
    provider_id?: string;
    reasoning_effort?: string;
    selectors_seq?: number;
  },
>(patch: T, storedSeq: number | undefined): T {
  if (
    patch.model === undefined &&
    patch.provider_id === undefined &&
    patch.reasoning_effort === undefined
  ) {
    return patch;
  }
  if (!isStaleSeq(patch.selectors_seq, storedSeq)) return patch;
  const rest = { ...patch } as Record<string, unknown>;
  delete rest.model;
  delete rest.provider_id;
  delete rest.reasoning_effort;
  delete rest.selectors_seq;
  return rest as T;
}
