import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { deleteScheduleById, fetchAllSchedules } from "../api";
import type { Schedule } from "../types";
import { eventBus } from "../lib/eventBus";
import { sessionPath } from "../hooks/useRoute";
import { runThreeStateSync } from "../progress/store";
import Icon from "./Icon";

interface Props {
  onBack: () => void;
  onOpenSession: (path: string) => void;
}

const REMOVE_ANIM_MS = 220;

function fmtDateTime(value?: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtInterval(seconds: number): string {
  if (seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

export function SchedulesPage({ onBack, onOpenSession }: Props) {
  const { t } = useTranslation();
  const [schedules, setSchedules] = useState<Schedule[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [removingIds, setRemovingIds] = useState<Set<string>>(new Set());
  const [confirmClearAll, setConfirmClearAll] = useState(false);

  const load = useCallback(async () => {
    try {
      const { schedules } = await fetchAllSchedules();
      setSchedules(schedules);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    // Global ping: any schedule mutated anywhere → refetch snapshot.
    const off = eventBus.subscribe("schedules_changed", () => void load());
    return off;
  }, [load]);

  const removeAnimated = useCallback(
    (ids: string[]) => {
      setRemovingIds((prev) => new Set([...prev, ...ids]));
      window.setTimeout(() => {
        setSchedules((prev) => prev?.filter((s) => !ids.includes(s.id)) ?? prev);
        setRemovingIds((prev) => {
          const next = new Set(prev);
          for (const id of ids) next.delete(id);
          return next;
        });
      }, REMOVE_ANIM_MS);
    },
    [],
  );

  const cancelOne = useCallback(
    async (id: string) => {
      try {
        await runThreeStateSync({
          operationId: `schedule:delete:${id}`,
          action: t("schedules.cancelTitle"),
          reconcile: load,
          mutate: () => deleteScheduleById(id),
        });
        removeAnimated([id]);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [load, removeAnimated, t],
  );

  const clearAll = useCallback(async () => {
    setConfirmClearAll(false);
    const ids = (schedules ?? []).map((s) => s.id);
    const failed: string[] = [];
    await Promise.all(
      ids.map((id) =>
        runThreeStateSync({
          operationId: `schedule:delete:${id}`,
          action: t("schedules.cancelTitle"),
          reconcile: load,
          mutate: () => deleteScheduleById(id),
        }).catch(() => {
          failed.push(id);
        }),
      ),
    );
    removeAnimated(ids.filter((id) => !failed.includes(id)));
    if (failed.length > 0) setError(t("schedulesPage.clearAllFailed"));
  }, [load, schedules, removeAnimated, t]);

  return (
    <div className="schedules-page">
      <header className="schedules-header">
        <button className="an-btn" onClick={onBack}>
          ← {t("common.back")}
        </button>
        <h1>
          <Icon name="clock" size={20} style={{ verticalAlign: "-3px", marginRight: 6 }} />
          {t("schedulesPage.title")}
        </h1>
        <div className="schedules-header-actions">
          {(schedules?.length ?? 0) > 0 &&
            (confirmClearAll ? (
              <>
                <button className="an-btn an-btn-sm schedules-danger" onClick={() => void clearAll()}>
                  {t("schedulesPage.clearAllConfirm")}
                </button>
                <button className="an-btn an-btn-sm" onClick={() => setConfirmClearAll(false)}>
                  {t("app.cancel")}
                </button>
              </>
            ) : (
              <button className="an-btn an-btn-sm" onClick={() => setConfirmClearAll(true)}>
                {t("schedulesPage.clearAll")}
              </button>
            ))}
          <button
            className="an-btn an-btn-sm"
            onClick={() => void load()}
            title={t("schedulesPage.refresh")}
            aria-label={t("schedulesPage.refresh")}
          >
            <Icon name="refresh" size={16} />
          </button>
        </div>
      </header>

      {error && <div className="analytics-error">{error}</div>}

      {schedules === null ? (
        <div className="schedules-empty">{t("common.loading")}</div>
      ) : schedules.length === 0 ? (
        <div className="schedules-empty">{t("schedulesPage.empty")}</div>
      ) : (
        <ul className="schedules-list">
          {schedules.map((s) => (
            <li
              key={s.id}
              className={`schedules-row ${removingIds.has(s.id) ? "removing" : ""}`}
            >
              <div className="schedules-row-main">
                <span className={`schedules-kind schedules-kind-${s.kind}`}>
                  {s.kind === "recurring"
                    ? t("schedulesPage.kindRecurring")
                    : t("schedulesPage.kindOnce")}
                </span>
                <span className="schedules-prompt" title={s.prompt}>
                  {s.prompt}
                </span>
              </div>
              <div className="schedules-row-meta">
                <span className="schedules-meta-item">
                  {t("schedulesPage.nextFire")}: {fmtDateTime(s.fire_at)}
                </span>
                {s.kind === "recurring" && s.interval_seconds != null && (
                  <span className="schedules-meta-item">
                    {t("schedules.interval")} {fmtInterval(s.interval_seconds)}
                  </span>
                )}
                <span className="schedules-meta-item">
                  {t("schedules.lastFired")}:{" "}
                  {s.last_fired_at ? fmtDateTime(s.last_fired_at) : t("schedules.neverFired")}
                </span>
              </div>
              <div className="schedules-row-actions">
                {s.session_exists ? (
                  <button
                    className="an-btn an-btn-sm schedules-session-link"
                    onClick={() => onOpenSession(sessionPath(s.app_session_id))}
                    title={t("schedulesPage.openSession")}
                  >
                    <Icon name="chevron-right" size={14} />
                    {s.session_name || s.app_session_id}
                  </button>
                ) : (
                  <span className="schedules-orphan">{t("schedulesPage.orphanSession")}</span>
                )}
                <button
                  className="an-btn an-btn-sm schedules-danger"
                  onClick={() => void cancelOne(s.id)}
                  title={t("schedules.cancelTitle")}
                  aria-label={t("schedules.cancelTitle")}
                >
                  <Icon name="x" size={14} />
                  {t("schedulesPage.cancel")}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
