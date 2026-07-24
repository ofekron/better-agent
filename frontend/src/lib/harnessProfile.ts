/** Sentinel the harness profile selector UI uses for the synthesized
 *  "Default" option (a real, selectable option shown to the user, but
 *  never a stored profile on the backend). */
export const DEFAULT_HARNESS_PROFILE_ID = "default";

/** Map the selector's UI-facing value onto the wire value the backend
 *  expects. The backend's `_harness_profile_selection` (main.py) treats
 *  an empty/missing `harness_profile_id` as "use Default implicitly";
 *  it 404s on the literal string "default" since Default isn't a stored
 *  profile. Every call site that sends a harness profile id sourced from
 *  the selector must route through this before it hits the network. */
export function wireHarnessProfileId(id: string | undefined | null): string | undefined {
  const trimmed = (id || "").trim();
  if (!trimmed || trimmed === DEFAULT_HARNESS_PROFILE_ID) return undefined;
  return trimmed;
}
