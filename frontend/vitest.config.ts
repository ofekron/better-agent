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
