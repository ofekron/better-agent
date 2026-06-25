import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import Icon from "./Icon";

export interface ScheduleSendPayload {
  prompt: string;
  kind: "once" | "recurring";
  /** Naive local ISO datetime — schedule_store rejects tz-aware values. */
  fire_at: string;
  interval_seconds: number | null;
}

type RecurringKey = "once" | "hourly" | "daily";

const RECURRING: Record<RecurringKey, { kind: "once" | "recurring"; interval: number | null }> = {
  once: { kind: "once", interval: null },
  hourly: { kind: "recurring", interval: 3600 },
  daily: { kind: "recurring", interval: 86400 },
};

/** Formats a Date as the value of <input type="datetime-local">:
 * `YYYY-MM-DDTHH:mm` in the host's local zone (no offset). */
function toLocalInputValue(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

/** Picker that turns the current draft into a backend-owned scheduled
 * prompt instead of sending it immediately. The parent owns the actual
 * create call (returns true on success); this component only validates
 * the time and builds the payload. */
export function ScheduleSendPopover({
  prompt,
  anchorRef,
  onClose,
  onSchedule,
}: {
  prompt: string;
  anchorRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
  onSchedule: (payload: ScheduleSendPayload) => Promise<boolean> | boolean;
}) {
  const { t } = useTranslation();
  const defaultAt = useMemo(() => {
    const d = new Date(Date.now() + 60 * 60 * 1000);
    d.setMinutes(0, 0, 0);
    return toLocalInputValue(d);
  }, []);
  const [at, setAt] = useState(defaultAt);
  const [recurring, setRecurring] = useState<RecurringKey>("once");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [dragOffset, setDragOffset] = useState({ x: 0, y: 0 });
  const dragState = useRef<{ startX: number; startY: number; origX: number; origY: number } | null>(null);
  const [dragging, setDragging] = useState(false);

  const handleDragPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest(".schedule-popover-close")) return;
    dragState.current = {
      startX: e.clientX,
      startY: e.clientY,
      origX: dragOffset.x,
      origY: dragOffset.y,
    };
    setDragging(true);
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const handleDragPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragState.current) return;
    const { startX, startY, origX, origY } = dragState.current;
    setDragOffset({ x: origX + (e.clientX - startX), y: origY + (e.clientY - startY) });
  };
  const endDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragState.current) return;
    dragState.current = null;
    setDragging(false);
    e.currentTarget.releasePointerCapture(e.pointerId);
  };

  // Close on outside click / Escape, like the other composer popovers.
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        panelRef.current?.contains(target) ||
        anchorRef.current?.contains(target)
      )
        return;
      onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose, anchorRef]);

  const trimmed = prompt.trim();
  const canSubmit = !!trimmed && !!at && !submitting;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    const { kind, interval } = RECURRING[recurring];
    setSubmitting(true);
    setError(null);
    try {
      const ok = await onSchedule({
        prompt: trimmed,
        kind,
        fire_at: at,
        interval_seconds: interval,
      });
      if (ok) onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      ref={panelRef}
      className="schedule-popover"
      data-testid="schedule-popover"
      role="dialog"
      aria-label={t("schedule.title")}
      style={{
        transform: `translate(${dragOffset.x}px, ${dragOffset.y}px)`,
        transition: dragging ? "none" : undefined,
      }}
    >
      <div
        className="schedule-popover-header"
        data-testid="schedule-popover-drag-handle"
        onPointerDown={handleDragPointerDown}
        onPointerMove={handleDragPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      >
        <Icon name="clock" size={14} />
        <span>{t("schedule.title")}</span>
        <button
          className="schedule-popover-close"
          onClick={onClose}
          aria-label={t("app.cancel")}
        >
          <Icon name="x" size={14} />
        </button>
      </div>

      <p className="schedule-popover-prompt" title={trimmed}>
        {trimmed}
      </p>

      <label className="schedule-popover-field">
        <span>{t("schedule.fireAt")}</span>
        <input
          type="datetime-local"
          value={at}
          onChange={(e) => setAt(e.target.value)}
          data-testid="schedule-fire-at"
        />
      </label>

      <label className="schedule-popover-field">
        <span>{t("schedule.repeat")}</span>
        <select
          value={recurring}
          onChange={(e) => setRecurring(e.target.value as RecurringKey)}
          data-testid="schedule-repeat"
        >
          <option value="once">{t("schedule.repeatOnce")}</option>
          <option value="hourly">{t("schedule.repeatHourly")}</option>
          <option value="daily">{t("schedule.repeatDaily")}</option>
        </select>
      </label>

      {error && <div className="schedule-popover-error">{error}</div>}

      <button
        className="schedule-popover-submit"
        onClick={() => void handleSubmit()}
        disabled={!canSubmit}
        data-testid="schedule-submit"
      >
        {submitting ? t("schedule.scheduling") : t("schedule.scheduleSend")}
      </button>
    </div>
  );
}
