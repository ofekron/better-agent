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
    vi.clearAllMocks();
  });

  it("loads backend-default all-time analytics by default", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(emptyReport);

    render(<AnalyticsPage onBack={() => undefined} />);

    await waitFor(() => {
      expect(fetchAnalytics).toHaveBeenCalledWith(undefined, undefined, "auto");
    });
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

    expect((await screen.findByTestId("analytics-time-series-chart")).getAttribute("data-series-keys")).toBe("count,user_count");
  });
});
