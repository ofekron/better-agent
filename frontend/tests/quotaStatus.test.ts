import { describe, expect, it } from "vitest";
import {
  optionLabelWithQuota,
  providerQuotaStatus,
  quotaResetText,
  type QuotaLabelTranslator,
  type QuotaSummary,
} from "../src/utils/quotaStatus";

const summary: QuotaSummary = {
  usedPercent: 65,
  remainingPercent: 35,
  level: "ok",
  windowLabel: "5-hour",
  resetsAt: "2026-07-11T12:30:00.000Z",
};

const t: QuotaLabelTranslator = (key, options) => {
  if (key === "quota.remaining") return `${options.percent}% left`;
  if (key === "preSendAdvisory.resetsAt") return `Resets ${options.time}`;
  return "";
};

describe("provider quota option labels", () => {
  it("includes the relevant window reset alongside remaining usage", () => {
    const reset = quotaResetText(summary, t, "en-US");

    expect(reset).toContain("Resets Jul 11");
    expect(optionLabelWithQuota("Claude", summary, t)).toMatch(
      /^Claude · 35% left · Resets /,
    );
  });

  it("omits missing or invalid reset timestamps", () => {
    expect(optionLabelWithQuota("Claude", { ...summary, resetsAt: null }, t)).toBe(
      "Claude · 35% left",
    );
    expect(optionLabelWithQuota("Claude", { ...summary, resetsAt: "invalid" }, t)).toBe(
      "Claude · 35% left",
    );
  });

  it("selects the complete status for the exact provider account", () => {
    const status = {
      claude: {
        provider: "claude",
        label: "Claude",
        supported: true,
        windows: [
          { key: "five_hour", label: "Session (5h)", used_percent: 40 },
          { key: "seven_day", label: "Weekly (7d)", used_percent: 55 },
          { key: "seven_day_opus", label: "Weekly opus (7d)", used_percent: 70 },
        ],
      },
    };

    expect(providerQuotaStatus(status, { id: "claude", kind: "claude" })?.windows)
      .toHaveLength(3);
  });
});
