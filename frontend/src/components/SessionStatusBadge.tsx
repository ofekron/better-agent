import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { useSessionMeta } from "../lib/sessionRegistry";
import Icon from "./Icon";

/** Single source for "this session needs attention" / running / unread
 * badges. Used wherever a session
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
  const {
    is_running,
    unread_count,
    pending_user_input_count,
    markers,
    testape_active,
    monitoring_state,
    has_error,
  } = useSessionMeta(sid);
  const debouncedRunning = useDebouncedFlag(is_running, 100);
  const markerEntries = Object.entries(markers);
  const awaitingApproval = monitoring_state === "blocked_on_user";
  const waitingOnBackground = monitoring_state === "waiting_on_background";
  const awaitingUserInput = pending_user_input_count > 0;

  if (
    !has_error &&
    !awaitingUserInput &&
    !awaitingApproval &&
    !debouncedRunning &&
    unread_count === 0 &&
    markerEntries.length === 0 &&
    !testape_active
  )
    return null;

  return (
    <>
      {has_error && (
        <span
          className="session-status-error"
          title={t("session.error")}
          data-testid="session-error-dot"
          data-session-id={sid}
        />
      )}
      {awaitingUserInput && (
        <span
          className="session-status-user-input"
          title={t("session.inputNeeded")}
          data-testid="session-user-input-dot"
          data-session-id={sid}
        />
      )}
      {awaitingApproval && (
        <span
          className="session-status-approval"
          title={t("session.awaitingApproval")}
          data-testid="session-approval-pulse"
          data-session-id={sid}
        />
      )}
      {testape_active && (
        <span
          className="session-status-testape"
          title="TestApe active"
          data-testid="session-testape-indicator"
          data-session-id={sid}
        >
          <Icon name="testape" size={12} />
        </span>
      )}
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
          className={
            waitingOnBackground
              ? "session-status-running session-status-background"
              : "session-status-running"
          }
          title={t(
            waitingOnBackground
              ? "session.waitingOnBackground"
              : "session.running",
          )}
          data-testid={
            waitingOnBackground
              ? "session-background-pulse"
              : "session-running-pulse"
          }
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
