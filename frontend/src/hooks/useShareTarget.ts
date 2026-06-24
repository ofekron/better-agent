import { useEffect } from "react";
import { Capacitor } from "@capacitor/core";
import { App as CapApp } from "@capacitor/app";
import type { PastedImage } from "../types";
import { fileToPastedImage } from "../utils/imageAttach";

/** Decode a base64 string into a Blob of the given MIME type so it can
 *  flow through the same {@link fileToPastedImage} resize/encode path as
 *  composer attachments. */
function base64ToBlob(base64: string, mimeType: string): Blob {
  const bytes = atob(base64);
  const arr = new Uint8Array(bytes.length);
  for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
  return new Blob([arr], { type: mimeType });
}

interface ShareResult {
  title?: string;
  description?: string;
  type?: string;
  url?: string;
  additionalItems?: Array<{ url?: string; type?: string }>;
}

/** Pull every shared file URL out of a send-intent result, covering
 *  both single (ACTION_SEND / one iOS attachment) and multiple
 *  (ACTION_SEND_MULTIPLE) deliveries. */
function extractItems(r: ShareResult): Array<{ url: string; type?: string }> {
  const items: Array<{ url: string; type?: string }> = [];
  if (r.url) items.push({ url: r.url, type: r.type });
  for (const extra of r.additionalItems ?? []) {
    if (extra.url) items.push({ url: extra.url, type: extra.type });
  }
  return items;
}

/**
 * OS share-sheet ingestion (native only). When the user shares
 * screenshot(s) into the app, reads the shared file bytes and hands the
 * resulting {@link PastedImage}s to `onImages`. No-op on web.
 *
 * Delivery is covered three ways so cold-start, warm-resume, and the
 * iOS Share-Extension URL-scheme launch all surface the payload:
 *  - on mount (cold start: app launched by the share)
 *  - on Capacitor `appUrlOpen` (iOS extension opens the app via scheme)
 *  - on Capacitor `resume` (warm resume / Android onNewIntent)
 */
export function useShareTarget(onImages: (images: PastedImage[]) => void): void {
  useEffect(() => {
    if (!Capacitor.isNativePlatform()) return;

    let cancelled = false;
    const removers: Array<() => void> = [];

    const process = async () => {
      const { SendIntent } = await import("send-intent");
      const { Filesystem } = await import("@capacitor/filesystem");
      let result: ShareResult;
      try {
        result = (await SendIntent.checkSendIntentReceived()) as ShareResult;
      } catch {
        return; // no pending intent
      }
      const items = extractItems(result);
      if (items.length === 0) return;

      const images: PastedImage[] = [];
      for (const item of items) {
        const path = decodeURIComponent(item.url);
        const file = await Filesystem.readFile({ path });
        const mimeType = item.type?.startsWith("image/")
          ? item.type
          : "image/png";
        const blob = base64ToBlob(file.data as string, mimeType);
        images.push(await fileToPastedImage(blob));
      }
      // Release the native intent so it isn't re-delivered next resume.
      SendIntent.finish();
      if (!cancelled && images.length > 0) onImages(images);
    };

    process();

    CapApp.addListener("appUrlOpen", () => process()).then((h) =>
      removers.push(() => h.remove())
    );
    CapApp.addListener("resume", () => process()).then((h) =>
      removers.push(() => h.remove())
    );

    return () => {
      cancelled = true;
      removers.forEach((r) => r());
    };
  }, [onImages]);
}
