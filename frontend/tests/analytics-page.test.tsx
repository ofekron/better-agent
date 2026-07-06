import { render, waitFor } from "@testing-library/react";
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
      expect(fetchAnalytics).toHaveBeenCalledWith(undefined, undefined);
    });
  });
});
