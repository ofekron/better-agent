import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Hoisted mock surface. vi.hoisted lifts these above the vi.mock factories so
// factories reference the live objects, and each test can flip `native` /
// reconfigure call returns. The mock fn instances persist across
// vi.resetModules() so their call state stays observable per test.
const mocks = vi.hoisted(() => ({
  native: false,
  platform: "ios",
  prefsGet: vi.fn(),
  prefsSet: vi.fn(),
  addListener: vi.fn(),
  checkPermissions: vi.fn(),
  requestPermissions: vi.fn(),
  register: vi.fn(),
  registerPushToken: vi.fn(),
  unregisterPushToken: vi.fn(),
  navigateRoute: vi.fn(),
  parseRoutePath: vi.fn(),
  requestMessageFocus: vi.fn(),
  scheduleSync: vi.fn(),
}));

vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => mocks.native, getPlatform: () => mocks.platform },
}));
vi.mock("@capacitor/preferences", () => ({
  Preferences: { get: mocks.prefsGet, set: mocks.prefsSet },
}));
vi.mock("@capacitor/push-notifications", () => ({
  PushNotifications: {
    addListener: mocks.addListener,
    checkPermissions: mocks.checkPermissions,
    requestPermissions: mocks.requestPermissions,
    register: mocks.register,
  },
}));
vi.mock("../src/api", () => ({
  registerPushToken: mocks.registerPushToken,
  unregisterPushToken: mocks.unregisterPushToken,
}));
vi.mock("../src/hooks/useRoute", () => ({
  navigateRoute: mocks.navigateRoute,
  parseRoutePath: mocks.parseRoutePath,
  sessionPath: (id: string) => `/s/${id}`,
  ROUTE_NAVIGATE_EVENT: "ba:route-navigate",
}));
vi.mock("../src/askSession", () => ({ ASK_SINGLETON_ID: "virtual:ask:ask" }));
vi.mock("../src/lib/uuid", () => ({ uuidv4: () => "uuid-fixed" }));
vi.mock("../src/lib/mobileBackgroundSync", () => ({ scheduleMobileBackgroundSyncMirror: mocks.scheduleSync }));
vi.mock("../src/utils/messageFocus", () => ({ requestMessageFocus: mocks.requestMessageFocus }));

import type * as PushMod from "../src/utils/mobilePushNotifications";

type AddListenerCall = [event: string, handler: (event: unknown) => void];
function listenerFor(event: string): ((event: unknown) => void) | undefined {
  const call = mocks.addListener.mock.calls.find((c) => c[0] === event) as AddListenerCall | undefined;
  return call?.[1];
}

/** Drain queued microtasks. subscribeCurrentSession is fire-and-forget
 * (`void subscribeCurrentSession()`) over several awaited resolved promises,
 * so a real macrotask boundary lets the whole chain settle before we assert. */
const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

describe("mobilePushNotifications", () => {
  let mod: typeof PushMod;
  let windowListeners: Array<[string, EventListener]>;

  beforeEach(async () => {
    vi.resetModules();
    // Clear accumulated call records from prior tests (keeps mockResolvedValue
    // implementations; the per-mock resets below re-establish the rest).
    vi.clearAllMocks();

    mocks.native = false;
    mocks.platform = "ios";

    mocks.prefsGet.mockResolvedValue({ value: undefined });
    mocks.prefsSet.mockResolvedValue(undefined);
    mocks.addListener.mockResolvedValue(undefined);
    mocks.checkPermissions.mockResolvedValue({ receive: "granted" });
    mocks.requestPermissions.mockResolvedValue({ receive: "granted" });
    mocks.register.mockResolvedValue(undefined);
    mocks.registerPushToken.mockResolvedValue(undefined);
    mocks.unregisterPushToken.mockResolvedValue(undefined);
    mocks.parseRoutePath.mockReturnValue({ kind: "machines" });
    mocks.navigateRoute.mockImplementation(() => undefined);
    mocks.requestMessageFocus.mockImplementation(() => undefined);
    mocks.scheduleSync.mockImplementation(() => undefined);

    // Capture window listeners without attaching them, so dispatching real
    // events in one test can't fire into another.
    windowListeners = [];
    vi.spyOn(window, "addEventListener").mockImplementation(((type: string, listener: EventListener) => {
      windowListeners.push([type, listener]);
      return undefined;
    }) as EventTarget["addEventListener"]);

    mod = await import("../src/utils/mobilePushNotifications");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  async function initNative(permission: "granted" | "denied" | "prompt-granted") {
    mocks.native = true;
    if (permission === "denied") {
      mocks.checkPermissions.mockResolvedValue({ receive: "denied" });
      mocks.requestPermissions.mockResolvedValue({ receive: "denied" });
    } else if (permission === "prompt-granted") {
      mocks.checkPermissions.mockResolvedValue({ receive: "prompt" });
      mocks.requestPermissions.mockResolvedValue({ receive: "granted" });
    } else {
      mocks.checkPermissions.mockResolvedValue({ receive: "granted" });
    }
    await mod.initMobilePushNotifications();
  }

  async function deliverToken(value = "tok-abc") {
    listenerFor("registration")!({ value });
    await flush();
  }

  function routeListener() {
    return windowListeners.find(([t]) => t === "ba:route-navigate")![1];
  }

  describe("getOrCreateMobilePushDeviceId", () => {
    it("creates and persists a new device id when none exists", async () => {
      mocks.prefsGet.mockResolvedValue({ value: undefined });
      const id = await mod.getOrCreateMobilePushDeviceId();
      expect(id).toBe("uuid-fixed");
      expect(mocks.prefsSet).toHaveBeenCalledWith({ key: "better_agent_push_device_id", value: "uuid-fixed" });
    });

    it("reuses an existing device id without writing", async () => {
      mocks.prefsGet.mockResolvedValue({ value: "existing-id" });
      const id = await mod.getOrCreateMobilePushDeviceId();
      expect(id).toBe("existing-id");
      expect(mocks.prefsSet).not.toHaveBeenCalled();
    });

    it("caches the lookup — a second call does not re-query storage", async () => {
      mocks.prefsGet.mockResolvedValue({ value: undefined });
      await mod.getOrCreateMobilePushDeviceId();
      await mod.getOrCreateMobilePushDeviceId();
      expect(mocks.prefsGet).toHaveBeenCalledTimes(1);
    });
  });

  describe("initMobilePushNotifications", () => {
    it("is a no-op on web (not native)", async () => {
      mocks.native = false;
      await mod.initMobilePushNotifications();
      expect(mocks.scheduleSync).not.toHaveBeenCalled();
      expect(mocks.addListener).not.toHaveBeenCalled();
      expect(mocks.register).not.toHaveBeenCalled();
    });

    it("registers listeners and the device when permission is granted", async () => {
      await initNative("granted");
      expect(mocks.scheduleSync).toHaveBeenCalledTimes(1);
      expect(mocks.addListener).toHaveBeenCalledTimes(3);
      expect(windowListeners.map(([t]) => t)).toEqual(["ba:route-navigate", "popstate"]);
      expect(mocks.register).toHaveBeenCalledTimes(1);
    });

    it("requests permission when the OS would prompt, then registers", async () => {
      await initNative("prompt-granted");
      expect(mocks.requestPermissions).toHaveBeenCalledTimes(1);
      expect(mocks.register).toHaveBeenCalledTimes(1);
    });

    it("does not register when permission is denied", async () => {
      await initNative("denied");
      expect(mocks.register).not.toHaveBeenCalled();
      expect(mocks.addListener).toHaveBeenCalledTimes(3);
    });

    it("is idempotent — a second native init no-ops", async () => {
      await initNative("granted");
      await mod.initMobilePushNotifications();
      expect(mocks.scheduleSync).toHaveBeenCalledTimes(1);
      expect(mocks.register).toHaveBeenCalledTimes(1);
    });

    it("fires console.error on a registrationError event", async () => {
      const errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
      await initNative("granted");
      listenerFor("registrationError")!({ error: "boom" });
      expect(errSpy).toHaveBeenCalled();
    });
  });

  describe("registration + subscribeCurrentSession", () => {
    it("subscribes the viewed session when a token arrives", async () => {
      mocks.parseRoutePath.mockReturnValue({ kind: "session", sessionId: "sess-1" });
      await initNative("granted");
      await deliverToken();
      expect(mocks.registerPushToken).toHaveBeenCalledWith("uuid-fixed", "tok-abc", "ios", "sess-1");
    });

    it("does not re-subscribe the same session twice", async () => {
      mocks.parseRoutePath.mockReturnValue({ kind: "session", sessionId: "sess-1" });
      await initNative("granted");
      await deliverToken();
      await routeListener()(new Event("ba:route-navigate"));
      await flush();
      expect(mocks.registerPushToken).toHaveBeenCalledTimes(1);
    });

    it("subscribes again when navigation lands on a different session", async () => {
      mocks.parseRoutePath.mockReturnValue({ kind: "session", sessionId: "sess-1" });
      await initNative("granted");
      await deliverToken();
      mocks.parseRoutePath.mockReturnValue({ kind: "session", sessionId: "sess-2" });
      await routeListener()(new Event("ba:route-navigate"));
      await flush();
      expect(mocks.registerPushToken).toHaveBeenNthCalledWith(2, "uuid-fixed", "tok-abc", "ios", "sess-2");
    });

    it("skips subscription while there is no token yet", async () => {
      mocks.parseRoutePath.mockReturnValue({ kind: "session", sessionId: "sess-1" });
      await initNative("granted");
      // No registration event → cachedToken stays null.
      await routeListener()(new Event("ba:route-navigate"));
      await flush();
      expect(mocks.registerPushToken).not.toHaveBeenCalled();
    });

    it("skips subscription when not viewing a session route", async () => {
      mocks.parseRoutePath.mockReturnValue({ kind: "settings" });
      await initNative("granted");
      await deliverToken();
      expect(mocks.registerPushToken).not.toHaveBeenCalled();
    });

    it("skips subscription for the ask singleton route", async () => {
      mocks.parseRoutePath.mockReturnValue({ kind: "session", sessionId: "virtual:ask:ask" });
      await initNative("granted");
      await deliverToken();
      expect(mocks.registerPushToken).not.toHaveBeenCalled();
    });

    it("swallows a registration failure without throwing and stays retryable", async () => {
      mocks.parseRoutePath.mockReturnValue({ kind: "session", sessionId: "sess-1" });
      mocks.registerPushToken.mockRejectedValueOnce(new Error("network"));
      await initNative("granted");
      await deliverToken();
      // Failed registration did not advance lastSubscribedSessionId: a later
      // route listener on the same session retries.
      await routeListener()(new Event("ba:route-navigate"));
      await flush();
      expect(mocks.registerPushToken).toHaveBeenCalledTimes(2);
    });

    it("also subscribes on a popstate navigation", async () => {
      mocks.parseRoutePath.mockReturnValue({ kind: "session", sessionId: "sess-9" });
      await initNative("granted");
      await deliverToken();
      mocks.parseRoutePath.mockReturnValue({ kind: "session", sessionId: "sess-10" });
      const pop = windowListeners.find(([t]) => t === "popstate")![1];
      await pop(new Event("popstate"));
      await flush();
      expect(mocks.registerPushToken).toHaveBeenNthCalledWith(2, "uuid-fixed", "tok-abc", "ios", "sess-10");
    });
  });

  describe("pushNotificationActionPerformed deep-linking", () => {
    async function fireAction(data: unknown) {
      await initNative("granted");
      listenerFor("pushNotificationActionPerformed")!({ notification: { data } });
    }

    it("focuses the message and navigates with ?m= when both ids are present", async () => {
      await fireAction({ session_id: "s1", message_id: "m1" });
      expect(mocks.requestMessageFocus).toHaveBeenCalledWith("s1", "m1");
      expect(mocks.navigateRoute).toHaveBeenCalledWith("/s/s1?m=m1");
    });

    it("navigates to the session only when there is no message id", async () => {
      await fireAction({ session_id: "s1" });
      expect(mocks.requestMessageFocus).not.toHaveBeenCalled();
      expect(mocks.navigateRoute).toHaveBeenCalledWith("/s/s1");
    });

    it("does nothing when session_id is absent or non-string", async () => {
      await fireAction({ message_id: "m1" });
      await fireAction({ session_id: 42 });
      expect(mocks.navigateRoute).not.toHaveBeenCalled();
      expect(mocks.requestMessageFocus).not.toHaveBeenCalled();
    });
  });

  describe("teardownMobilePushNotifications", () => {
    it("is a no-op on web", async () => {
      mocks.native = false;
      await mod.teardownMobilePushNotifications();
      expect(mocks.unregisterPushToken).not.toHaveBeenCalled();
    });

    it("unregisters the device on native", async () => {
      mocks.native = true;
      await mod.teardownMobilePushNotifications();
      expect(mocks.unregisterPushToken).toHaveBeenCalledWith("uuid-fixed");
    });

    it("swallows unregister failure and still resets the subscribed session", async () => {
      mocks.native = true;
      mocks.parseRoutePath.mockReturnValue({ kind: "session", sessionId: "sess-1" });
      await initNative("granted");
      await deliverToken();
      mocks.unregisterPushToken.mockRejectedValueOnce(new Error("offline"));

      await expect(mod.teardownMobilePushNotifications()).resolves.toBeUndefined();

      // After teardown, the same session is re-subscribable (reset by finally).
      await routeListener()(new Event("ba:route-navigate"));
      await flush();
      expect(mocks.registerPushToken).toHaveBeenCalled();
    });
  });
});
