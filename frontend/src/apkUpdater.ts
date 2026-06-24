import { registerPlugin } from "@capacitor/core";

// Native counterpart: ApkUpdaterPlugin.java (com.betteragent.app).
// Downloads an APK from the backend (carrying the bearer token the auth
// gate requires) and hands it to Android's package installer.
interface ApkUpdaterPlugin {
  downloadAndInstall(options: {
    url: string;
    token: string;
  }): Promise<{ path: string }>;
}

export const ApkUpdater = registerPlugin<ApkUpdaterPlugin>("ApkUpdater", {
  // No-op on web — the updater only runs on the native Capacitor shell.
  web: {
    async downloadAndInstall() {
      throw new Error("APK update is only available in the native app.");
    },
  },
});
