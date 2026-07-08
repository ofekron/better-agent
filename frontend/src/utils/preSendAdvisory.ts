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

// Per (provider, model) frontend-only snooze of the pre-send advisory dialog.
// Mirrors the ba_bypass_perm_ack pattern: a transient UI ack, not backend state.
const SNOOZE_STORAGE_KEY = "ba_pre_send_advisory_snooze_v1";
const SNOOZE_MS = 5 * 60 * 60 * 1000;

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

/** Ask the backend for pre-send advisories. Advisories are signals, never
 * gates: any error or timeout resolves to an empty list so sending is
 * never blocked by a slow or failing advisor. */
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
