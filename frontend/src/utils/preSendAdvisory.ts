export interface PreSendAdvisory {
  extension_id: string;
  title: string;
  severity: "info" | "warn";
  detail?: string;
  usage_percent?: number;
  resets_at?: string;
  source?: string;
}

const FETCH_TIMEOUT_MS = 2500;
const CACHE_TTL_MS = 60_000;

// Per (provider, model) frontend-only snooze of the pre-send advisory dialog.
// Mirrors the ba_bypass_perm_ack pattern: a transient UI ack, not backend state.
const SNOOZE_STORAGE_KEY = "ba_pre_send_advisory_snooze_v1";
const SNOOZE_MS = 5 * 60 * 60 * 1000;

type AdvisoryCacheEntry = {
  fetchedAt: number;
  advisories: PreSendAdvisory[];
};

const advisoryCache = new Map<string, AdvisoryCacheEntry>();
const advisoryRefreshes = new Map<string, Promise<void>>();

export function preSendAdvisorySnoozeKey(
  providerId: string | undefined,
  model: string | undefined,
): string {
  return `${providerId || ""}:${model || ""}`;
}

function readSnoozeMap(): Record<string, number> {
  try {
    const raw = localStorage.getItem(SNOOZE_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? (parsed as Record<string, number>) : {};
  } catch {
    return {};
  }
}

function writeSnoozeMap(map: Record<string, number>): void {
  try {
    localStorage.setItem(SNOOZE_STORAGE_KEY, JSON.stringify(map));
  } catch {
    // Storage unavailable or full — snooze is best-effort; sending never blocks.
  }
}

export function isPreSendAdvisorySnoozed(
  providerId: string | undefined,
  model: string | undefined,
): boolean {
  const expiry = readSnoozeMap()[preSendAdvisorySnoozeKey(providerId, model)];
  return typeof expiry === "number" && Date.now() < expiry;
}

export function snoozePreSendAdvisory(
  providerId: string | undefined,
  model: string | undefined,
): void {
  const map = readSnoozeMap();
  map[preSendAdvisorySnoozeKey(providerId, model)] = Date.now() + SNOOZE_MS;
  writeSnoozeMap(map);
}

function cacheKey(
  apiBase: string,
  sessionId: string,
  providerId: string | undefined,
  model: string | undefined,
): string {
  return `${apiBase}:${sessionId}:${providerId || ""}:${model || ""}`;
}

export async function fetchPreSendAdvisories(
  apiBase: string,
  sessionId: string,
  providerId: string | undefined,
  model: string | undefined,
): Promise<PreSendAdvisory[]> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(
      `${apiBase}/api/sessions/${encodeURIComponent(sessionId)}/pre-send-advisories`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider_id: providerId || "", model: model || "" }),
        signal: controller.signal,
      },
    );
    if (!res.ok) return [];
    const data = await res.json();
    return Array.isArray(data?.advisories) ? (data.advisories as PreSendAdvisory[]) : [];
  } catch {
    return [];
  } finally {
    clearTimeout(timer);
  }
}

export function cachedPreSendAdvisories(
  apiBase: string,
  sessionId: string,
  providerId: string | undefined,
  model: string | undefined,
): PreSendAdvisory[] | null {
  const entry = advisoryCache.get(cacheKey(apiBase, sessionId, providerId, model));
  if (!entry || Date.now() - entry.fetchedAt > CACHE_TTL_MS) return null;
  return entry.advisories;
}

export function refreshPreSendAdvisories(
  apiBase: string,
  sessionId: string,
  providerId: string | undefined,
  model: string | undefined,
): void {
  const key = cacheKey(apiBase, sessionId, providerId, model);
  if (advisoryRefreshes.has(key)) return;
  const refresh = fetchPreSendAdvisories(apiBase, sessionId, providerId, model)
    .then((advisories) => {
      advisoryCache.set(key, { fetchedAt: Date.now(), advisories });
    })
    .finally(() => {
      advisoryRefreshes.delete(key);
    });
  advisoryRefreshes.set(key, refresh);
}

export function clearPreSendAdvisoryCacheForTests(): void {
  advisoryCache.clear();
  advisoryRefreshes.clear();
}
