import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { fetchAnalytics, type AnalyticsReport } from "../api";
import Icon from "./Icon";

interface Props {
  onBack: () => void;
}

const PALETTE = ["#7b68ee", "#4ac2c0", "#e8a838", "#e0556a", "#56b6c2", "#a78bfa", "#f06292"];
const BAR_COLOR = "#7b68ee";

type Preset = "7d" | "30d" | "90d" | "all" | "custom";

function toDateStr(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}
function daysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return toDateStr(d);
}
function fmt(n: number): string {
  if (!Number.isFinite(n)) return "—";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}
function fmtMs(ms: number): string {
  if (!ms) return "—";
  if (ms >= 60000) return (ms / 60000).toFixed(1) + "m";
  if (ms >= 1000) return (ms / 1000).toFixed(1) + "s";
  return Math.round(ms) + "ms";
}
function tickFormatter(t: string, granularity: string): string {
  if (granularity === "month") return t;
  if (granularity === "hour") return t.split(" ")[1] ?? t;
  return t.length >= 10 ? t.slice(5) : t;
}
function tooltipFmt(value: unknown, name: unknown): [string, string] {
  return [fmt(Number(value) || 0), String(name)];
}

const TOOLTIP_STYLE = {
  background: "var(--bg-tertiary)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  color: "var(--text-primary)",
  fontSize: 12,
} as const;

export function AnalyticsPage({ onBack }: Props) {
  const { t } = useTranslation();
  const [preset, setPreset] = useState<Preset>("30d");
  const [customStart, setCustomStart] = useState(daysAgo(30));
  const [customEnd, setCustomEnd] = useState(toDateStr(new Date()));
  const [report, setReport] = useState<AnalyticsReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const { start, end } = useMemo(() => {
    if (preset === "custom") return { start: customStart, end: customEnd };
    if (preset === "all") return { start: "2000-01-01", end: toDateStr(new Date()) };
    const n = preset === "7d" ? 7 : preset === "90d" ? 90 : 30;
    return { start: daysAgo(n), end: toDateStr(new Date()) };
  }, [preset, customStart, customEnd]);

  // Single fetcher for both the range-change effect and the refresh button.
  const reqIdRef = useRef(0);
  const load = useCallback(async () => {
    if (!start || !end) return;
    const myId = ++reqIdRef.current;
    setLoading(true);
    setError(null);
    try {
      const data = await fetchAnalytics(start, end);
      if (reqIdRef.current !== myId) return;
      setReport(data);
    } catch (e) {
      if (reqIdRef.current !== myId) return;
      setError(e instanceof Error ? e.message : String(e));
      setReport(null);
    } finally {
      if (reqIdRef.current === myId) setLoading(false);
    }
  }, [start, end]);

  useEffect(() => {
    void load();
  }, [load]);

  const granularity = report?.range.granularity ?? "day";
  const presets: Preset[] = ["7d", "30d", "90d", "all", "custom"];
  const noData = loading ? t("common.loading") : t("analytics.noData");

  return (
    <div className="analytics-page">
      <header className="analytics-header">
        <button className="an-btn" onClick={onBack}>
          ← {t("common.back")}
        </button>
        <h1>
          <Icon name="chart" size={20} style={{ verticalAlign: "-3px", marginRight: 6 }} />
          {t("analytics.title")}
        </h1>
        <div className="analytics-controls">
          <div className="analytics-presets">
            {presets.map((p) => (
              <button
                key={p}
                className={`an-btn an-btn-sm ${preset === p ? "active" : ""}`}
                onClick={() => setPreset(p)}
              >
                {p === "custom" ? t("analytics.custom") : p}
              </button>
            ))}
          </div>
          {preset === "custom" && (
            <div className="analytics-custom-range">
              <input type="date" value={customStart} max={customEnd} onChange={(e) => setCustomStart(e.target.value)} />
              <span>–</span>
              <input type="date" value={customEnd} min={customStart} max={toDateStr(new Date())} onChange={(e) => setCustomEnd(e.target.value)} />
            </div>
          )}
          <button className="an-btn an-btn-sm" onClick={load} disabled={loading} title={t("analytics.refresh")} aria-label={t("analytics.refresh")}>
            {loading ? "…" : <Icon name="refresh" size={16} />}
          </button>
        </div>
      </header>

      {error && <div className="analytics-error">{error}</div>}

      <div className="analytics-stats">
        <StatCard label={t("analytics.statSessions")} value={fmt(report?.sessions.total ?? 0)} />
        <StatCard label={t("analytics.statTurns")} value={fmt(report?.turns.total ?? 0)} />
        <StatCard label={t("analytics.statMessages")} value={fmt(report?.sessions.messages_total ?? 0)} />
        <StatCard label={t("analytics.statAvgTurn")} value={fmtMs(report?.turns.duration_avg_ms ?? 0)} />
      </div>

      <div className="analytics-charts">
        <ChartCard title={t("analytics.sessionsOverTime")} full>
          {report && report.sessions.series.length > 0 ? (
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={report.sessions.series} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                <XAxis dataKey="t" tickFormatter={(v) => tickFormatter(v, granularity)} stroke="var(--text-muted)" fontSize={11} minTickGap={20} />
                <YAxis stroke="var(--text-muted)" fontSize={11} tickFormatter={fmt} allowDecimals={false} />
                <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: "var(--bg-hover)", opacity: 0.3 }} />
                <Bar dataKey="count" name={t("analytics.statSessions")} fill={BAR_COLOR} radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : <EmptyState label={noData} />}
        </ChartCard>

        <ChartCard title={t("analytics.turnsOverTime")} full>
          {report && report.turns.series.length > 0 ? (
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={report.turns.series} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                <XAxis dataKey="t" tickFormatter={(v) => tickFormatter(v, granularity)} stroke="var(--text-muted)" fontSize={11} minTickGap={20} />
                <YAxis stroke="var(--text-muted)" fontSize={11} tickFormatter={fmt} allowDecimals={false} />
                <Tooltip contentStyle={TOOLTIP_STYLE} />
                <Line type="monotone" dataKey="count" name={t("analytics.statTurns")} stroke={BAR_COLOR} strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          ) : <EmptyState label={noData} />}
        </ChartCard>

        <ChartCard title={t("analytics.sessionsByProvider")}>
          {report && report.sessions.by_provider.length > 0 ? (
            <HBar data={report.sessions.by_provider} dataKey="count" labelKey="name" />
          ) : <EmptyState label={noData} />}
        </ChartCard>

        <ChartCard title={t("analytics.sessionsByModel")}>
          {report && report.sessions.by_model.length > 0 ? (
            <HBar data={report.sessions.by_model.slice(0, 10)} dataKey="count" labelKey="model" />
          ) : <EmptyState label={noData} />}
        </ChartCard>

        <ChartCard title={t("analytics.turnsByProvider")}>
          {report && report.turns.by_provider.length > 0 ? (
            <HBar data={report.turns.by_provider} dataKey="turns" labelKey="name" />
          ) : <EmptyState label={noData} />}
        </ChartCard>

        <ChartCard title={t("analytics.turnsByModel")}>
          {report && report.turns.by_model.length > 0 ? (
            <HBar data={report.turns.by_model.slice(0, 10)} dataKey="turns" labelKey="model" />
          ) : <EmptyState label={noData} />}
        </ChartCard>

        <ChartCard title={t("analytics.byOrchestration")}>
          {report && report.sessions.by_orchestration.length > 0 ? (
            <ResponsiveContainer width="100%" height={260}>
              <PieChart>
                <Pie data={report.sessions.by_orchestration} dataKey="count" nameKey="mode" cx="50%" cy="50%" outerRadius={80} innerRadius={44} paddingAngle={2}>
                  {report.sessions.by_orchestration.map((_, i) => (
                    <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
                  ))}
                </Pie>
                <Tooltip contentStyle={TOOLTIP_STYLE} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
              </PieChart>
            </ResponsiveContainer>
          ) : <EmptyState label={noData} />}
        </ChartCard>
      </div>
    </div>
  );
}

/** Horizontal bar chart used for the by-provider / by-model breakdowns. */
function HBar({ data, dataKey, labelKey }: { data: Record<string, unknown>[]; dataKey: string; labelKey: string }) {
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart layout="vertical" data={data} margin={{ top: 8, right: 16, left: 8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" horizontal={false} />
        <XAxis type="number" stroke="var(--text-muted)" fontSize={11} tickFormatter={fmt} />
        <YAxis type="category" dataKey={labelKey} stroke="var(--text-muted)" fontSize={11} width={110} />
        <Tooltip contentStyle={TOOLTIP_STYLE} formatter={tooltipFmt} cursor={{ fill: "var(--bg-hover)", opacity: 0.3 }} />
        <Bar dataKey={dataKey} fill={BAR_COLOR} radius={[0, 3, 3, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="analytics-stat-card">
      <div className="analytics-stat-value">{value}</div>
      <div className="analytics-stat-label">{label}</div>
    </div>
  );
}

function ChartCard({ title, full, children }: { title: string; full?: boolean; children: React.ReactNode }) {
  return (
    <section className={`analytics-chart-card ${full ? "full" : ""}`}>
      <h2 className="analytics-chart-title">{title}</h2>
      {children}
    </section>
  );
}

function EmptyState({ label }: { label: string }) {
  return <div className="analytics-empty">{label}</div>;
}
