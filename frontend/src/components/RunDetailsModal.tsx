import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import { SurfaceClient } from "../adapter/client";
import type { DetailValueKindWire, RunDetailWire } from "../adapter/wire";

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
  popen_alive: boolean | null;
  popen_pid: number | null;
}

/** Legacy `GET /api/sessions/{id}/runs/{run_id}/details` shape — the
 * fields here (process tree, startup phase, provider popen state) have
 * no v2 (ADR 0009) equivalent yet: `turn_manager.py`'s live `_run_state`
 * (pid / startup_phase / stalled_at / last_activity_*) and
 * `process_inspect.inspect_process_tree(pid)` are both outside
 * `store_access`'s store allowlist today (see `runs_adapter.py`'s own
 * `# gap:` comments on `run_detail()`). This interface — and the
 * "Legacy diagnostics" section below that renders it — stays in place,
 * clearly bounded, until those facts are persisted somewhere the v2
 * adapter can read; remove both together at that point, not before. */
interface LegacyRunDetails {
  run_id: string;
  app_session_id: string;
  kind: string;
  target_message_id: string | null;
  delegation_id: string | null;
  pid: number | null;
  started_at: string;
  last_event_at: string;
  provider_kind: string | null;
  startup_phase: string | null;
  startup_expected_activity: string | null;
  startup_phase_started_at: string | null;
  startup_silence_threshold_seconds: number | null;
  stalled_at: string | null;
  last_activity_at: string | null;
  last_activity_kind: string | null;
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

function formatDetailValue(kind: DetailValueKindWire, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (kind === "duration" && typeof value === "number") return `${value.toFixed(1)}s`;
  return String(value);
}

const surfaceClient = new SurfaceClient();

/**
 * Diagnostic modal: "why is this run still considered running?".
 *
 * Primary source: `GET /api/v2/surface/runs/{run_id}` (ADR 0009) —
 * `RunDetail.entries` is a typed, OPEN-ENDED list (`{key, value_kind,
 * value}`); rendered generically below (`GenericEntries`), so a new
 * backend-added entry key shows up with zero client changes.
 *
 * Secondary, clearly-bounded "Legacy diagnostics" section: the fields v2
 * doesn't carry yet (process tree, startup phase, provider popen state —
 * see `LegacyRunDetails`'s docstring for exactly why) still come from the
 * pre-existing `/api/sessions/{sid}/runs/{run_id}/details` route,
 * best-effort (that route 404s for any run outside `turn_manager`'s live
 * in-memory set, e.g. a completed/historic run — silently omitted then,
 * never surfaced as an error, since v2's own section already covers that
 * run honestly). No WS on either source — diagnostic info, polled on
 * open + Refresh.
 */
export function RunDetailsModal({ open, sessionId, runId, onClose }: Props) {
  const { t } = useTranslation();
  useBackButtonDismiss(open, onClose);
  const [v2Detail, setV2Detail] = useState<RunDetailWire | null>(null);
  const [legacy, setLegacy] = useState<LegacyRunDetails | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const envelope = await surfaceClient.fetchRunDetail(runId);
      if (envelope.kind === "ok") {
        setV2Detail(envelope);
      } else if (envelope.kind === "rebuilding") {
        setV2Detail(null);
      } else {
        setV2Detail(null);
        setError(t("runDetails.failedToLoad"));
      }
    } catch (e) {
      setV2Detail(null);
      setError((e as Error).message || t("runDetails.failedToLoad"));
    }
    // Best-effort: only active (turn_manager-tracked) runs have a legacy
    // entry — a 404/network failure here is expected and silent, never
    // promoted to the modal's error banner (the v2 fetch above already
    // owns that).
    try {
      const r = await fetch(
        `${API}/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/details`,
        { credentials: "include" },
      );
      setLegacy(r.ok ? ((await r.json()) as LegacyRunDetails) : null);
    } catch {
      setLegacy(null);
    } finally {
      setLoading(false);
    }
  }, [sessionId, runId, t]);

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
            {t("runDetails.title")}{legacy ? ` · ${legacy.kind}` : ""}
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
          {loading && !v2Detail && !legacy && (
            <div style={{ color: "var(--text-secondary)" }}>{t("progress.loading")}</div>
          )}
          {(v2Detail || legacy) && <RunDetailsBody v2Detail={v2Detail} legacy={legacy} />}
        </div>
        <div className="modal-footer">
          <button
            type="button"
            className="btn-secondary"
            onClick={() => void load()}
            disabled={loading}
          >
            {loading ? t("progress.refreshing") : t("runDetails.refresh")}
          </button>
          <button type="button" className="btn-primary" onClick={onClose}>
            {t("fileViewer.close")}
          </button>
        </div>
      </div>
    </div>
  );
}

function RunDetailsBody({
  v2Detail,
  legacy,
}: {
  v2Detail: RunDetailWire | null;
  legacy: LegacyRunDetails | null;
}) {
  const { t } = useTranslation();
  const p = legacy?.provider ?? null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {v2Detail && (
        <Section title={t("runDetails.v2Summary")}>
          <KV k="run_id" v={v2Detail.summary.run_id} mono />
          <KV k="session_id" v={v2Detail.summary.session_id} mono />
          <KV k="phase" v={v2Detail.summary.phase} />
          {/* `entries` is the open-ended, generically-rendered set — a
              backend-added key (e.g. a future startup_phase fact) shows up
              here automatically, no client change needed. */}
          {v2Detail.entries.map((entry) => (
            <KV
              key={entry.key}
              k={entry.key.replace(/^run\.detail\./, "")}
              v={formatDetailValue(entry.value_kind, entry.value)}
              mono={entry.value_kind === "ref" || entry.value_kind === "duration"}
            />
          ))}
        </Section>
      )}

      {legacy && (
        <>
          <div
            style={{
              fontSize: "0.72rem",
              color: "var(--text-tertiary, #888)",
            }}
          >
            {t("runDetails.legacyDiagnosticsNote")}
          </div>
          <Section title={t("runDetails.legacyDiagnostics")}>
            <KV k="pid" v={legacy.pid == null ? "—" : String(legacy.pid)} mono />
            <KV k="started_at" v={legacy.started_at} mono />
            <KV k="last_event_at" v={legacy.last_event_at} mono />
            <KV k="startup_phase" v={legacy.startup_phase ?? "—"} />
            <KV
              k="startup_expected_activity"
              v={legacy.startup_expected_activity ?? "—"}
            />
            <KV
              k="startup_silence_threshold_seconds"
              v={legacy.startup_silence_threshold_seconds == null
                ? "—"
                : String(legacy.startup_silence_threshold_seconds)}
            />
            <KV k="startup_phase_started_at" v={legacy.startup_phase_started_at ?? "—"} mono />
            <KV k="last_activity_at" v={legacy.last_activity_at ?? "—"} mono />
            <KV k="last_activity_kind" v={legacy.last_activity_kind ?? "—"} />
            <KV k="stalled_at" v={legacy.stalled_at ?? "—"} mono />
            <KV
              k="target_message_id"
              v={legacy.target_message_id ?? t("runDetails.noAnchoredMessage")}
              mono
            />
            {legacy.delegation_id && (
              <KV k="delegation_id" v={legacy.delegation_id} mono />
            )}
          </Section>

          {p && (
            <Section title={t("runDetails.providerState")}>
              <KV k="provider_kind" v={p.provider_kind ?? "—"} />
              <KV k="mode" v={p.mode ?? "—"} />
              <KV k="session_id" v={p.session_id ?? "—"} mono />
              <KV k="jsonl_path" v={p.jsonl_path ?? "—"} mono />
              <KV k="run_dir" v={p.run_dir ?? "—"} mono />
              <KV k="cancelled" v={p.cancelled ? t("common.yes") : t("common.no")} />
              <KV
                k="popen_alive"
                v={
                  p.popen_alive === null
                    ? t("runDetails.noPopen")
                    : p.popen_alive
                      ? t("common.yes")
                      : t("runDetails.subprocessGone")
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
            title={t("runDetails.processes", { count: legacy.processes.length })}
            subtitle={t("runDetails.processesSubtitle")}
          >
            {legacy.processes.length === 0 ? (
              <div style={{ color: "var(--text-secondary)", fontStyle: "italic" }}>
                {t("runDetails.noPid")}
              </div>
            ) : (
              <ProcessTable rows={legacy.processes} />
            )}
          </Section>
        </>
      )}
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
  const { t } = useTranslation();
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
            <Th>{t("runDetails.state")}</Th>
            <Th align="right">CPU%</Th>
            <Th align="right">RSS</Th>
            <Th>{t("runDetails.elapsed")}</Th>
            <Th>{t("runDetails.command")}</Th>
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
