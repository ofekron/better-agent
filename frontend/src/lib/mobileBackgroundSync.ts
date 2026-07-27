import { BackgroundRunner } from "@capacitor/background-runner";
import { Capacitor } from "@capacitor/core";

const RUNNER_LABEL = "com.betteragent.offline-sync";
const QUEUE_STORAGE_KEY = "better_agent_offline_queue";
const TOKEN_STORAGE_KEY = "better_agent_auth_token";
const SERVER_URL_STORAGE_KEY = "better_agent_server_url";

function currentState(): string | null {
  try {
    const serverUrl = localStorage.getItem(SERVER_URL_STORAGE_KEY)?.replace(/\/+$/, "");
    const accessToken = localStorage.getItem(TOKEN_STORAGE_KEY);
    if (!serverUrl || !accessToken) return null;
    const parsed = JSON.parse(localStorage.getItem(QUEUE_STORAGE_KEY) || "[]");
    if (!Array.isArray(parsed)) return null;
    return JSON.stringify({ serverUrl, accessToken, actions: parsed });
  } catch {
    return null;
  }
}

export async function mirrorMobileBackgroundSyncState(): Promise<void> {
  if (!Capacitor.isNativePlatform()) return;
  const state = currentState();
  await BackgroundRunner.dispatchEvent({
    label: RUNNER_LABEL,
    event: state ? "updateOfflineState" : "clearOfflineState",
    details: state ? { state } : {},
  });
}

export interface MobileBackgroundAcknowledgement {
  sessionId: string;
  clientId: string;
}

export async function getMobileBackgroundAcknowledgements(): Promise<
  MobileBackgroundAcknowledgement[]
> {
  if (!Capacitor.isNativePlatform()) return [];
  const result = await BackgroundRunner.dispatchEvent<{
    acknowledged?: MobileBackgroundAcknowledgement[];
  }>({
    label: RUNNER_LABEL,
    event: "getOfflineAcknowledgements",
    details: {},
  });
  return Array.isArray(result?.acknowledged) ? result.acknowledged : [];
}

export async function clearMobileBackgroundAcknowledgements(): Promise<void> {
  if (!Capacitor.isNativePlatform()) return;
  await BackgroundRunner.dispatchEvent({
    label: RUNNER_LABEL,
    event: "clearOfflineAcknowledgements",
    details: {},
  });
}

export function scheduleMobileBackgroundSyncMirror(): void {
  void mirrorMobileBackgroundSyncState().catch(() => {
    // Foreground replay remains the durable retry path.
  });
}
