import type { RunInfo } from "../types";

/** Active backend run detail controls turn-group presentation. Session-level
 * lifecycle stays authoritative in sessionRegistry. */
export function isGroupRunning(runs: RunInfo[] | undefined): boolean {
  return (runs ?? []).length > 0;
}
