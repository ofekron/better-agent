import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AnalyticsPage } from "../src/components/AnalyticsPage";
import { fetchAnalytics } from "../src/api";

vi.mock("../src/api", () => ({
  fetchAnalytics: vi.fn(),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

const emptyReport = {
  range: { start: "2000-01-01T00:00:00", end: "2026-07-06T23:59:59", granularity: "month" },
  providers: [],
  sessions: {
    total: 0,
    messages_total: 0,
    series: [],
    by_provider: [],
    by_model: [],
    by_orchestration: [],
  },
  turns: {
    total: 0,
    series: [],
    by_provider: [],
    by_model: [],
    duration_avg_ms: 0,
    duration_p50_ms: 0,
  },
  llm_calls: {
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
  },
};

describe("AnalyticsPage", () => {
  afterEach(() => {
    vi.resetAllMocks();
  });

  it("loads backend-default all-time analytics by default", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(emptyReport);

    render(<AnalyticsPage onBack={() => undefined} />);

    await waitFor(() => {
      expect(fetchAnalytics).toHaveBeenCalledWith(
        undefined,
        undefined,
        "auto",
        expect.any(AbortSignal),
      );
    });
  });

  it("labels partial native data instead of leaving charts loading", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue({
      ...emptyReport,
      native_data: {
        state: "stale",
        refresh_requested: true,
        partial_metrics: ["turns"],
      },
    });

    render(<AnalyticsPage onBack={() => undefined} />);

    expect((await screen.findByRole("status")).textContent).toBe(
      "analytics.nativePartial",
    );
    expect(screen.queryByText("common.loading")).toBeNull();
  });

  it("aborts a superseded analytics request", async () => {
    const signals: AbortSignal[] = [];
    vi.mocked(fetchAnalytics).mockImplementation(
      async (_start, _end, _granularity, signal) => {
        if (signal) signals.push(signal);
        return emptyReport;
      },
    );

    render(<AnalyticsPage onBack={() => undefined} />);
    await waitFor(() => expect(signals).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    await waitFor(() => expect(signals).toHaveLength(2));

    expect(signals[0].aborted).toBe(true);
  });

  it("aborts analytics loading when the page unmounts", async () => {
    let signal: AbortSignal | undefined;
    vi.mocked(fetchAnalytics).mockImplementation(
      async (_start, _end, _granularity, requestSignal) => {
        signal = requestSignal;
        return new Promise<never>(() => undefined);
      },
    );

    const view = render(<AnalyticsPage onBack={() => undefined} />);
    await waitFor(() => expect(signal).toBeDefined());
    view.unmount();

    expect(signal?.aborted).toBe(true);
  });

  it("shows zoom controls for populated time-series charts", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue({
      ...emptyReport,
      sessions: {
        ...emptyReport.sessions,
        series: [
          { t: "2026-07-01", count: 1 },
          { t: "2026-07-02", count: 2 },
          { t: "2026-07-03", count: 3 },
          { t: "2026-07-04", count: 4 },
          { t: "2026-07-05", count: 5 },
        ],
      },
    });

    render(<AnalyticsPage onBack={() => undefined} />);

    const chart = await screen.findByTestId("analytics-time-series-chart");
    fireEvent.wheel(chart, { deltaY: -100 });

    expect(await screen.findByRole("button", { name: "analytics.resetZoom" })).toBeTruthy();
  });

  it("shows user-turn sub-series on the turns chart", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue({
      ...emptyReport,
      turns: {
        ...emptyReport.turns,
        series: [
          { t: "2026-07-01", count: 5, user_count: 2, duration_ms: 100 },
          { t: "2026-07-02", count: 8, user_count: 3, duration_ms: 200 },
        ],
      },
    });

    render(<AnalyticsPage onBack={() => undefined} />);

    const charts = await screen.findAllByTestId("analytics-time-series-chart");
    expect(charts.some(
      (chart) => chart.getAttribute("data-series-keys") === "user_count,non_user",
    )).toBe(true);
  });
});
