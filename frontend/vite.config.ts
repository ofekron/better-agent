import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'
import { execSync } from 'node:child_process'

// Build-time version identifiers injected as global constants.
function buildVersion(): string {
  try { return execSync('git rev-parse --short HEAD').toString().trim() } catch { return 'dev' }
}

export default defineConfig({
  base: '/',
  build: {
    // run.sh builds into a temporary sibling directory and swaps it into
    // place only after success, so a failed refresh keeps the previous dist.
    outDir: process.env.VITE_OUT_DIR || 'dist',
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('/node_modules/')) return undefined
          if (id.includes('/monaco-editor/') || id.includes('/@monaco-editor/')) {
            return 'vendor-monaco'
          }
          if (
            id.includes('/@better-agent/provider-config-sync-') ||
            id.includes('/diff/') ||
            id.includes('/codemirror/')
          ) {
            return 'vendor-provider-sync'
          }
          if (
            id.includes('/react-markdown/') ||
            id.includes('/remark-gfm/') ||
            id.includes('/rehype-highlight/') ||
            id.includes('/hast-util-') ||
            id.includes('/mdast-util-') ||
            id.includes('/micromark') ||
            id.includes('/unified/') ||
            id.includes('/unist-util-') ||
            id.includes('/highlight.js/')
          ) {
            return 'vendor-markdown'
          }
          if (id.includes('/@capacitor/')) {
            return 'vendor-capacitor'
          }
          if (
            id.includes('/react/') ||
            id.includes('/react-dom/') ||
            id.includes('/scheduler/') ||
            id.includes('/i18next/') ||
            id.includes('/react-i18next/')
          ) {
            return 'vendor-react'
          }
          return undefined
        },
      },
    },
  },
  define: {
    __BUILD_HASH__: JSON.stringify(buildVersion()),
    __BUILD_TIME__: JSON.stringify(new Date().toISOString()),
  },
  resolve: {
    alias: {
      'src': '/src',
    },
    dedupe: ['react', 'react-dom'],
  },
  // Proxy /api and /ws to the backend so the dev server and backend
  // share an origin. Same-origin lets the bc_session cookie ride
  // along on every fetch and WebSocket upgrade without CORS-with-
  // credentials gymnastics. Prod is already single-origin (backend
  // serves the built frontend at :8000).
  server: {
    host: true, // bind 0.0.0.0 so LAN/other-devices can reach the dev server
    port: 3000, // canonical dev port — see project-structure `running.md`
    strictPort: true, // fail fast instead of silently falling back to another port
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://localhost:8000",
        ws: true,
        changeOrigin: true,
      },
    },
  },
  plugins: [
    react(),
    VitePWA({
      injectRegister: null,
      registerType: 'autoUpdate',
      includeAssets: ['icon.svg'],
      manifest: {
        name: 'Better Agent',
        short_name: 'BetterAgent',
        description: 'Web UI for Claude Code',
        theme_color: '#111318',
        background_color: '#111318',
        display: 'standalone',
        orientation: 'any',
        start_url: '/',
        icons: [
          { src: 'icon-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'icon-512.png', sizes: '512x512', type: 'image/png' },
          {
            src: 'icon-512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
      },
      workbox: {
        globPatterns: ['**/*.{js,css,html,ico,png,svg,woff2}'],
        maximumFileSizeToCacheInBytes: 5 * 1024 * 1024,
        // skipWaiting + clientsClaim: a newly-installed SW activates
        // immediately and takes over every open client (no
        // close-all-tabs dance). Paired with handleRefreshApp's
        // `registration.update()` call, hitting ↻ fetches the latest
        // sw.js, swaps it in, and the subsequent window.location.reload()
        // returns the brand-new bundle.
        skipWaiting: true,
        clientsClaim: true,
        runtimeCaching: [
          {
            urlPattern: /^https?:\/\/.*\/api\//,
            handler: 'NetworkOnly',
          },
        ],
      },
    }),
  ],
})
