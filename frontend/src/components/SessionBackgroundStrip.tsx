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
import type { Schedule } from "../types";

/** Compact strip above the input area surfacing two backend-owned
 * facts about the CURRENTLY VIEWED session (REST snapshot on open,
 * live deltas via the typed eventBus — per the state-ownership rule):
 *
 * 1. Babysitter-lingering background work (bg shells / monitors alive
 *    after the turn ended) + a kill lever.
 * 2. Pending model-created schedules (prompt preview, next fire time,
 *    cancel ✕). No create UI — creation is model-driven.
 *
 * Renders nothing when both surfaces are empty.
 *
 * Mount with `key={sessionId}` — state is per-session and resets by
 * remounting instead of an in-effect clear. */
export function SessionBackgroundStrip({ sessionId }: { sessionId?: string }) {
  const { t } = useTranslation();
  const [lingeringRunIds, setLingeringRunIds] = useState<string[]>([]);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [killing, setKilling] = useState(false);
  const [dismissedSignature, setDismissedSignature] = useState<string | null>(
    null,
  );

  useEffect(() => {
    if (!sessionId) return;
    let stale = false;
    void fetchSessionBackground(sessionId)
      .then((d) => {
        if (!stale) setLingeringRunIds(d.lingering_run_ids ?? []);
      })
      .catch(() => {});
    void fetchSessionSchedules(sessionId)
      .then((d) => {
        if (!stale) setSchedules(d.schedules ?? []);
      })
      .catch(() => {});
    const offLinger = eventBus.subscribe("run_lingering", (p) => {
      if (p.app_session_id !== sessionId) return;
      setLingeringRunIds((prev) =>
        p.lingering
          ? prev.includes(p.run_id)
            ? prev
            : [...prev, p.run_id]
          : prev.filter((id) => id !== p.run_id),
      );
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
      setLingeringRunIds((prev) =>
        prev.filter((id) => !r.killed_run_ids.includes(id)),
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
    const runPart = lingeringRunIds.slice().sort().join(",");
    const schedulePart = schedules.map((s) => s.id).sort().join(",");
    return `${runPart}|${schedulePart}`;
  }, [lingeringRunIds, schedules]);

  if (lingeringRunIds.length === 0 && schedules.length === 0) return null;
  if (dismissedSignature === visibleSignature) return null;

  return (
    <div className="session-bg-strip" data-testid="session-bg-strip">
      <button
        className="session-bg-dismiss"
        onClick={() => setDismissedSignature(visibleSignature)}
        title={t("background.dismiss")}
        aria-label={t("background.dismiss")}
      >
        ×
      </button>
      {lingeringRunIds.length > 0 && (
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
    </div>
  );
}

/** Compact local-time render of the next fire timestamp — time only
 * when it fires today, day+time otherwise. */
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
