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
