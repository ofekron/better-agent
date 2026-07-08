// Per-provider quota projection for the Usage extension's quota-status
// endpoint. Pure helpers + the wire types; the fetch lives in
// useQuotaStatus. Mirrors the burn-rate/worst-window logic of the
// usage-gauge extension module so pickers and gauge agree.

export const QUOTA_STATUS_PATH = "/api/extensions/ofek-dev.usage/backend/quota-status";

export const quotaStatusUrl = (apiBase: string): string => `${apiBase}${QUOTA_STATUS_PATH}`;

export interface QuotaWindow {
  key: string;
  label: string;
  used_percent: number;
  resets_at?: string | null;
  minutes_to_exhaustion?: number;
}

export interface QuotaProviderStatus {
  provider: string;
  label: string;
  supported: boolean;
  plan?: string;
  error?: string;
  windows?: QuotaWindow[];
}

/** Map keyed by provider kind (claude, codex, gemini, ...). */
export type QuotaStatus = Record<string, QuotaProviderStatus>;

export type QuotaLevel = "ok" | "warn" | "critical";

export interface QuotaSummary {
  /** Rounded percent of quota already used (worst window). */
  usedPercent: number;
  /** Rounded percent remaining (100 - used). */
  remainingPercent: number;
  level: QuotaLevel;
  windowLabel: string;
  resetsAt?: string | null;
}

export type QuotaLabelTranslator = (
  key: string,
  options: { percent: number; defaultValue: string },
) => string;

// Thresholds match the usage-gauge so every surface colors consistently.
const WARN_USED = 70;
const CRITICAL_USED = 90;

export function quotaLevel(usedPercent: number): QuotaLevel {
  if (usedPercent >= CRITICAL_USED) return "critical";
  if (usedPercent >= WARN_USED) return "warn";
  return "ok";
}

/** Highest-utilization window for a provider, or null when unsupported /
 * errored / windowless. This is the window a picker warns about. */
export function worstWindow(status: QuotaProviderStatus | undefined): QuotaWindow | null {
  if (!status || status.supported === false || status.error) return null;
  let worst: QuotaWindow | null = null;
  for (const w of status.windows ?? []) {
    if (typeof w.used_percent !== "number") continue;
    if (!worst || w.used_percent > worst.used_percent) worst = w;
  }
  return worst;
}

/** One-line summary for a provider kind, or null when no usage data.
 *
 * NOTE: usage is measured per KIND against the provider CLI's single OAuth
 * token (default ~/.claude / ~/.codex), not per provider instance. For the
 * common one-provider-per-kind setup this is exact; for multiple same-kind
 * providers on different accounts/config_dirs every one of them shows the
 * same number. Tracked as a known limitation. */
export function summarizeKind(status: QuotaStatus, kind: string | undefined): QuotaSummary | null {
  if (!kind) return null;
  const worst = worstWindow(status[kind]);
  if (!worst) return null;
  const used = Math.round(worst.used_percent);
  return {
    usedPercent: used,
    remainingPercent: Math.max(0, 100 - used),
    level: quotaLevel(used),
    windowLabel: worst.label,
    resetsAt: worst.resets_at ?? null,
  };
}

export function quotaRemainingText(
  summary: QuotaSummary | null | undefined,
  t: QuotaLabelTranslator,
): string {
  if (!summary) return "";
  return t("quota.remaining", {
    percent: summary.remainingPercent,
    defaultValue: "{{percent}}% left",
  });
}

export function optionLabelWithQuota(
  label: string,
  summary: QuotaSummary | null | undefined,
  t: QuotaLabelTranslator,
): string {
  const remaining = quotaRemainingText(summary, t);
  return remaining ? `${label} · ${remaining}` : label;
}
