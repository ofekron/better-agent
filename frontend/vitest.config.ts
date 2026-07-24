import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Pin React to the frontend package so tests and linked source packages
// share the same runtime copy.
const REACT = path.resolve(__dirname, "node_modules/react");
const REACT_DOM = path.resolve(__dirname, "node_modules/react-dom");

// Web shims for Capacitor plugins — mirror vite.config.ts so the test
// graph (which imports the full App) resolves `@capacitor/*` to the same
// no-op web implementations instead of the native packages.
const webPlatformAliases = {
  "@capacitor-community/speech-recognition": fileURLToPath(new URL("./src/platform/web/speech-recognition.ts", import.meta.url)),
  "@capacitor/app": fileURLToPath(new URL("./src/platform/web/capacitor-app.ts", import.meta.url)),
  "@capacitor/browser": fileURLToPath(new URL("./src/platform/web/capacitor-browser.ts", import.meta.url)),
  "@capacitor/core": fileURLToPath(new URL("./src/platform/web/capacitor-core.ts", import.meta.url)),
  "@capacitor/filesystem": fileURLToPath(new URL("./src/platform/web/capacitor-filesystem.ts", import.meta.url)),
  "@capacitor/preferences": fileURLToPath(new URL("./src/platform/web/capacitor-preferences.ts", import.meta.url)),
  "@capacitor/push-notifications": fileURLToPath(new URL("./src/platform/web/push-notifications.ts", import.meta.url)),
  "@capgo/capacitor-updater": fileURLToPath(new URL("./src/platform/web/capacitor-updater.ts", import.meta.url)),
  "send-intent": fileURLToPath(new URL("./src/platform/web/send-intent.ts", import.meta.url)),
};

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      src: path.resolve(__dirname, "src"),
      react: REACT,
      "react-dom": REACT_DOM,
      ...webPlatformAliases,
    },
  },
  test: {
    environment: "happy-dom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}"],
    css: false,
    // Force vite to bundle animation packages so their React imports
    // resolve through the aliases above.
    server: {
      deps: {
        inline: [/framer-motion/, /motion-dom/, /motion-utils/],
      },
    },
  },
});
