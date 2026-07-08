import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Bar,
  BarChart,
  Brush,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  Pie,
  PieChart,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { fetchAnalytics, type AnalyticsGranularity, type AnalyticsReport } from "../api";
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
function fmtDateTime(value?: string): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
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

const EMPTY_LLM_CALLS: AnalyticsReport["llm_calls"] = {
  total: 0,
  token_usage: {
    input_tokens: 0,
    output_tokens: 0,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    total_tokens: 0,
  },
  series: [],
  by_provider: [],
  by_model: [],
  by_source: [],
  by_reason: [],
  recent: [],
};

export function AnalyticsPage({ onBack }: Props) {
  const { t } = useTranslation();
  const [preset, setPreset] = useState<Preset>("all");
  const [customStart, setCustomStart] = useState(daysAgo(30));
  const [customEnd, setCustomEnd] = useState(toDateStr(new Date()));
  const [granularity, setGranularity] = useState<AnalyticsGranularity>("auto");
  const [report, setReport] = useState<AnalyticsReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const { start, end } = useMemo(() => {
    if (preset === "custom") return { start: customStart, end: customEnd };
    if (preset === "all") return { start: undefined, end: undefined };
    const n = preset === "7d" ? 7 : preset === "90d" ? 90 : 30;
    return { start: daysAgo(n), end: toDateStr(new Date()) };
  }, [preset, customStart, customEnd]);

  // Single fetcher for both the range-change effect and the refresh button.
  const reqIdRef = useRef(0);
  const load = useCallback(async () => {
    const myId = ++reqIdRef.current;
    setLoading(true);
    setError(null);
    try {
      const data = await fetchAnalytics(start, end, granularity);
      if (reqIdRef.current !== myId) return;
      setReport(data);
    } catch (e) {
      if (reqIdRef.current !== myId) return;
      setError(e instanceof Error ? e.message : String(e));
      setReport(null);
    } finally {
      if (reqIdRef.current === myId) setLoading(false);
    }
  }, [start, end, granularity]);

  useEffect(() => {
    void load();
  }, [load]);

  const resolvedGranularity = report?.range.granularity ?? "day";
  const presets: Preset[] = ["7d", "30d", "90d", "all", "custom"];
  const granularityOptions: AnalyticsGranularity[] = ["auto", "day", "week", "month"];
  const noData = loading ? t("common.loading") : t("analytics.noData");
  const llmCalls = report?.llm_calls ?? EMPTY_LLM_CALLS;
  const llmUsage = llmCalls.token_usage;

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
          <div className="analytics-presets" aria-label={t("analytics.granularity")}>
            {granularityOptions.map((g) => (
              <button
                key={g}
                className={`an-btn an-btn-sm ${granularity === g ? "active" : ""}`}
                onClick={() => setGranularity(g)}
                title={t("analytics.granularity")}
              >
                {g === "auto" ? t("analytics.granularityAuto") : t(`analytics.granularity${g[0].toUpperCase()}${g.slice(1)}`)}
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
        <StatCard label={t("analytics.statLlmCalls")} value={fmt(llmCalls.total)} />
        <StatCard label={t("analytics.statLlmTokens")} value={fmt(llmUsage?.total_tokens ?? 0)} />
      </div>

      <div className="analytics-charts">
        <ChartCard title={t("analytics.sessionsOverTime")} full>
          {report && report.sessions.series.length > 0 ? (
            <TimeSeriesChart
              data={report.sessions.series as unknown as Record<string, unknown>[]}
              granularity={resolvedGranularity}
              series={[{ type: "bar", dataKey: "count", name: t("analytics.statSessions"), color: BAR_COLOR }]}
            />
          ) : <EmptyState label={noData} />}
        </ChartCard>

        <ChartCard title={t("analytics.turnsOverTime")} full>
          {report && report.turns.series.length > 0 ? (
            <TimeSeriesChart
              data={(report.turns.series as unknown as Record<string, unknown>[]).map((b) => ({
                ...b,
                non_user: Math.max(0, Number(b.count ?? 0) - Number(b.user_count ?? 0)),
              }))}
              granularity={resolvedGranularity}
              legend
              series={[
                { type: "bar", dataKey: "user_count", name: t("analytics.statUserTurns"), color: "#4ac2c0", stackId: "turns" },
                { type: "bar", dataKey: "non_user", name: t("analytics.statOtherTurns"), color: BAR_COLOR, stackId: "turns" },
              ]}
            />
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
        <ChartCard title={t("analytics.llmCallsOverTime")} full>
          {llmCalls.series.length > 0 ? (
            <TimeSeriesChart
              data={llmCalls.series as unknown as Record<string, unknown>[]}
              granularity={resolvedGranularity}
              legend
              series={[
                { type: "line", dataKey: "count", name: t("analytics.statLlmCalls"), color: BAR_COLOR, yAxisId: "calls" },
                { type: "line", dataKey: "total_tokens", name: t("analytics.statLlmTokens"), color: "#4ac2c0", yAxisId: "tokens" },
              ]}
              rightAxis
            />
          ) : <EmptyState label={noData} />}
        </ChartCard>

        <ChartCard title={t("analytics.llmTokensBreakdown")}>
          {llmUsage && (llmUsage.input_tokens || llmUsage.output_tokens || llmUsage.cache_read_input_tokens || llmUsage.cache_creation_input_tokens) ? (
            <ResponsiveContainer width="100%" height={260}>
              <PieChart>
                <Pie
                  data={[
                    { name: t("tokens.input"), value: llmUsage.input_tokens },
                    { name: t("tokens.output"), value: llmUsage.output_tokens },
                    { name: t("analytics.cacheRead"), value: llmUsage.cache_read_input_tokens },
                    { name: t("analytics.cacheWrite"), value: llmUsage.cache_creation_input_tokens },
                  ].filter((row) => row.value > 0)}
                  dataKey="value"
                  nameKey="name"
                  cx="50%"
                  cy="50%"
                  outerRadius={80}
                  innerRadius={44}
                  paddingAngle={2}
                >
                  {[0, 1, 2, 3].map((i) => (
                    <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
                  ))}
                </Pie>
                <Tooltip contentStyle={TOOLTIP_STYLE} formatter={tooltipFmt} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
              </PieChart>
            </ResponsiveContainer>
          ) : <EmptyState label={noData} />}
        </ChartCard>

        <ChartCard title={t("analytics.llmCallsBySource")}>
          {llmCalls.by_source.length > 0 ? (
            <HBar data={llmCalls.by_source} dataKey="calls" labelKey="source" />
          ) : <EmptyState label={noData} />}
        </ChartCard>

        <ChartCard title={t("analytics.llmCallsByReason")}>
          {llmCalls.by_reason.length > 0 ? (
            <HBar data={llmCalls.by_reason} dataKey="calls" labelKey="reason" />
          ) : <EmptyState label={noData} />}
        </ChartCard>

        <ChartCard title={t("analytics.llmCallsByProvider")}>
          {llmCalls.by_provider.length > 0 ? (
            <HBar data={llmCalls.by_provider} dataKey="calls" labelKey="name" />
          ) : <EmptyState label={noData} />}
        </ChartCard>

        <ChartCard title={t("analytics.llmCallsByModel")}>
          {llmCalls.by_model.length > 0 ? (
            <HBar data={llmCalls.by_model.slice(0, 10)} dataKey="calls" labelKey="model" />
          ) : <EmptyState label={noData} />}
        </ChartCard>

        <ChartCard title={t("analytics.llmCallLog")} full>
          {llmCalls.recent.length > 0 ? (
            <div className="analytics-log-list">
              {llmCalls.recent.slice(0, 40).map((call) => (
                <article className="analytics-log-row" key={call.id || `${call.timestamp}-${call.provider_session_id}`}>
                  <div className="analytics-log-main">
                    <div className="analytics-log-title">
                      <span>{call.reason}</span>
                      <span className={call.success === false ? "analytics-log-status error" : "analytics-log-status"}>
                        {call.success === false ? t("analytics.failed") : t("analytics.succeeded")}
                      </span>
                    </div>
                    <div className="analytics-log-prompt">{call.prompt_preview || t("analytics.noPromptPreview")}</div>
                    {call.error && <div className="analytics-log-error">{call.error}</div>}
                  </div>
                  <div className="analytics-log-meta">
                    <span>{fmtDateTime(call.timestamp)}</span>
                    <span>{call.source}</span>
                    <span>{call.provider_name || call.provider_kind}</span>
                    <span>{call.model}</span>
                    <span>{fmt(call.token_usage.total_tokens)} {t("analytics.tokensShort")}</span>
                  </div>
                </article>
              ))}
            </div>
          ) : <EmptyState label={noData} />}
        </ChartCard>

      </div>
    </div>
  );
}

interface TimeSeriesSeries {
  type: "line" | "bar";
  dataKey: string;
  name: string;
  color: string;
  yAxisId?: string;
  stackId?: string;
}

/**
 * Interactive time-series chart with drag-to-zoom, brush pan/resize, mouse-wheel
 * zoom, and a reset control. The zoom window is expressed as [startIndex, endIndex]
 * into `data`; the brush both reflects and drives that window so panning and zooming
 * stay in sync. The window resets whenever the underlying data identity changes
 * (e.g. the user switches the date range or granularity).
 */
function TimeSeriesChart({
  data,
  granularity,
  series,
  legend,
  rightAxis,
}: {
  data: Record<string, unknown>[];
  granularity: string;
  series: TimeSeriesSeries[];
  legend?: boolean;
  rightAxis?: boolean;
}) {
  const { t } = useTranslation();
  const lastIndex = Math.max(0, data.length - 1);

  // Committed zoom window (inclusive indices into `data`).
  const [window, setWindow] = useState<{ start: number; end: number }>({ start: 0, end: lastIndex });
  // In-progress drag selection (indices), or null when not dragging.
  const [sel, setSel] = useState<{ a: string; b: string | null } | null>(null);
  // Track the data identity so we can reset the zoom window when the range or
  // granularity changes. Adjusting state during render (the React-recommended
  // pattern for derived-from-props resets) avoids a cascading effect re-render.
  const [prevData, setPrevData] = useState(data);
  if (prevData !== data) {
    setPrevData(data);
    setWindow({ start: 0, end: lastIndex });
    if (sel !== null) setSel(null);
  }

  const start = Math.min(window.start, lastIndex);
  const end = Math.min(window.end, lastIndex);
  const zoomed = start > 0 || end < lastIndex;

  const indexOf = useCallback(
    (label: unknown) => data.findIndex((row) => String(row.t) === String(label)),
    [data],
  );

  const resetZoom = useCallback(() => {
    setWindow({ start: 0, end: lastIndex });
    setSel(null);
  }, [lastIndex]);

  const commitSelection = useCallback(() => {
    if (!sel || sel.b === null || sel.a === sel.b) {
      setSel(null);
      return;
    }
    let a = indexOf(sel.a);
    let b = indexOf(sel.b);
    if (a < 0 || b < 0) {
      setSel(null);
      return;
    }
    if (a > b) [a, b] = [b, a];
    setWindow({ start: a, end: b });
    setSel(null);
  }, [sel, indexOf]);

  // Mouse-wheel zoom centered on the current window midpoint. This is wired as a
  // native non-passive listener below so preventDefault actually stops the page
  // from scrolling while the chart zooms.
  const onWheel = useCallback(
    (e: WheelEvent) => {
      if (data.length < 3) return;
      e.preventDefault();
      const span = end - start;
      const mid = (start + end) / 2;
      const zoomingIn = e.deltaY < 0;
      const nextSpan = Math.max(
        1,
        Math.min(lastIndex, zoomingIn ? Math.floor(span * 0.8) : Math.ceil(span * 1.25)),
      );
      let ns = Math.round(mid - nextSpan / 2);
      let ne = ns + nextSpan;
      if (ns < 0) { ns = 0; ne = nextSpan; }
      if (ne > lastIndex) { ne = lastIndex; ns = Math.max(0, lastIndex - nextSpan); }
      setWindow({ start: ns, end: ne });
    },
    [data.length, start, end, lastIndex],
  );

  const surfaceRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const node = surfaceRef.current;
    if (!node) return undefined;
    node.addEventListener("wheel", onWheel, { passive: false });
    return () => node.removeEventListener("wheel", onWheel);
  }, [onWheel]);

  const onBrushChange = useCallback(
    (range: { startIndex?: number; endIndex?: number }) => {
      if (range && typeof range.startIndex === "number" && typeof range.endIndex === "number") {
        setWindow({ start: range.startIndex, end: range.endIndex });
      }
    },
    [],
  );

  const axisFmt = useCallback((v: string) => tickFormatter(v, granularity), [granularity]);
  const showBrush = data.length > 2;

  return (
    <div className="analytics-chart-interactive">
      <div className="analytics-chart-actions">
        {zoomed && (
          <button
            type="button"
            className="an-btn an-btn-sm"
            onClick={resetZoom}
            title={t("analytics.resetZoom")}
            aria-label={t("analytics.resetZoom")}
          >
            {t("analytics.resetZoom")}
          </button>
        )}
      </div>
      <div
        ref={surfaceRef}
        className="analytics-chart-surface"
        data-testid="analytics-time-series-chart"
        data-series-keys={series.map((s) => s.dataKey).join(",")}
        style={{ userSelect: sel ? "none" : undefined }}
      >
        <ResponsiveContainer width="100%" height={240}>
          <ComposedChart
            data={data}
            margin={{ top: 8, right: 16, left: 0, bottom: 0 }}
            onMouseDown={(st: { activeLabel?: string | number }) => {
              if (st && st.activeLabel != null) setSel({ a: String(st.activeLabel), b: null });
            }}
            onMouseMove={(st: { activeLabel?: string | number }) => {
              setSel((cur) => (cur && st && st.activeLabel != null ? { ...cur, b: String(st.activeLabel) } : cur));
            }}
            onMouseUp={commitSelection}
            onMouseLeave={() => setSel(null)}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
            <XAxis
              dataKey="t"
              tickFormatter={axisFmt}
              stroke="var(--text-muted)"
              fontSize={11}
              minTickGap={20}
              allowDataOverflow
            />
            <YAxis
              yAxisId={rightAxis ? "calls" : "0"}
              stroke="var(--text-muted)"
              fontSize={11}
              tickFormatter={fmt}
              allowDecimals={false}
            />
            {rightAxis && (
              <YAxis
                yAxisId="tokens"
                orientation="right"
                stroke="var(--text-muted)"
                fontSize={11}
                tickFormatter={fmt}
              />
            )}
            <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: "var(--bg-hover)", opacity: 0.3 }} />
            {legend && <Legend wrapperStyle={{ fontSize: 11 }} />}
            {series.map((s) =>
              s.type === "bar" ? (
                <Bar
                  key={s.dataKey}
                  yAxisId={s.yAxisId ?? (rightAxis ? "calls" : "0")}
                  dataKey={s.dataKey}
                  name={s.name}
                  fill={s.color}
                  stackId={s.stackId}
                  radius={s.stackId ? undefined : [3, 3, 0, 0]}
                  isAnimationActive={false}
                />
              ) : (
                <Line
                  key={s.dataKey}
                  yAxisId={s.yAxisId ?? (rightAxis ? "calls" : "0")}
                  type="monotone"
                  dataKey={s.dataKey}
                  name={s.name}
                  stroke={s.color}
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              ),
            )}
            {sel && sel.b !== null && sel.a !== sel.b && (
              <ReferenceArea
                yAxisId={series[0].yAxisId ?? (rightAxis ? "calls" : "0")}
                x1={sel.a}
                x2={sel.b}
                strokeOpacity={0.3}
                fill="var(--accent, #7b68ee)"
                fillOpacity={0.15}
              />
            )}
            {showBrush && (
              <Brush
                dataKey="t"
                height={22}
                travellerWidth={8}
                stroke="var(--border)"
                fill="var(--bg-tertiary)"
                startIndex={start}
                endIndex={end}
                onChange={onBrushChange}
                tickFormatter={axisFmt}
              />
            )}
          </ComposedChart>
        </ResponsiveContainer>
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
