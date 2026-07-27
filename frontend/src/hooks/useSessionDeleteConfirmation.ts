import { useCallback, useMemo, useState, type MutableRefObject } from "react";
import type { Session } from "../types";

type DraftDebounceTimers = MutableRefObject<Map<string, ReturnType<typeof setTimeout>>>;

interface UseSessionDeleteConfirmationArgs {
  sessions: Session[];
  getNode: (id: string) => Session | null | undefined;
  deleteSession: (id: string) => Promise<void>;
  draftDebounceRef: DraftDebounceTimers;
}

export function useSessionDeleteConfirmation({
  sessions,
  getNode,
  deleteSession,
  draftDebounceRef,
}: UseSessionDeleteConfirmationArgs) {
  const [sessionsToDelete, setSessionsToDelete] = useState<string[]>([]);

  const requestDeleteSession = useCallback((id: string) => {
    setSessionsToDelete([id]);
  }, []);

  const requestDeleteSessions = useCallback((ids: string[]) => {
    const uniqueIds = Array.from(new Set(ids.filter(Boolean)));
    if (uniqueIds.length === 0) return;
    setSessionsToDelete(uniqueIds);
  }, []);

  const cancelDeleteSessions = useCallback(() => {
    setSessionsToDelete([]);
  }, []);

  const confirmDeleteSessions = useCallback(async () => {
    if (sessionsToDelete.length === 0) return;
    const ids = sessionsToDelete;
    setSessionsToDelete([]);

    const timers = draftDebounceRef.current;
    for (const id of ids) {
      const existing = timers.get(id);
      if (!existing) continue;
      clearTimeout(existing);
      timers.delete(id);
    }
    await Promise.all(ids.map((id) => deleteSession(id)));
  }, [deleteSession, draftDebounceRef, sessionsToDelete]);

  const sessionBeingDeleted = useMemo(() => {
    if (sessionsToDelete.length !== 1) return null;
    const [id] = sessionsToDelete;
    return sessions.find((session) => session.id === id) || getNode(id) || null;
  }, [getNode, sessions, sessionsToDelete]);

  return {
    sessionsToDelete,
    sessionBeingDeleted,
    requestDeleteSession,
    requestDeleteSessions,
    confirmDeleteSessions,
    cancelDeleteSessions,
  };
}
