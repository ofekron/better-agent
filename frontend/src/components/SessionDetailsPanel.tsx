import { useCallback, useEffect, useState } from "react";
import Icon from "./Icon";
import { API } from "../api";
import { eventBus } from "../lib/eventBus";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import {
  ProcessTable,
  Section,
  KV,
  type ProcessEntry,
} from "./RunDetailsModal";

interface ProvenanceRow {
  uuid: string;
  tool: string | null;
  input: unknown;
  why: string;
  ts: string | null;
  msg_id: string | null;
}

interface RunTree {
  run_id: string;
  kind: string;
  pid: number | null;
  started_at: string;
  processes: ProcessEntry[];
}

interface SessionDetails {
  session_id: string;
  monitoring_state: string;
  tracking_guaranteed: boolean;
  provenance: ProvenanceRow[];
  runs: RunTree[];
}

interface Props {
  open: boolean;
  sessionId: string;
  onClose: () => void;
}

const STATE_LABEL: Record<string, string> = {
  active: "Active — executing",
  idle: "Idle — awaiting next prompt",
  blocked_on_user: "Blocked — waiting on you",
  waiting_on_background: "Waiting on background work",
  stopped: "Stopped",
};

const STATE_COLOR: Record<string, string> = {
  active: "var(--success, #3a3)",
  idle: "var(--text-secondary)",
  blocked_on_user: "var(--warning, #d99)",
  waiting_on_background: "var(--info, #39c)",
  stopped: "var(--text-tertiary, #888)",
};

/**
 * Session-level "Details" panel opened from the session menu. Reflects
 * backend state only (REST pull on open, refetch on the
 * `session_monitoring_changed` / `session_provenance_changed` WS pings).
 * Shows the live monitoring state, the provenance log (what ran + WHY),
 * and the escape-proof process tree across the session's runs.
 */
export function SessionDetailsPanel({ open, sessionId, onClose }: Props) {
  useBackButtonDismiss(open, onClose);
  const [data, setData] = useState<SessionDetails | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(
        `${API}/api/sessions/${encodeURIComponent(sessionId)}/details`,
        { credentials: "include" },
      );
      if (!r.ok) {
        const body = await r.text().catch(() => "");
        throw new Error(`HTTP ${r.status}: ${body || r.statusText}`);
      }
      setData((await r.json()) as SessionDetails);
    } catch (e) {
      setError((e as Error).message || "Failed to load session details");
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    if (!open) return;
    void load();
    // Live: refetch when the backend pings monitoring/provenance for THIS
    // session (push, not poll — per the state-ownership rule).
    const refetchIf = (p: { session_id: string }) => {
      if (p.session_id === sessionId) void load();
    };
    const unsubM = eventBus.subscribe("session_monitoring_changed", refetchIf);
    const unsubP = eventBus.subscribe("session_provenance_changed", refetchIf);
    return () => {
      unsubM();
      unsubP();
    };
  }, [open, sessionId, load]);

  if (!open) return null;

  const state = data?.monitoring_state ?? "stopped";

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-content"
        style={{ maxWidth: "880px", width: "92vw" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2>Session details</h2>
          <button className="modal-close" onClick={onClose}>
            &times;
          </button>
        </div>
        <div className="modal-body" style={{ maxHeight: "72vh", overflow: "auto" }}>
          {error && (
            <div
              style={{
                color: "var(--danger, #d44)",
                background: "rgba(220,68,68,0.08)",
                border: "1px solid rgba(220,68,68,0.35)",
                padding: "8px 10px",
                borderRadius: 6,
                marginBottom: 12,
                fontFamily: "monospace",
                fontSize: "0.8rem",
              }}
            >
              {error}
            </div>
          )}
          {loading && !data && (
            <div style={{ color: "var(--text-secondary)" }}>Loading…</div>
          )}
          {data && (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <Section title="State">
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    fontSize: "0.85rem",
                  }}
                >
                  <span
                    style={{
                      width: 9,
                      height: 9,
                      borderRadius: "50%",
                      background: STATE_COLOR[state] ?? "var(--text-secondary)",
                      display: "inline-block",
                    }}
                  />
                  <span>{STATE_LABEL[state] ?? state}</span>
                </div>
                {!data.tracking_guaranteed && (
                  <div
                    style={{
                      marginTop: 8,
                      fontSize: "0.72rem",
                      color: "var(--warning, #d99)",
                    }}
                  >
                    <Icon name="warning" size={13} style={{ verticalAlign: "-2px" }} /> Best-effort process tracking on this platform — a
                    process that fully daemonizes (double-fork / setsid) may
                    not appear. Escape-proof on Linux (cgroup) / Windows (job
                    object).
                  </div>
                )}
              </Section>

              <Section
                title={`Provenance (${data.provenance.length})`}
                subtitle="What the agent ran and why — most recent last."
              >
                {data.provenance.length === 0 ? (
                  <div
                    style={{ color: "var(--text-secondary)", fontStyle: "italic" }}
                  >
                    No tool activity recorded yet.
                  </div>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                    {data.provenance.map((p) => (
                      <ProvenanceItem key={p.uuid} row={p} />
                    ))}
                  </div>
                )}
              </Section>

              {data.runs.map((run) => (
                <Section
                  key={run.run_id}
                  title={`Process tree · ${run.kind}`}
                  subtitle={`run ${run.run_id.slice(0, 8)} · pid ${
                    run.pid ?? "—"
                  }. State R=running, S=sleeping, D=blocked-on-IO, Z=zombie.`}
                >
                  {run.processes.length === 0 ? (
                    <div
                      style={{
                        color: "var(--text-secondary)",
                        fontStyle: "italic",
                      }}
                    >
                      No live processes.
                    </div>
                  ) : (
                    <ProcessTable rows={run.processes} />
                  )}
                </Section>
              ))}
              {data.runs.length === 0 && (
                <Section title="Process tree">
                  <div
                    style={{ color: "var(--text-secondary)", fontStyle: "italic" }}
                  >
                    No live runs for this session.
                  </div>
                </Section>
              )}
            </div>
          )}
        </div>
        <div className="modal-footer">
          <button
            type="button"
            className="btn-secondary"
            onClick={() => void load()}
            disabled={loading}
          >
            {loading ? "Refreshing…" : "Refresh"}
          </button>
          <button type="button" className="btn-primary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

function ProvenanceItem({ row }: { row: ProvenanceRow }) {
  const cmd =
    row.input && typeof row.input === "object" && "command" in row.input
      ? String((row.input as { command: unknown }).command)
      : JSON.stringify(row.input);
  return (
    <div
      style={{
        borderLeft: "2px solid var(--border-color, #2a2a2a)",
        paddingLeft: 10,
      }}
    >
      <div style={{ fontSize: "0.8rem", fontWeight: 600 }}>
        {row.tool ?? "tool"}
        {row.ts && (
          <span
            style={{
              marginLeft: 8,
              fontWeight: 400,
              fontSize: "0.7rem",
              color: "var(--text-tertiary, #888)",
            }}
          >
            {row.ts}
          </span>
        )}
      </div>
      {row.why && (
        <div
          style={{
            fontSize: "0.76rem",
            color: "var(--text-secondary)",
            margin: "2px 0",
          }}
        >
          {row.why}
        </div>
      )}
      <KV k="" v={cmd} mono />
    </div>
  );
}
