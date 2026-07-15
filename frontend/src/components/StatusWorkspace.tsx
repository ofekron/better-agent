import { useTranslation } from "react-i18next";
import type { RunInfo, Session } from "../types";
import { useSessionMeta } from "../lib/sessionRegistry";
import { SessionStatusBadge } from "./SessionStatusBadge";
import { RunBadge } from "./RunBadge";
import { TodosPanel } from "./TodosPanel";

interface Props {
  session: Session;
  /** In-flight CLI runs for this session (`runStateBySession[sid]`). */
  runs: RunInfo[];
}

/**
 * Live "what is the assistant doing right now" surface for the status
 * view. Reuses the same status primitives the rest of the app uses —
 * `SessionStatusBadge` (monitoring state), `RunBadge` (in-flight runs),
 * `TodosPanel` (current todos/tasks) — so this never drifts from the
 * chat-panel status indicators.
 */
export function StatusWorkspace({ session, runs }: Props) {
  const { t } = useTranslation();
  const meta = useSessionMeta(session.id);
  const state = meta.monitoring_state;
  const todos = session.current_todos ?? [];
  const tasks = session.current_tasks ?? [];

  return (
    <div className="status-workspace" data-testid="status-workspace">
      <header className="status-workspace-header">
        <div className="status-workspace-title">
          <h2>{t("statusView.title")}</h2>
          <SessionStatusBadge sid={session.id} />
        </div>
        <span className={`status-workspace-state status-workspace-state--${state}`}>
          {t(`statusView.state.${state}`)}
        </span>
      </header>

      <section className="status-workspace-section status-workspace-activity">
        <h3>{t("statusView.activity")}</h3>
        {runs.length === 0 ? (
          <p className="status-workspace-empty">{t("statusView.noActivity")}</p>
        ) : (
          <div className="status-workspace-runs">
            {runs.map((run) => (
              <RunBadge key={run.run_id} run={run} sessionId={session.id} />
            ))}
          </div>
        )}
      </section>

      <section className="status-workspace-section status-workspace-todos">
        <h3>{t("statusView.tasks")}</h3>
        {todos.length === 0 && tasks.length === 0 ? (
          <p className="status-workspace-empty">{t("statusView.noTasks")}</p>
        ) : (
          <TodosPanel todos={todos} tasks={tasks} />
        )}
      </section>
    </div>
  );
}
