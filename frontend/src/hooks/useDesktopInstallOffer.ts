import { useCallback, useEffect, useState } from "react";
import { Capacitor } from "@capacitor/core";
import { API } from "../api";

export type DesktopInstallPlatform = "macos" | "windows";

export interface DesktopInstallOffer {
  platform: DesktopInstallPlatform;
  label: string;
  url: string;
  version: string | null;
}

export interface DesktopStatus {
  macos?: boolean;
  windows?: boolean;
  desktop_shell?: boolean;
  version?: string | null;
}

const DISMISS_PREFIX = "bc_desktop_install_dismissed";

function browserPlatform(): DesktopInstallPlatform | null {
  const platform = `${navigator.platform || ""} ${navigator.userAgent || ""}`;
  if (/Mac/i.test(platform)) return "macos";
  if (/Win/i.test(platform)) return "windows";
  return null;
}

export function platformLabel(platform: DesktopInstallPlatform): string {
  return platform === "macos" ? "macOS" : "Windows";
}

export function downloadUrl(platform: DesktopInstallPlatform): string {
  return `${API}/api/download/desktop/${platform}`;
}

function dismissKey(platform: DesktopInstallPlatform, version: string | null): string {
  return `${DISMISS_PREFIX}:${platform}:${version || "unknown"}`;
}

export function useDesktopInstallOffer(): {
  offer: DesktopInstallOffer | null;
  dismiss: () => void;
} {
  const [offer, setOffer] = useState<DesktopInstallOffer | null>(null);

  useEffect(() => {
    if (Capacitor.isNativePlatform()) return;
    const platform = browserPlatform();
    if (!platform) return;

    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch(`${API}/api/desktop/status`, {
          credentials: "include",
        });
        if (!res.ok) return;
        const status = (await res.json()) as DesktopStatus;
        if (status.desktop_shell) return;
        if (!status[platform]) return;
        const version = status.version ?? null;
        if (localStorage.getItem(dismissKey(platform, version)) === "1") return;
        if (cancelled) return;
        setOffer({
          platform,
          label: platformLabel(platform),
          url: downloadUrl(platform),
          version,
        });
      } catch {
        return;
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  const dismiss = useCallback(() => {
    setOffer((cur) => {
      if (cur) localStorage.setItem(dismissKey(cur.platform, cur.version), "1");
      return null;
    });
  }, []);

  return { offer, dismiss };
}
