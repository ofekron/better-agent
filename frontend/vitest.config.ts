import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Pin React to the frontend package so tests and linked source packages
// share the same runtime copy.
const REACT = path.resolve(__dirname, "node_modules/react");
const REACT_DOM = path.resolve(__dirname, "node_modules/react-dom");

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      src: path.resolve(__dirname, "src"),
      react: REACT,
      "react-dom": REACT_DOM,
      "@better-agent/provider-config-sync-core/diff": path.resolve(__dirname, "../provider-config-sync/packages/provider-config-sync-core/src/diff.ts"),
      "@better-agent/provider-config-sync-core/items": path.resolve(__dirname, "../provider-config-sync/packages/provider-config-sync-core/src/items.ts"),
      "@better-agent/provider-config-sync-core": path.resolve(__dirname, "../provider-config-sync/packages/provider-config-sync-core/src/index.ts"),
      "@better-agent/provider-config-sync-ui/styles.css": path.resolve(__dirname, "../provider-config-sync/packages/provider-config-sync-ui/src/styles.css"),
      "@better-agent/provider-config-sync-ui": path.resolve(__dirname, "../provider-config-sync/packages/provider-config-sync-ui/src/index.ts"),
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
