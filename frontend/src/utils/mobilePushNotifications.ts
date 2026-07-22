import { Capacitor } from "@capacitor/core";
import { Preferences } from "@capacitor/preferences";
import { PushNotifications, type ActionPerformed, type Token } from "@capacitor/push-notifications";
import { registerPushToken, unregisterPushToken } from "../api";
import { navigateRoute, parseRoutePath, sessionPath, ROUTE_NAVIGATE_EVENT } from "../hooks/useRoute";
import { ASK_SINGLETON_ID } from "../askSession";
import { uuidv4 } from "../lib/uuid";

const DEVICE_ID_KEY = "better_agent_push_device_id";

let deviceIdPromise: Promise<string> | null = null;
let initialized = false;
let cachedToken: string | null = null;
let lastSubscribedSessionId: string | null = null;

async function getOrCreateDeviceId(): Promise<string> {
  if (!deviceIdPromise) {
    deviceIdPromise = (async () => {
      const existing = await Preferences.get({ key: DEVICE_ID_KEY });
      if (existing.value) return existing.value;
      const created = uuidv4();
      await Preferences.set({ key: DEVICE_ID_KEY, value: created });
      return created;
    })();
  }
  return deviceIdPromise;
}

function currentSessionId(): string | null {
  const route = parseRoutePath(window.location.pathname);
  if (route.kind !== "session" || route.sessionId === ASK_SINGLETON_ID) return null;
  return route.sessionId;
}

function deepLinkToRequest(data: Record<string, unknown> | undefined | null): void {
  const sessionId = typeof data?.session_id === "string" ? data.session_id : null;
  if (!sessionId) return;
  navigateRoute(sessionPath(sessionId));
}

// The backend associates a device token with the set of sessions it should
// receive pending-approval pushes for (POST /api/push-tokens appends
// session_id to that device's subscription list — see
// backend/device_token_store.py:register_token). It requires a non-empty
// session_id per call, so registration happens once we have BOTH a device
// token and a session actually being viewed, and again every time the
// viewed session changes (route navigation), rather than once at boot.
async function subscribeCurrentSession(): Promise<void> {
  if (!cachedToken) return;
  const sessionId = currentSessionId();
  if (!sessionId || sessionId === lastSubscribedSessionId) return;
  const deviceId = await getOrCreateDeviceId();
  try {
    await registerPushToken(deviceId, cachedToken, Capacitor.getPlatform(), sessionId);
    lastSubscribedSessionId = sessionId;
  } catch {
    // Will retry on the next route change or app launch; never blocks navigation.
  }
}

/** Registers this install for push notifications and wires the
 * registration/action listeners. Native-only (Capacitor); no-op on web,
 * mirroring how the desktop/web notification path (userInputNotifications.ts)
 * is a separate, OS-appropriate implementation of the same "notify about a
 * pending user-input/approval request" concern. Idempotent — safe to call
 * again (e.g. re-authentication) without registering duplicate listeners. */
export async function initMobilePushNotifications(): Promise<void> {
  if (!Capacitor.isNativePlatform() || initialized) return;
  initialized = true;

  await PushNotifications.addListener("registration", (token: Token) => {
    cachedToken = token.value;
    void subscribeCurrentSession();
  });

  await PushNotifications.addListener("registrationError", (error) => {
    console.error("Push notification registration failed", error);
  });

  await PushNotifications.addListener("pushNotificationActionPerformed", (action: ActionPerformed) => {
    deepLinkToRequest(action.notification.data as Record<string, unknown> | undefined);
  });

  window.addEventListener(ROUTE_NAVIGATE_EVENT, () => void subscribeCurrentSession());
  window.addEventListener("popstate", () => void subscribeCurrentSession());

  let permStatus = await PushNotifications.checkPermissions();
  if (permStatus.receive === "prompt") {
    permStatus = await PushNotifications.requestPermissions();
  }
  if (permStatus.receive !== "granted") return;

  await PushNotifications.register();
}

/** Best-effort unregister of this install's push token. Never throws —
 * logout must proceed even if the backend is unreachable. */
export async function teardownMobilePushNotifications(): Promise<void> {
  if (!Capacitor.isNativePlatform()) return;
  try {
    const deviceId = await getOrCreateDeviceId();
    await unregisterPushToken(deviceId);
  } catch {
    // Best-effort — logout must proceed regardless of network reachability.
  } finally {
    lastSubscribedSessionId = null;
  }
}
