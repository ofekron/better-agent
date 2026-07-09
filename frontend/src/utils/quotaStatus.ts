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
  /** Set when the reading is a re-served last-good snapshot after a
   * transient fetch failure (the error field carries the failure code). */
  stale?: boolean;
  stale_seconds?: number;
  /** Set on unsupported providers with a specific cause (e.g. Antigravity's
   * credentials_unavailable). */
  reason?: string;
  windows?: QuotaWindow[];
}

/** Map keyed by provider id (falling back to "<kind>::<config_dir>"). */
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
  /** True when this is a re-served last-good reading (fetch failing now). */
  stale?: boolean;
  /** Failure code accompanying a stale reading. */
  error?: string;
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
 * windowless. Stale readings (last-good snapshot re-served during a
 * transient failure) still count — hiding them is what made the
 * indicator "sometimes disappear". */
export function worstWindow(status: QuotaProviderStatus | undefined): QuotaWindow | null {
  if (!status || status.supported === false) return null;
  if (status.error && !status.stale) return null;
  let worst: QuotaWindow | null = null;
  for (const w of status.windows ?? []) {
    if (typeof w.used_percent !== "number") continue;
    if (!worst || w.used_percent > worst.used_percent) worst = w;
  }
  return worst;
}

/** Response key of the extension's POST /quota-status: the provider id
 * when known, else "<kind>::<config_dir>" (one CLI token per pair). */
export function providerQuotaKey(provider: {
  id?: string;
  kind: string;
  config_dir?: string;
}): string {
  return provider.id || `${provider.kind}::${provider.config_dir || ""}`;
}

/** Worst-window summary from a single provider's status, or null when there
 * is no usage data (unsupported, offline, no credentials). */
export function summarizeProviderStatus(status: QuotaProviderStatus | undefined): QuotaSummary | null {
  const worst = worstWindow(status);
  if (!worst || !status) return null;
  const used = Math.round(worst.used_percent);
  const summary: QuotaSummary = {
    usedPercent: used,
    remainingPercent: Math.max(0, 100 - used),
    level: quotaLevel(used),
    windowLabel: worst.label,
    resetsAt: worst.resets_at ?? null,
  };
  if (status.stale) {
    summary.stale = true;
    if (status.error) summary.error = status.error;
  }
  return summary;
}

/** Per-provider lookup into a quota-status map keyed by `providerQuotaKey`. */
export function summarizeProvider(
  status: QuotaStatus,
  provider: { id?: string; kind?: string; config_dir?: string } | undefined,
): QuotaSummary | null {
  if (!provider?.kind) return null;
  return summarizeProviderStatus(
    status[providerQuotaKey({ id: provider.id, kind: provider.kind, config_dir: provider.config_dir })],
  );
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
