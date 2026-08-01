import type { RuntimeProfile } from "../types";

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
  /** The session's model moved out from under us (another tab/pane on the
   * same session PATCHed it, or the backend reconciled it). Adopt the new
   * value into the local selector — do NOT persist. */
  | { kind: "adopt"; model: string }
  /** The local selector moved because of a real user pick. Persist it. */
  | { kind: "patch"; model: string };

/**
 * Decides what the model-drift effect should do on this render, given
 * whether the session's stored model changed since the last render.
 *
 * Root cause this closes: when two tabs/panes have the same session open,
 * each tab keeps its own local `model` selector state independently of the
 * other's writes. A naive "local !== session -> PATCH local" comparison
 * can't tell "the session moved" from "the user moved the selector" apart,
 * so each tab's own broadcast looks like drift to the other tab, and both
 * PATCH their (now stale) local value back — an unbounded ping-pong between
 * the two values. Tracking which side actually moved since the last render
 * breaks the cycle: a remote session-model change is adopted, never echoed.
 */
export function resolveModelDriftAction(params: {
  model: string;
  sessionModel: string | null | undefined;
  prevSessionModel: string | null;
}): ModelDriftAction {
  const { model, sessionModel, prevSessionModel } = params;
  const normalizedSessionModel = sessionModel ?? null;
  const sessionModelChangedRemotely =
    prevSessionModel !== null && prevSessionModel !== normalizedSessionModel;

  if (sessionModelChangedRemotely) {
    if (normalizedSessionModel && normalizedSessionModel !== model) {
      return { kind: "adopt", model: normalizedSessionModel };
    }
    return { kind: "none" };
  }

  const drift = model && normalizedSessionModel && normalizedSessionModel !== model;
  if (!drift) return { kind: "none" };
  return { kind: "patch", model };
}
