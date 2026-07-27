// Capacitor OTA updater (manual mode, self-hosted).
//
// The bundled web build is the offline fallback baked into the app. On
// launch we ask the user's OWN backend for the current bundle version; if
// it differs from what's running, we download + apply it. The download URL
// carries a short-lived capability scoped to the exact bundle because capgo's
// native HTTP GET can't send our Authorization header.
import { Capacitor } from "@capacitor/core";
import { CapacitorUpdater } from "@capgo/capacitor-updater";
import { API } from "../api";
import { getStoredToken } from "../bearerAuth";

interface BundleManifest {
  version: string;
  checksum: string;
  download_path: string;
}

const MANIFEST_FETCH_TIMEOUT_MS = 4_000;
const UPDATE_CHECK_DELAY_MS = 3_000;

async function commitRunningMobileBundle(): Promise<void> {
  if (!Capacitor.isNativePlatform()) return;
  await CapacitorUpdater.notifyAppReady();
}

export function initializeMobileUpdater(onCommitError: (error: unknown) => void): void {
  if (!Capacitor.isNativePlatform()) return;
  const bundleCommitted = commitRunningMobileBundle().then(
    () => true,
    (error) => {
      onCommitError(error);
      return false;
    },
  );
  window.setTimeout(() => {
    void bundleCommitted.then((committed) => {
      if (!committed) return;
      return runMobileOtaCheck();
    });
  }, UPDATE_CHECK_DELAY_MS);
}

export async function runMobileOtaCheck(): Promise<void> {
  if (!Capacitor.isNativePlatform()) return;

  // No token => not logged in yet; the manifest is auth-gated. Skip; the
  // next launch after login will pick it up.
  if (!getStoredToken()) return;

  try {
    const controller = new AbortController();
    const timeout = window.setTimeout(
      () => controller.abort(new DOMException("bundle manifest fetch timed out", "TimeoutError")),
      MANIFEST_FETCH_TIMEOUT_MS,
    );
    let res: Response;
    try {
      res = await fetch(`${API}/api/mobile/bundle/manifest`, {
        credentials: "include",
        signal: controller.signal,
      });
    } finally {
      window.clearTimeout(timeout);
    }
    if (!res.ok) return;
    const manifest = (await res.json()) as BundleManifest;

    const current = await CapacitorUpdater.current();
    if (current?.bundle?.version === manifest.version) return;

    const url = `${API}${manifest.download_path}`;
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
