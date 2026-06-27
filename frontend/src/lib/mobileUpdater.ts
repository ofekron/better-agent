// Capacitor OTA updater (manual mode, self-hosted).
//
// The bundled web build is the offline fallback baked into the app. On
// launch we ask the user's OWN backend for the current bundle version; if
// it differs from what's running, we download + apply it. The download URL
// carries the bearer token as a query param because capgo's native HTTP GET
// can't send our Authorization header (the backend validates it the same way
// the WS endpoints do). notifyAppReady commits the running bundle so a
// broken update auto-rolls-back to the last good one.
import { Capacitor } from "@capacitor/core";
import { CapacitorUpdater } from "@capgo/capacitor-updater";
import { API } from "../api";
import { getStoredToken, withTokenQuery } from "../bearerAuth";

interface BundleManifest {
  version: string;
  checksum: string;
  download_path: string;
}

export async function runMobileOtaCheck(): Promise<void> {
  if (!Capacitor.isNativePlatform()) return;

  // Commit the currently-running bundle first, so capgo never rolls it back.
  try {
    await CapacitorUpdater.notifyAppReady();
  } catch {
    /* builtin bundle on a fresh install — nothing to commit yet */
  }

  // No token => not logged in yet; the manifest is auth-gated. Skip; the
  // next launch after login will pick it up.
  if (!getStoredToken()) return;

  try {
    const res = await fetch(`${API}/api/mobile/bundle/manifest`, {
      credentials: "include",
    });
    if (!res.ok) return;
    const manifest = (await res.json()) as BundleManifest;

    const current = await CapacitorUpdater.current();
    if (current?.bundle?.version === manifest.version) return;

    const url = withTokenQuery(`${API}${manifest.download_path}`);
    const bundle = await CapacitorUpdater.download({
      url,
      version: manifest.version,
      checksum: manifest.checksum,
    });
    // Activate + reload into the new bundle. The reloaded bundle calls
    // notifyAppReady (above) to commit itself.
    await CapacitorUpdater.set(bundle);
  } catch (e) {
    // Best-effort: on any failure the running bundle keeps working.
    console.error("mobile OTA check failed", e);
  }
}
