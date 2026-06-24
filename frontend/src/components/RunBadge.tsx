import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { RunInfo } from "../types";
import { RunDetailsModal } from "./RunDetailsModal";

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
