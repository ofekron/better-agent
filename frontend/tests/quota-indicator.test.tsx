import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { QuotaIndicator } from "../src/components/QuotaIndicator";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, string | number>) => {
      if (key === "quota.remaining") return `${options?.percent}% left`;
      if (key === "preSendAdvisory.resetsAt") return `Resets ${options?.time}`;
      if (key === "quota.stale") return "stale";
      if (key === "tokens.noUsage") return "No usage yet";
      return String(options?.defaultValue ?? key)
        .replace("{{remaining}}", String(options?.remaining ?? ""))
        .replace("{{window}}", String(options?.window ?? ""))
        .replace("{{error}}", String(options?.error ?? ""))
        .replace("{{reason}}", String(options?.reason ?? ""));
    },
  }),
}));

describe("QuotaIndicator", () => {
  it("shows every time and model-specific window with reset and projection data", () => {
    render(
      <QuotaIndicator
        status={{
          provider: "claude",
          label: "Claude",
          supported: true,
          windows: [
            { key: "five_hour", label: "Session (5h)", used_percent: 92, resets_at: "2026-07-12T20:00:00Z" },
            { key: "seven_day", label: "Weekly (7d)", used_percent: 40 },
            { key: "seven_day_opus", label: "Weekly opus (7d)", used_percent: 75, minutes_to_exhaustion: 86 },
          ],
        }}
      />,
    );

    expect(screen.getByText("Session (5h)")).toBeTruthy();
    expect(screen.getByText("8% left")).toBeTruthy();
    expect(screen.getByText("Weekly (7d)")).toBeTruthy();
    expect(screen.getByText("60% left")).toBeTruthy();
    expect(screen.getByText("Weekly opus (7d)")).toBeTruthy();
    expect(screen.getByText("25% left")).toBeTruthy();
    expect(screen.getByText("~86m")).toBeTruthy();
    expect(screen.getByText(/^Resets /)).toBeTruthy();
  });

  it.each([
    undefined,
    { provider: "codex", label: "Codex", supported: true, windows: [] },
  ])("reports genuinely absent usage as such", (status) => {
    render(<QuotaIndicator status={status} />);
    expect(screen.getByText("No usage yet")).toBeTruthy();
  });

  // A provider that CANNOT be read is not the same as one with no usage:
  // collapsing both into "No usage yet" hid the actionable cause.
  it.each([
    [
      { provider: "agy", label: "Antigravity", supported: false, reason: "credentials_unavailable", windows: [] },
      "credentials not readable",
    ],
    [
      { provider: "claude", label: "Claude", supported: true, error: "no_credentials", windows: [] },
      "not signed in",
    ],
    [
      { provider: "claude", label: "Claude", supported: true, error: "http_418", windows: [] },
      "http_418",
    ],
  ])("names why quota is unavailable", (status, expected) => {
    const { container } = render(<QuotaIndicator status={status} />);
    expect(container.querySelector(".quota-unavailable")).not.toBeNull();
    expect(container.textContent).toContain(expected);
    expect(screen.queryByText("No usage yet")).toBeNull();
  });

  it("shows an explicit pending state while a first reading is fetched", () => {
    const { container } = render(
      <QuotaIndicator
        status={{ provider: "claude", label: "Claude", supported: true, loading: true, windows: [] }}
      />,
    );
    expect(container.querySelector(".quota-loading")).not.toBeNull();
    expect(screen.queryByText("No usage yet")).toBeNull();
  });

  it("keeps all stale windows visible", () => {
    render(
      <QuotaIndicator
        status={{
          provider: "agy",
          label: "Antigravity",
          supported: true,
          stale: true,
          error: "http_503",
          windows: [
            { key: "model:pro", label: "agy-pro", used_percent: 80 },
            { key: "model:flash", label: "agy-flash", used_percent: 10 },
          ],
        }}
      />,
    );

    expect(screen.getByText("agy-pro")).toBeTruthy();
    expect(screen.getByText("agy-flash")).toBeTruthy();
    expect(screen.getAllByText("stale")).toHaveLength(2);
  });
});
