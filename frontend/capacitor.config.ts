import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "com.betteragent.app",
  appName: "Better Agent",
  webDir: "dist",
  // The WebView loads local files at http://localhost/ for the lifetime
  // of the app. The user enters the backend URL on first launch
  // (ServerSetup → localStorage), api.ts reads it at runtime, and all
  // API/WS calls go to that origin. Auth crosses the cross-origin gap
  // via bearer token (see bearerAuth.ts) so we never have to navigate
  // the WebView itself. CAPACITOR_SERVER_URL is only used to bake a
  // server URL at build time (non-Capacitor remote deploys).
  server: {
    url: process.env.CAPACITOR_SERVER_URL || undefined,
    androidScheme: "http",
  },
  plugins: {
    SplashScreen: {
      launchShowDuration: 0,
    },
  },
};

export default config;
