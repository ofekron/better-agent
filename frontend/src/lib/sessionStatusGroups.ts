import type { Session } from "../types";

const STATUS_GROUP_I18N_KEY: Record<number, string> = {
  7: "session.statusGroup.new",
  6: "session.statusGroup.errors",
  5: "session.statusGroup.needsDecision",
  4: "session.statusGroup.unread",
  3: "session.statusGroup.openWork",
  2: "session.statusGroup.running",
  1: "session.statusGroup.done",
  0: "session.statusGroup.inactive",
};

export type SessionStatusGroupRun = { rank: number; sessions: Session[] };

export function statusGroupI18nKey(rank: number): string {
  const key = STATUS_GROUP_I18N_KEY[rank];
  if (!key) throw new Error(`Unknown session status rank: ${rank}`);
  return key;
}

export function groupSessionsByStatusRank(sessions: Session[]): SessionStatusGroupRun[] {
  const runs: SessionStatusGroupRun[] = [];
  for (const session of sessions) {
    const rank = session.status_rank ?? 0;
    const last = runs[runs.length - 1];
    if (last?.rank === rank) {
      last.sessions.push(session);
      continue;
    }
    runs.push({ rank, sessions: [session] });
  }
  return runs;
}
