import type { Session } from "../types";

/** Sort fields shared by the sidebar list (`session_sort`) and the open
 * tabs bar (`sessions_tabs_sort`), with their i18n label keys. */
export const SESSION_SORT_LABEL: Record<string, string> = {
  updated_at: "session.sortByModified",
  last_user_prompt_at: "session.sortByUserPrompt",
  last_opened_at: "session.sortByOpened",
};

/** ISO timestamp on a session for the given sort field (empty if absent). */
export function sessionSortValue(session: Session, field: string): string {
  const v = (session as unknown as Record<string, unknown>)[field];
  return typeof v === "string" ? v : "";
}

/** Numeric epoch-ms for a session under the given sort field (0 if the
 * field is missing/unparseable). Used by the sidebar list comparator so
 * it can order by ANY sort field, not just `updated_at`. */
export function sessionSortNumeric(session: Session, sortField: string): number {
  const parsed = Date.parse(sessionSortValue(session, sortField || "updated_at"));
  return Number.isFinite(parsed) ? parsed : 0;
}

/** Order sessions for the sidebar. Mirrors the backend sort key
 * (`_session_list_sort_key` in main.py): pinned first, then the chosen
 * sort field descending, then stable by original index. `sortField`
 * MUST match the backend `sort_by` or local re-sorts silently scramble
 * the backend's ordering on every mutation. */
export function sortSessionsForList(
  sessions: Session[],
  folderView = false,
  sortField: string = "updated_at",
): Session[] {
  return sessions
    .map((session, index) => ({ session, index }))
    .sort((a, b) => {
      if (folderView) {
        const folderDelta =
          Number(Boolean(b.session.folder_id)) -
          Number(Boolean(a.session.folder_id));
        if (folderDelta !== 0) return folderDelta;
      }
      const pinnedDelta =
        Number(Boolean(b.session.pinned)) - Number(Boolean(a.session.pinned));
      if (pinnedDelta !== 0) return pinnedDelta;
      const modifiedDelta =
        sessionSortNumeric(b.session, sortField) -
        sessionSortNumeric(a.session, sortField);
      if (modifiedDelta !== 0) return modifiedDelta;
      return a.index - b.index;
    })
    .map(({ session }) => session);
}

/** Compact relative time ("3m ago", "2d ago", or a date for older). */
export function timeAgo(t: (key: string) => string, iso?: string): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (isNaN(then)) return "";
  const sec = Math.round((Date.now() - then) / 1000);
  if (sec < 5) return t("session.justNow");
  if (sec < 60) return `${sec}s ago`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  if (day < 7) return `${day}d ago`;
  return new Date(iso).toLocaleDateString();
}
