import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { createWebPlatformAliases } from "./vite.web-platform-aliases";

// Pin React to the frontend package so tests and linked source packages
// share the same runtime copy.
const REACT = path.resolve(__dirname, "node_modules/react");
const REACT_DOM = path.resolve(__dirname, "node_modules/react-dom");

const webPlatformAliases = createWebPlatformAliases(__dirname);

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      src: path.resolve(__dirname, "src"),
      react: REACT,
      "react-dom": REACT_DOM,
      "@monaco-editor/react": path.resolve(
        __dirname,
        "tests/stubs/monaco-editor-react.tsx",
      ),
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
    // Unit-tier coverage. Opt-in via `vitest run --coverage` so normal
    // `npm test` runs stay fast. `all: true` reports every file under
    // src/ so uncovered modules surface at 0% instead of being hidden.
    coverage: {
      provider: "v8",
      all: true,
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.d.ts",
        "src/**/*.test.{ts,tsx}",
        "src/**/__mocks__/**",
        "src/**/types.ts",
      ],
      reporter: ["text", "json", "html"],
      reportsDirectory: "./coverage-unit",
    },
  },
});
