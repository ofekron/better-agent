import { useTranslation } from "react-i18next";

import { useProjectAggregate } from "../lib/sessionRegistry";

/** Per-project aggregate badges — one independent counter per session
 * status dimension: how many sessions under this project are running,
 * have unread activity, are waiting for the user, or ended their last
 * turn in an unrecovered error. Mirrors the per-session
 * SessionStatusBadge but for the home/projects pane.
 *
 * Counters never mask each other: one errored, unread session shows on
 * both. Order matches the attention order of the per-session badge.
 *
 * `path` + `nodeId` together identify the project (multi-machine
 * topology: two machines can share the same path string).
 */
const COUNTERS = [
  {
    field: "errored_count",
    className: "project-status-errored",
    testid: "project-errored-count",
    one: "projects.errored_1",
    other: "projects.errored_other",
  },
  {
    field: "waiting_for_user_count",
    className: "project-status-waiting",
    testid: "project-waiting-count",
    one: "projects.waiting_1",
    other: "projects.waiting_other",
  },
  {
    field: "running_count",
    className: "project-status-running",
    testid: "project-running-count",
    one: "projects.running_1",
    other: "projects.running_other",
  },
  {
    field: "unread_session_count",
    className: "project-status-unread",
    testid: "project-unread-count",
    one: "projects.unread_1",
    other: "projects.unread_other",
  },
] as const;

export function ProjectStatusBadge({
  path,
  nodeId = "primary",
}: {
  path: string;
  nodeId?: string;
}) {
  const { t } = useTranslation();
  const aggregate = useProjectAggregate(path, nodeId);

  return (
    <>
      {COUNTERS.map(({ field, className, testid, one, other }) => {
        const count = aggregate[field];
        if (count === 0) return null;
        return (
          <span
            key={field}
            className={className}
            title={t(count === 1 ? one : other, { count })}
            data-testid={testid}
            data-project-path={path}
          >
            {count > 99 ? "99+" : count}
          </span>
        );
      })}
    </>
  );
}
