import { ASK_SINGLETON_ID } from "./askSession";
import { editSingletonId } from "./projectStructureEditSession";
import type { Session } from "./types";

export function isAutoSelectableSession(session: Session): boolean {
  return (
    !session.archived
    && session.id !== ASK_SINGLETON_ID
    && session.id !== editSingletonId()
  );
}

export function firstAutoSelectableSession(sessions: Session[]): Session | null {
  return sessions.find(isAutoSelectableSession) ?? null;
}
