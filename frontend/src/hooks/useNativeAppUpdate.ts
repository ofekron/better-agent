import { useCallback, useEffect, useState } from "react";
import { Capacitor } from "@capacitor/core";
import { App as CapApp, type AppState } from "@capacitor/app";
import { API } from "../api";

export interface NativeUpdate {
  code: number;
  versionName: string | null;
}

// Persist the code the user dismissed so a nag doesn't reappear every
// launch until an even newer build is staged.
const DISMISS_KEY = "bc_update_dismissed_code";

interface MobileStatus {
  version_code?: number;
  version_name?: string | null;
}

/** Native-only: detects when the backend has a newer APK staged than the
 * running build, and exposes a dismiss() so the user can skip a version.
 * Returns null on web or while idle. */
export function useNativeAppUpdate(): {
  update: NativeUpdate | null;
  dismiss: () => void;
} {
  const [update, setUpdate] = useState<NativeUpdate | null>(null);

  const check = useCallback(async () => {
    if (!Capacitor.isNativePlatform()) return;
    try {
      const info = await CapApp.getInfo();
      const own = parseInt(String(info.build), 10) || 0;
      const res = await fetch(`${API}/api/mobile/status`, {
        credentials: "include",
      });
      if (!res.ok) return;
      const data = (await res.json()) as MobileStatus;
      const serverCode = typeof data.version_code === "number" ? data.version_code : 0;
      if (!serverCode || serverCode <= own) {
        setUpdate(null);
        return;
      }
      const dismissed = parseInt(localStorage.getItem(DISMISS_KEY) || "0", 10) || 0;
      setUpdate(serverCode <= dismissed ? null : { code: serverCode, versionName: data.version_name ?? null });
    } catch {
      // Offline / unreachable — leave current state; no popup spam.
    }
  }, []);

  useEffect(() => {
    if (!Capacitor.isNativePlatform()) return;
    void check();
    // Re-check when the user returns to the app (backend may have staged
    // a newer build while it was backgrounded).
    const handle = CapApp.addListener("appStateChange", (state: AppState) => {
      if (state.isActive) void check();
    });
    return () => {
      void handle.then((h) => h.remove());
    };
  }, [check]);

  const dismiss = useCallback(() => {
    setUpdate((cur) => {
      if (cur) localStorage.setItem(DISMISS_KEY, String(cur.code));
      return null;
    });
  }, []);

  return { update, dismiss };
}
