import { useCallback, useEffect, useMemo, useState } from "react";
import Icon from "./Icon";
import { useTranslation } from "react-i18next";

import { cancelSchedule, fetchSessionSchedules } from "../api";
import { eventBus } from "../lib/eventBus";
import type { Schedule } from "../types";

/** Compact strip above the input area surfacing pending model-created
 * schedules for the CURRENTLY VIEWED session (REST snapshot on open,
 * live deltas via the typed eventBus — per the state-ownership rule):
 * prompt preview, next fire time, cancel ✕. No create UI — creation is
 * model-driven.
 *
 * The (i) button unfolds a details panel showing each schedule's
 * created / last-fired / interval. Renders nothing when empty.
 *
 * Mount with `key={sessionId}` — state is per-session and resets by
 * remounting instead of an in-effect clear. */
export function SessionBackgroundStrip({ sessionId }: { sessionId?: string }) {
  const { t } = useTranslation();
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [dismissedSignature, setDismissedSignature] = useState<string | null>(
    null,
  );

  useEffect(() => {
    if (!sessionId) return;
    let stale = false;
    void fetchSessionSchedules(sessionId)
      .then((d) => {
        if (!stale) setSchedules(d.schedules ?? []);
      })
      .catch(() => {});
    const offSched = eventBus.subscribe("schedules_updated", (p) => {
      if (p.app_session_id !== sessionId) return;
      setSchedules(p.schedules ?? []);
    });
    return () => {
      stale = true;
      offSched();
    };
  }, [sessionId]);

  const handleCancelSchedule = useCallback(async (schedule: Schedule) => {
    await cancelSchedule(schedule.id, schedule.app_session_id);
    // Optimistic removal; the schedules_updated frame re-converges.
    setSchedules((prev) => prev.filter((s) => s.id !== schedule.id));
  }, []);

  const visibleSignature = useMemo(
    () => schedules.map((s) => s.id).sort().join(","),
    [schedules],
  );

  if (schedules.length === 0) return null;
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
      {expanded && (
        <div className="session-bg-details" data-testid="session-bg-details">
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
 * today, day+time otherwise. Used for schedule created / last-fired /
 * next-fire alike. */
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
