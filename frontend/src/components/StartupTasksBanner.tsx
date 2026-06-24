import { useEffect, useState } from "react";
import Icon from "./Icon";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import type { StartupTask } from "../types";

/** Non-blocking banner that surfaces backend startup work (migrations,
 * recovery scans, jsonl replay) so the user sees what's running and
 * isn't confused by sessions whose state is still being rehydrated.
 *
 * Authoritative state lives in the backend's `startup_task_registry`;
 * this component pulls a snapshot from `GET /api/startup_tasks` on
 * mount and applies live deltas from the `startup_task_changed` window
 * event (dispatched by `useWebSocket` when the WS frame arrives).
 *
 * Convergence rule: WS is the live truth, REST is a backfill. The
 * mount-time REST merge only fills in tasks WS hasn't already
 * delivered — overwriting a fresher WS state with a stale REST
 * snapshot would flip a `done` row back to `running`.
 *
 * `done` rows are evicted N seconds after their `finished_at`
 * (server-stamped). Running and failed rows stay until completion,
 * backend clear, or local popup dismissal.
 */

const DONE_FADE_MS = 1500;
const EVICTION_TICK_MS = 500;

export function StartupTasksBanner() {
  const { t } = useTranslation();
  const [tasks, setTasks] = useState<Record<string, StartupTask>>({});

  useEffect(() => {
    let cancelled = false;
    fetch(`${API}/api/startup_tasks`)
      .then((r) => r.json())
      .then((arr: unknown) => {
        if (cancelled) return;
        // Defensive guard: `fetch` doesn't reject on 4xx/5xx, so an
        // auth-gated 401 (`{"detail":"unauthenticated"}` from the
        // backend middleware) or any future schema drift would land
        // here as a non-array. Iterating it with `for-of` inside the
        // setState updater below throws `TypeError: ... is not
        // iterable` ON THE NEXT RENDER (not in this .then), which
        // escapes the surrounding `.catch` and propagates to the
        // top-level error boundary — blanking the whole app. A single
        // `Array.isArray` check covers 401, 500, 200-with-wrong-shape,
        // and any future drift. (This fix was previously landed in
        // ea0b0e5 and accidentally reverted by 4458f43 — do not
        // remove without the regression test in
        // tests/startup-tasks-banner.test.tsx also being updated.)
        if (!Array.isArray(arr)) return;
        // Merge: only insert tasks WS hasn't already populated.
        // Overwriting a WS-known task with a REST row would clobber
        // any state transition (running → done) that arrived between
        // the REST request and its response.
        setTasks((prev) => {
          const next = { ...prev };
          for (const task of arr as StartupTask[]) {
            if (!(task.id in next)) {
              next[task.id] = task;
            }
          }
          return next;
        });
      })
      .catch(() => {
        // Backend hasn't accepted REST yet (process still booting).
        // The WS handler populates as events arrive; no retry needed.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    function onDelta(ev: Event) {
      const detail = (ev as CustomEvent).detail as
        | { cleared?: boolean; task?: StartupTask }
        | undefined;
      if (!detail) return;
      if (detail.cleared) {
        setTasks({});
        return;
      }
      const task = detail.task;
      // Symmetric defense-in-depth with the REST path: a malformed
      // delta (`{task: null}`, `{task: {}}`, missing id) would write
      // `[undefined]` into the map and silently corrupt the dataset.
      // Drop anything without an `id`.
      if (!task?.id) return;
      setTasks((prev) => ({ ...prev, [task.id]: task }));
    }
    window.addEventListener("startup_task_changed", onDelta);
    return () => window.removeEventListener("startup_task_changed", onDelta);
  }, []);

  // Time-based eviction: every tick, drop `done` rows older than the
  // fade window. Using `finished_at` (server clock) rather than a
  // setTimeout-per-task means the schedule is robust to WS updates
  // arriving between effect runs. Failed rows stay until manually
  // dismissed.
  useEffect(() => {
    const interval = window.setInterval(() => {
      setTasks((prev) => {
        const now = Date.now();
        let changed = false;
        const next: Record<string, StartupTask> = {};
        for (const [id, task] of Object.entries(prev)) {
          if (task.state === "done" && task.finished_at) {
            const finishedMs = Date.parse(task.finished_at);
            if (!Number.isNaN(finishedMs) && now - finishedMs > DONE_FADE_MS) {
              changed = true;
              continue;
            }
          }
          next[id] = task;
        }
        return changed ? next : prev;
      });
    }, EVICTION_TICK_MS);
    return () => window.clearInterval(interval);
  }, []);

  const visible = Object.values(tasks).filter(
    (task) => task.state === "running" || task.state === "failed",
  );
  if (visible.length === 0) return null;

  const dismissVisible = () => {
    const visibleIds = new Set(visible.map((task) => task.id));
    setTasks((prev) => {
      const next: Record<string, StartupTask> = {};
      for (const [id, task] of Object.entries(prev)) {
        if (!visibleIds.has(id)) next[id] = task;
      }
      return next;
    });
  };

  // Future task ids ship the i18n key as `label`; render the
  // localized string when known, else humanize the key tail so the
  // user sees "v3 migration" rather than "startup_tasks.v3_migration".
  const renderLabel = (label: string) =>
    t(label, {
      defaultValue: label.split(".").pop()!.replace(/_/g, " "),
    });

  return (
    <div className="startup-tasks-banner" role="status">
      <div className="startup-tasks-banner-header">
        <span>{t("startup_tasks.banner_title")}</span>
        <button
          className="startup-tasks-banner-close"
          onClick={dismissVisible}
          aria-label={t("startup_tasks.dismiss")}
          title={t("startup_tasks.dismiss")}
        >
          ×
        </button>
      </div>
      <ul className="startup-tasks-banner-list">
        {visible.map((task) => (
          <li
            key={task.id}
            className={`startup-tasks-banner-row startup-tasks-banner-row-${task.state}`}
          >
            <span className="startup-tasks-banner-spinner" aria-hidden>
              {task.state === "running" ? <Icon name="refresh" size={12} /> : <Icon name="warning" size={12} />}
            </span>
            <span className="startup-tasks-banner-text">
              {renderLabel(task.label)}
              {task.state === "failed" && task.error ? (
                <span className="startup-tasks-banner-error"> — {task.error}</span>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
