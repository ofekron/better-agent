import type { Session } from "../types";

export function sessionHasForkSource(session: Session | null | undefined): boolean {
  return Boolean(session?.agent_session_id);
}
