import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import type { RunInfo } from "../types";
import { useRunSummary } from "../hooks/useRunSummary";
import { RunDetailsModal } from "./RunDetailsModal";
import { runtimeKindLabelKey } from "./modelPicker";

/**
 * Animated "running" badge for one in-flight CLI run. Reflects the
 * backend's authoritative `run_state` — always visible while the
 * backend says the run is running, gone the moment it isn't.
 *
 * Shows: kind label (manager / native / worker), an optional worker
 * description (when we have it via the targeting message's panel),
 * and elapsed time since `started_at`. The local clock tick makes the
 * counter live without any backend chatter.
 *
 * Clickable when `sessionId` is provided — opens `RunDetailsModal`
 * with the runner PID, descendant process tree (CPU%, status, cmd),
 * and provider-side jsonl_path / run_dir so the user can see WHY the
 * backend still considers this run alive.
 *
 * Stalled honesty (ADR 0009): the message-anchored `run: RunInfo` prop
 * (`target_message_id`) has no v2 equivalent — `RunSummary` carries no
 * message anchor at all (a confirmed backend gap, not an oversight; see
 * this package's report) — so `run` itself stays legacy-sourced. Layered
 * on top, `useRunSummary` correlates the SAME `run_id` against the v2
 * `runs` feed's disk-heartbeat-derived `phase`: `run.startup_phase ===
 * "stalled"` (legacy's `turn_manager`-heuristic detection, unchanged) OR
 * v2's `phase === "stalled"` (heartbeat-file-derived, independent of
 * `turn_manager`'s in-memory state — e.g. still honest across a backend
 * restart that resets `turn_manager` but not the on-disk `runner_alive`
 * sentinel) renders the stalled card — the union of two honest signals,
 * never a regression from either alone.
 */
export function RunBadge({
  run,
  workerLabel,
  sessionId,
}: {
  run: RunInfo;
  /** Optional: pretty name for a worker run (looked up from the
   * assistant message's worker panels by `delegation_id`). */
  workerLabel?: string;
  /** When set, the badge becomes a button that opens the run details
   * modal. Omitted in contexts that don't have a session id at hand. */
  sessionId?: string;
}) {
  const { t } = useTranslation();
  const [now, setNow] = useState(() => Date.now());
  const [modalOpen, setModalOpen] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState(false);
  const v2Summary = useRunSummary(sessionId, run.run_id);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const start = Date.parse(run.started_at);
  const elapsedSec = Number.isFinite(start)
    ? Math.max(0, Math.floor((now - start) / 1000))
    : 0;

  const label =
    run.kind === "worker"
      ? workerLabel
        ? `worker · ${workerLabel}`
        : "worker"
      : run.kind === "native"
      ? ""
      : run.kind;

  // Legacy's own `startup_phase` heuristic (turn_manager, in-memory,
  // richer — carries a real threshold + started_at) stays primary; v2's
  // heartbeat-file-derived `phase` (runs_adapter.py, disk-backed,
  // survives a backend restart) is an ADDITIONAL honest signal — see the
  // component docstring. Rendered distinguishably: the legacy signal
  // keeps the existing rich card verbatim, the v2-only signal gets a
  // minimal variant (no fabricated threshold text to show).
  const legacyStalled = run.startup_phase === "stalled";
  const v2Stalled = v2Summary?.phase === "stalled";
  const isStalled = legacyStalled || v2Stalled;
  const heartbeatSilenceSec =
    v2Summary?.last_heartbeat_at != null
      ? Math.max(0, Math.floor(now / 1000 - v2Summary.last_heartbeat_at))
      : null;
  const cancel = async () => {
    if (!sessionId || cancelling) return;
    setCancelling(true);
    setCancelError(false);
    try {
      const response = await fetch(
        `${API}/api/sessions/${encodeURIComponent(sessionId)}/stop`,
        { method: "POST", credentials: "include" },
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
    } catch {
      setCancelError(true);
      setCancelling(false);
    }
  };

  if (isStalled && !legacyStalled) {
    // v2-only signal: legacy's own heuristic doesn't (yet) agree, so
    // there is no threshold/started_at to show honestly — a minimal
    // variant reusing the same `turn-stalled-card` styling idiom, never
    // fabricating the richer legacy copy's numbers.
    return (
      <div className="turn-stalled-card" role="status" aria-live="polite">
        <div className="turn-stalled-card__icon" aria-hidden="true">!</div>
        <div className="turn-stalled-card__body">
          <strong>{t("runBadge.stalledTitleUnknown")}</strong>
          <span>
            {t("runBadge.stalledDescriptionHeartbeat", {
              seconds: heartbeatSilenceSec ?? 0,
            })}
          </span>
          {cancelError && (
            <span className="turn-stalled-card__error">
              {t("runBadge.cancelFailed")}
            </span>
          )}
          <div className="turn-stalled-card__actions">
            <button
              type="button"
              className="turn-stalled-card__cancel"
              onClick={() => void cancel()}
              disabled={!sessionId || cancelling}
            >
              {cancelling ? t("runBadge.cancelling") : t("runBadge.cancel")}
            </button>
            {sessionId && (
              <button
                type="button"
                className="turn-stalled-card__details"
                onClick={() => setModalOpen(true)}
              >
                {t("runBadge.details")}
              </button>
            )}
          </div>
        </div>
        {sessionId && (
          <RunDetailsModal
            open={modalOpen}
            sessionId={sessionId}
            runId={run.run_id}
            onClose={() => setModalOpen(false)}
          />
        )}
      </div>
    );
  }

  if (legacyStalled) {
    const rawKind = run.provider_kind || "";
    const capitalized = rawKind
      ? rawKind.charAt(0).toUpperCase() + rawKind.slice(1)
      : t("runBadge.providerFallback");
    const provider = rawKind
      ? t(runtimeKindLabelKey(rawKind), { defaultValue: capitalized })
      : capitalized;
    const threshold = run.startup_silence_threshold_seconds ?? 0;
    return (
      <div className="turn-stalled-card" role="status" aria-live="polite">
        <div className="turn-stalled-card__icon" aria-hidden="true">!</div>
        <div className="turn-stalled-card__body">
          <strong>{t("runBadge.stalledTitle", { provider })}</strong>
          <span>
            {t("runBadge.stalledDescription", { seconds: threshold })}
          </span>
          {cancelError && (
            <span className="turn-stalled-card__error">
              {t("runBadge.cancelFailed")}
            </span>
          )}
          <div className="turn-stalled-card__actions">
            <button
              type="button"
              className="turn-stalled-card__cancel"
              onClick={() => void cancel()}
              disabled={!sessionId || cancelling}
            >
              {cancelling ? t("runBadge.cancelling") : t("runBadge.cancel")}
            </button>
            <button
              type="button"
              className="turn-stalled-card__retry"
              disabled
              title={t("runBadge.retryAfterCancelHint")}
            >
              {t("runBadge.retry")}
            </button>
            {sessionId && (
              <button
                type="button"
                className="turn-stalled-card__details"
                onClick={() => setModalOpen(true)}
              >
                {t("runBadge.details")}
              </button>
            )}
          </div>
          <span className="turn-stalled-card__hint">
            {t("runBadge.retryAfterCancelHint")}
          </span>
        </div>
        {sessionId && (
          <RunDetailsModal
            open={modalOpen}
            sessionId={sessionId}
            runId={run.run_id}
            onClose={() => setModalOpen(false)}
          />
        )}
      </div>
    );
  }

  const inner = (
    <>
      <span className="run-badge-pulse" aria-hidden="true" />
      <span className="run-badge-label">
        {label ? `${label} ${t("runBadge.running")}` : "Running..."}
      </span>
      <span className="run-badge-age">{elapsedSec}s</span>
    </>
  );

  if (sessionId) {
    return (
      <>
        <button
          type="button"
          className="run-badge run-badge-clickable"
          data-kind={run.kind}
          aria-live="polite"
          onClick={() => setModalOpen(true)}
          title="Show run details (PIDs, status, CPU…)"
        >
          {inner}
        </button>
        <RunDetailsModal
          open={modalOpen}
          sessionId={sessionId}
          runId={run.run_id}
          onClose={() => setModalOpen(false)}
        />
      </>
    );
  }

  return (
    <span className="run-badge" data-kind={run.kind} aria-live="polite">
      {inner}
    </span>
  );
}

/**
 * Stack of `RunBadge`s for one bubble. `targetMessageId` filters
 * `runs` down to the ones that name this message (or null, for
 * pre-lazy turns where no assistant message exists yet).
 */
export function RunBadgeStack({
  runs,
  workerLabelByDelegation,
  sessionId,
}: {
  runs: RunInfo[];
  workerLabelByDelegation?: Map<string, string>;
  /** Forwarded to each `RunBadge` to make it clickable. */
  sessionId?: string;
}) {
  if (runs.length === 0) return null;
  return (
    <div className="run-badge-stack">
      {runs.map((r) => (
        <RunBadge
          key={r.run_id}
          run={r}
          sessionId={sessionId}
          workerLabel={
            r.delegation_id
              ? workerLabelByDelegation?.get(r.delegation_id)
              : undefined
          }
        />
      ))}
    </div>
  );
}
