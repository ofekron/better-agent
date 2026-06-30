import { useCallback, useEffect, useMemo, useState } from "react";
import Icon from "./Icon";
import { useTranslation } from "react-i18next";

import {
  cancelSchedule,
  fetchSessionBackground,
  fetchSessionSchedules,
  killSessionBackground,
} from "../api";
import { eventBus } from "../lib/eventBus";
import type { BackgroundRun, Schedule } from "../types";

/** Compact strip above the input area surfacing two backend-owned
 * facts about the CURRENTLY VIEWED session (REST snapshot on open,
 * live deltas via the typed eventBus — per the state-ownership rule):
 *
 * 1. Babysitter-lingering background work (bg shells / monitors alive
 *    after the turn ended) + a kill lever.
 * 2. Pending model-created schedules (prompt preview, next fire time,
 *    cancel ✕). No create UI — creation is model-driven.
 *
 * The (i) button unfolds a details panel showing WHAT is running and
 * WHY (each run's originating prompt, mode, started time; each
 * schedule's created / last-fired / interval). Renders nothing when
 * both surfaces are empty.
 *
 * Mount with `key={sessionId}` — state is per-session and resets by
 * remounting instead of an in-effect clear. */
export function SessionBackgroundStrip({ sessionId }: { sessionId?: string }) {
  const { t } = useTranslation();
  const [lingeringRuns, setLingeringRuns] = useState<BackgroundRun[]>([]);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [killing, setKilling] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [dismissedSignature, setDismissedSignature] = useState<string | null>(
    null,
  );

  useEffect(() => {
    if (!sessionId) return;
    let stale = false;
    const loadRuns = () => {
      void fetchSessionBackground(sessionId)
        .then((d) => {
          if (!stale) setLingeringRuns(d.runs ?? []);
        })
        .catch(() => {});
    };
    loadRuns();
    void fetchSessionSchedules(sessionId)
      .then((d) => {
        if (!stale) setSchedules(d.schedules ?? []);
      })
      .catch(() => {});
    const offLinger = eventBus.subscribe("run_lingering", (p) => {
      if (p.app_session_id !== sessionId) return;
      if (p.lingering) {
        // New run started lingering — WS frame carries run_id + flag
        // only, so refetch to fill the detail the info panel shows.
        loadRuns();
      } else {
        setLingeringRuns((prev) => prev.filter((r) => r.run_id !== p.run_id));
      }
    });
    const offSched = eventBus.subscribe("schedules_updated", (p) => {
      if (p.app_session_id !== sessionId) return;
      setSchedules(p.schedules ?? []);
    });
    return () => {
      stale = true;
      offLinger();
      offSched();
    };
  }, [sessionId]);

  const handleKill = useCallback(async () => {
    if (!sessionId || killing) return;
    setKilling(true);
    try {
      const r = await killSessionBackground(sessionId);
      setLingeringRuns((prev) =>
        prev.filter((run) => !r.killed_run_ids.includes(run.run_id)),
      );
    } finally {
      setKilling(false);
    }
  }, [sessionId, killing]);

  const handleCancelSchedule = useCallback(async (schedule: Schedule) => {
    await cancelSchedule(schedule.id, schedule.app_session_id);
    // Optimistic removal; the schedules_updated frame re-converges.
    setSchedules((prev) => prev.filter((s) => s.id !== schedule.id));
  }, []);

  const visibleSignature = useMemo(() => {
    const runPart = lingeringRuns.map((r) => r.run_id).slice().sort().join(",");
    const schedulePart = schedules.map((s) => s.id).sort().join(",");
    return `${runPart}|${schedulePart}`;
  }, [lingeringRuns, schedules]);

  if (lingeringRuns.length === 0 && schedules.length === 0) return null;
  if (dismissedSignature === visibleSignature) return null;

  return (
    <div className="session-bg-strip" data-testid="session-bg-strip">
      <div className="session-bg-actions">
        <button
          className="session-bg-icon-btn"
          onClick={() => setExpanded((v) => !v)}
          title={t("background.info")}
          aria-label={t("background.info")}
          aria-expanded={expanded}
          data-testid="background-info-btn"
        >
          <Icon name="info" size={14} />
        </button>
        <button
          className="session-bg-icon-btn session-bg-dismiss"
          onClick={() => setDismissedSignature(visibleSignature)}
          title={t("background.dismiss")}
          aria-label={t("background.dismiss")}
        >
          ×
        </button>
      </div>
      {lingeringRuns.length > 0 && (
        <div className="session-bg-bar" data-testid="background-work-bar">
          <span className="session-bg-label">{t("background.running")}</span>
          <button
            className="queued-cancel-btn"
            onClick={() => void handleKill()}
            disabled={killing}
            data-testid="background-kill-btn"
          >
            {killing ? t("background.killing") : t("background.kill")}
          </button>
        </div>
      )}
      {schedules.length > 0 && (
        <div className="session-schedules" data-testid="session-schedules">
          {schedules.map((s) => (
            <span key={s.id} className="session-schedule-pill" title={s.prompt}>
              <span className="session-schedule-prompt">{s.prompt}</span>
              <span className="session-schedule-time">
                {s.kind === "recurring" ? <Icon name="refresh" size={11} style={{ verticalAlign: "-1px", marginRight: 3 }} /> : null}
                {formatFireAt(s.fire_at)}
              </span>
              <button
                className="session-schedule-cancel"
                onClick={() => void handleCancelSchedule(s)}
                title={t("schedules.cancelTitle")}
                aria-label={t("schedules.cancelTitle")}
              >
                <Icon name="x" size={14} />
              </button>
            </span>
          ))}
        </div>
      )}
      {expanded && (
        <div className="session-bg-details" data-testid="session-bg-details">
          {lingeringRuns.map((run) => (
            <div key={run.run_id} className="session-bg-detail-row">
              <div className="session-bg-detail-prompt">
                {run.prompt || <span className="session-bg-detail-empty">{run.run_id}</span>}
              </div>
              <div className="session-bg-detail-meta">
                {run.started_at && (
                  <span>
                    {t("background.started")}: {formatFireAt(run.started_at)}
                  </span>
                )}
                {run.mode && (
                  <span>
                    {t("background.mode")}: {run.mode}
                  </span>
                )}
              </div>
            </div>
          ))}
          {schedules.map((s) => (
            <div key={s.id} className="session-bg-detail-row">
              <div className="session-bg-detail-prompt">{s.prompt}</div>
              <div className="session-bg-detail-meta">
                {s.created_at && (
                  <span>
                    {t("schedules.created")}: {formatFireAt(s.created_at)}
                  </span>
                )}
                <span>
                  {t("schedules.lastFired")}:{" "}
                  {s.last_fired_at ? formatFireAt(s.last_fired_at) : t("schedules.neverFired")}
                </span>
                {s.kind === "recurring" && s.interval_seconds != null && (
                  <span>
                    {t("schedules.interval")}: {formatInterval(s.interval_seconds)}
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Compact local-time render of a timestamp — time only when it falls
 * today, day+time otherwise. Used for run started_at and schedule
 * created / last-fired / next-fire alike. */
function formatFireAt(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay
    ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}

/** Human interval label for a recurring schedule's seconds value:
 * 60 -> 1m, 3600 -> 1h, 86400 -> 1d, 7200 -> 2h, etc. */
function formatInterval(seconds: number): string {
  if (seconds <= 0) return String(seconds);
  const units: [number, string][] = [
    [86400, "d"],
    [3600, "h"],
    [60, "m"],
  ];
  for (const [secs, label] of units) {
    if (seconds % secs === 0) return `${seconds / secs}${label}`;
  }
  return `${Math.round(seconds / 60)}m`;
}
