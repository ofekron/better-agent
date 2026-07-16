import { useTranslation } from "react-i18next";

import { useProjectAggregate } from "../lib/sessionRegistry";

/** Backend-owned project counts plus the transient TestApe running overlay.
 *
 * `path` + `nodeId` together identify the project (multi-machine
 * topology: two machines can share the same path string).
 */
export function ProjectStatusBadge({
  path,
  nodeId = "primary",
  runningCount = 0,
  unreadSessionCount = 0,
  waitingForUserCount = 0,
  erroredCount = 0,
}: {
  path: string;
  nodeId?: string;
  runningCount?: number;
  unreadSessionCount?: number;
  waitingForUserCount?: number;
  erroredCount?: number;
}) {
  const { t } = useTranslation();
  const { running_count: testapeRunningCount } = useProjectAggregate(
    path,
    nodeId,
  );
  const running_count = runningCount + testapeRunningCount;
  const unread_session_count = unreadSessionCount;
  const waiting_for_user_count = waitingForUserCount;
  const errored_count = erroredCount;

  if (
    running_count === 0 &&
    unread_session_count === 0 &&
    waiting_for_user_count === 0 &&
    errored_count === 0
  )
    return null;

  return (
    <>
      {running_count > 0 && (
        <span
          className="project-status-running"
          title={t(
            running_count === 1
              ? "projects.running_1"
              : "projects.running_other",
            { count: running_count },
          )}
          data-testid="project-running-count"
          data-project-path={path}
        >
          {running_count}
        </span>
      )}
      {unread_session_count > 0 && (
        <span
          className="project-status-unread"
          title={t(
            unread_session_count === 1
              ? "projects.unread_1"
              : "projects.unread_other",
            { count: unread_session_count },
          )}
          data-testid="project-unread-count"
          data-project-path={path}
        >
          {unread_session_count > 99 ? "99+" : unread_session_count}
        </span>
      )}
      {waiting_for_user_count > 0 && (
        <span
          className="project-status-waiting"
          title={t(
            waiting_for_user_count === 1
              ? "projects.waiting_1"
              : "projects.waiting_other",
            { count: waiting_for_user_count },
          )}
          data-testid="project-waiting-count"
          data-project-path={path}
        >
          {waiting_for_user_count > 99 ? "99+" : waiting_for_user_count}
        </span>
      )}
      {errored_count > 0 && (
        <span
          className="project-status-errored"
          title={t(
            errored_count === 1
              ? "projects.errored_1"
              : "projects.errored_other",
            { count: errored_count },
          )}
          data-testid="project-errored-count"
          data-project-path={path}
        >
          {errored_count > 99 ? "99+" : errored_count}
        </span>
      )}
    </>
  );
}
