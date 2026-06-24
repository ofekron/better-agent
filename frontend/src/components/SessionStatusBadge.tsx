import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { useSessionMeta } from "../lib/sessionRegistry";

/** Single source for "this session is running" + "this session has N
 * unseen events after the turn ended" badges. Used wherever a session
 * row is rendered — sidebar, home sessions list, browser tab title.
 *
 * State is pulled from `sessionRegistry` (which subscribes to the
 * typed eventBus), so parents only pass identity plus display shape.
 *
 * Running pulse is debounced by 100ms to avoid flicker on
 * turn_start → immediate turn_complete (queued-prompt drain pattern).
 * Unread remains visible while running until the focused-session ack path
 * clears the backend-owned unread count.
 */
export function SessionStatusBadge({
  sid,
  showUnreadCount = false,
}: {
  sid: string;
  showUnreadCount?: boolean;
}) {
  const { t } = useTranslation();
  const { is_running, unread_count, markers } = useSessionMeta(sid);
  const debouncedRunning = useDebouncedFlag(is_running, 100);
  const markerEntries = Object.entries(markers);

  if (!debouncedRunning && unread_count === 0 && markerEntries.length === 0)
    return null;

  return (
    <>
      {markerEntries.map(([extId, m]) => (
        <span
          key={extId}
          className="session-status-marker"
          style={{ backgroundColor: m.color }}
          title={m.tooltip}
          data-testid="session-marker-dot"
          data-extension-id={extId}
          data-session-id={sid}
        />
      ))}
      {debouncedRunning && (
        <span
          className="session-status-running"
          title={t("session.running")}
          data-testid="session-running-pulse"
          data-session-id={sid}
        />
      )}
      {unread_count > 0 && (
        <span
          className={
            showUnreadCount
              ? "session-status-unread session-status-unread-count"
              : "session-status-unread"
          }
          title={t(
            unread_count === 1 ? "session.unread_1" : "session.unread_other",
            { count: unread_count },
          )}
          data-testid="session-unread-count"
          data-session-id={sid}
        >
          {showUnreadCount ? (unread_count > 99 ? "99+" : unread_count) : null}
        </span>
      )}
    </>
  );
}

/** Two-cycle debounce: a flip from false→true settles on the next tick
 * (we WANT the pulse to appear as soon as a turn starts), but a flip
 * from true→false waits `delayMs` so a same-tick turn_start→turn_complete
 * burst doesn't visibly thrash. */
function useDebouncedFlag(value: boolean, delayMs: number): boolean {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(
      () => setDebounced(value),
      value ? 0 : delayMs,
    );
    return () => window.clearTimeout(id);
  }, [value, delayMs]);
  return debounced;
}
