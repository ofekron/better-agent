import { Capacitor } from "@capacitor/core";
import { Browser } from "@capacitor/browser";

/** Open a URL externally — system browser on Capacitor, new tab on web. */
export async function openExternalLink(url: string): Promise<void> {
  if (Capacitor.isNativePlatform()) {
    await Browser.open({ url });
  } else {
    window.open(url, "_blank", "noopener,noreferrer");
  }
}
