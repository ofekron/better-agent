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
