import { useCallback, useEffect, useState } from "react";
import { API } from "../api";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";

export interface ProcessEntry {
  pid: number;
  ppid: number | null;
  stat: string;
  state_desc: string;
  cpu_percent: number;
  rss_kb: number;
  elapsed: string;
  command: string;
  alive: boolean;
}

interface ProviderInfo {
  provider_kind: string | null;
  mode: string | null;
  session_id: string | null;
  jsonl_path: string | null;
  run_dir: string | null;
  cancelled: boolean;
  lingering: boolean;
  popen_alive: boolean | null;
  popen_pid: number | null;
}

interface RunDetails {
  run_id: string;
  app_session_id: string;
  kind: string;
  target_message_id: string | null;
  delegation_id: string | null;
  pid: number | null;
  started_at: string;
  last_event_at: string;
  provider: ProviderInfo | null;
  processes: ProcessEntry[];
}

interface Props {
  open: boolean;
  sessionId: string;
  runId: string;
  onClose: () => void;
}

export function fmtMem(kb: number): string {
  if (kb <= 0) return "—";
  if (kb < 1024) return `${kb} KB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${mb.toFixed(1)} MB`;
  return `${(mb / 1024).toFixed(2)} GB`;
}

/**
 * Diagnostic modal: "why is this run still considered running?".
 *
 * Pulls a snapshot from `/api/sessions/{sid}/runs/{run_id}/details` on
 * open and on Refresh — shows the runner PID + every descendant with
 * status / CPU% / RSS / elapsed / cmdline, plus provider-side
 * jsonl_path / run_dir / cancelled / popen_alive. No WS — diagnostic
 * info, polled on demand.
 */
export function RunDetailsModal({ open, sessionId, runId, onClose }: Props) {
  useBackButtonDismiss(open, onClose);
  const [details, setDetails] = useState<RunDetails | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(
        `${API}/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/details`,
        { credentials: "include" },
      );
      if (!r.ok) {
        const body = await r.text().catch(() => "");
        throw new Error(`HTTP ${r.status}: ${body || r.statusText}`);
      }
      const data: RunDetails = await r.json();
      setDetails(data);
    } catch (e) {
      setError((e as Error).message || "Failed to load run details");
    } finally {
      setLoading(false);
    }
  }, [sessionId, runId]);

  useEffect(() => {
    if (!open) return;
    void load();
  }, [open, load]);

  if (!open) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-content"
        style={{ maxWidth: "880px", width: "92vw" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2>
            Run details{details ? ` · ${details.kind}` : ""}
          </h2>
          <button className="modal-close" onClick={onClose}>
            &times;
          </button>
        </div>
        <div className="modal-body" style={{ maxHeight: "70vh", overflow: "auto" }}>
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
          {loading && !details && (
            <div style={{ color: "var(--text-secondary)" }}>Loading…</div>
          )}
          {details && <RunDetailsBody details={details} />}
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

function RunDetailsBody({ details }: { details: RunDetails }) {
  const p = details.provider;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Section title="Run">
        <KV k="run_id" v={details.run_id} mono />
        <KV k="kind" v={details.kind} />
        <KV k="pid" v={details.pid == null ? "—" : String(details.pid)} mono />
        <KV k="started_at" v={details.started_at} mono />
        <KV k="last_event_at" v={details.last_event_at} mono />
        <KV
          k="target_message_id"
          v={details.target_message_id ?? "— (no anchored msg yet)"}
          mono
        />
        {details.delegation_id && (
          <KV k="delegation_id" v={details.delegation_id} mono />
        )}
      </Section>

      {p && (
        <Section title="Provider state">
          <KV k="provider_kind" v={p.provider_kind ?? "—"} />
          <KV k="mode" v={p.mode ?? "—"} />
          <KV k="session_id" v={p.session_id ?? "—"} mono />
          <KV k="jsonl_path" v={p.jsonl_path ?? "—"} mono />
          <KV k="run_dir" v={p.run_dir ?? "—"} mono />
          <KV k="cancelled" v={p.cancelled ? "yes" : "no"} />
          <KV k="lingering" v={p.lingering ? "yes" : "no"} />
          <KV
            k="popen_alive"
            v={
              p.popen_alive === null
                ? "— (no popen)"
                : p.popen_alive
                  ? "yes"
                  : "NO (subprocess gone)"
            }
          />
          <KV
            k="popen_pid"
            v={p.popen_pid == null ? "—" : String(p.popen_pid)}
            mono
          />
        </Section>
      )}

      <Section
        title={`Processes (${details.processes.length})`}
        subtitle="Root PID + every descendant. State R=running, S=sleeping, D=blocked-on-IO, Z=zombie."
      >
        {details.processes.length === 0 ? (
          <div style={{ color: "var(--text-secondary)", fontStyle: "italic" }}>
            No PID stamped yet — runner is still starting (or this entry leaked).
          </div>
        ) : (
          <ProcessTable rows={details.processes} />
        )}
      </Section>
    </div>
  );
}

export function Section({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div
        style={{
          fontSize: "0.78rem",
          fontWeight: 600,
          textTransform: "uppercase",
          letterSpacing: "0.5px",
          color: "var(--text-secondary)",
          marginBottom: 4,
        }}
      >
        {title}
      </div>
      {subtitle && (
        <div
          style={{
            fontSize: "0.72rem",
            color: "var(--text-tertiary, #888)",
            marginBottom: 6,
          }}
        >
          {subtitle}
        </div>
      )}
      <div
        style={{
          border: "1px solid var(--border-color, #2a2a2a)",
          borderRadius: 6,
          padding: 10,
          background: "var(--surface-2, rgba(255,255,255,0.02))",
        }}
      >
        {children}
      </div>
    </div>
  );
}

export function KV({ k, v, mono = false }: { k: string; v: string; mono?: boolean }) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "160px 1fr",
        gap: 8,
        fontSize: "0.8rem",
        lineHeight: 1.6,
      }}
    >
      <div style={{ color: "var(--text-secondary)" }}>{k}</div>
      <div
        style={{
          fontFamily: mono ? "monospace" : undefined,
          color: "var(--text-primary)",
          wordBreak: "break-all",
        }}
      >
        {v}
      </div>
    </div>
  );
}

export function ProcessTable({ rows }: { rows: ProcessEntry[] }) {
  return (
    <div style={{ overflowX: "auto" }}>
      <table
        style={{
          width: "100%",
          borderCollapse: "collapse",
          fontSize: "0.78rem",
          fontFamily: "monospace",
        }}
      >
        <thead>
          <tr style={{ color: "var(--text-secondary)", textAlign: "left" }}>
            <Th>PID</Th>
            <Th>PPID</Th>
            <Th>State</Th>
            <Th align="right">CPU%</Th>
            <Th align="right">RSS</Th>
            <Th>Elapsed</Th>
            <Th>Command</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr
              key={`${r.pid}-${i}`}
              style={{
                borderTop: "1px solid var(--border-color, #2a2a2a)",
                opacity: r.alive ? 1 : 0.55,
              }}
            >
              <Td>{r.pid}</Td>
              <Td>{r.ppid ?? "—"}</Td>
              <Td>
                <span
                  title={r.state_desc}
                  style={{
                    color: !r.alive
                      ? "var(--danger, #d44)"
                      : r.stat.startsWith("Z")
                        ? "var(--danger, #d44)"
                        : r.stat.startsWith("D")
                          ? "var(--warning, #d99)"
                          : undefined,
                  }}
                >
                  {r.stat || "—"} <span style={{ opacity: 0.6 }}>({r.state_desc})</span>
                </span>
              </Td>
              <Td align="right">{r.cpu_percent.toFixed(1)}</Td>
              <Td align="right">{fmtMem(r.rss_kb)}</Td>
              <Td>{r.elapsed || "—"}</Td>
              <Td
                style={{
                  maxWidth: 460,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
                title={r.command}
              >
                {r.command || "—"}
              </Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Th({
  children,
  align = "left",
}: {
  children: React.ReactNode;
  align?: "left" | "right";
}) {
  return (
    <th
      style={{
        padding: "4px 8px",
        textAlign: align,
        fontWeight: 600,
      }}
    >
      {children}
    </th>
  );
}

function Td({
  children,
  align = "left",
  style,
  title,
}: {
  children: React.ReactNode;
  align?: "left" | "right";
  style?: React.CSSProperties;
  title?: string;
}) {
  return (
    <td
      style={{
        padding: "4px 8px",
        textAlign: align,
        ...style,
      }}
      title={title}
    >
      {children}
    </td>
  );
}
