import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Capacitor } from '@capacitor/core'
import { App as CapApp } from '@capacitor/app'
import { ErrorBoundary } from './components/ErrorBoundary'
import { cleanupRestoredModalSentinel, getModalStackSize } from './hooks/useBackButtonDismiss'
import { installBearerAuthInterceptor } from './bearerAuth'
import { clearHardRefreshMarker } from './lib/hardRefresh'
import { installFrontendLogger } from './lib/frontendLogger'
import { ScreenWakeLock } from './components/ScreenWakeLock'
import { loadBuiltinExtensionIds } from './extensionIds'
import './i18n'
import './styles/globals.css'
import App from './App'

// On Capacitor native, the WebView origin (http://localhost/) is
// cross-site to the backend, so SameSite=Lax drops the bc_session
// cookie on every fetch after login. Bearer-token auth via a request
// header sidesteps the cookie entirely. Installs BEFORE any module
// fires a request.
if (Capacitor.isNativePlatform()) {
  installBearerAuthInterceptor()
}
installFrontendLogger()

// Browsers restore `history.state` after a reload, so a modal sentinel
// pushed in the previous page lifetime survives — but the React
// component that owned it doesn't. Wipe the phantom BEFORE the app
// mounts so the next back press isn't silently swallowed.
cleanupRestoredModalSentinel()
clearHardRefreshMarker()

// Private/commercial extension ids are fetched from the backend (never
// hardcoded in this repo) and must be available before any runtime call
// site that builds an extension API URL.
loadBuiltinExtensionIds().finally(() => {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <ErrorBoundary>
        <ScreenWakeLock />
        <App />
      </ErrorBoundary>
    </StrictMode>,
  )
})

// The workbox service worker is intentionally NOT registered. On a
// localhost/LAN deployment the app always talks to a backend that is
// reachable whenever the page itself loaded, so SW precaching buys no
// offline value — but it actively caused two bugs: (1) its NavigationRoute
// served a STALE cached index.html (users kept seeing an old bundle after
// a rebuild), and (2) its NetworkOnly route was registered for GET only,
// so POST /api/auth/* fell through to the shell fallback and returned
// index.html (HTTP 200, HTML) — surfacing as "status 200" errors in the
// login/setup screens. Instead, proactively tear down any SW + caches a
// previous build registered, so existing installs self-heal on next load.
// (The offline-first action backlog lives in localStorage and is wholly
// independent of the service worker — it is unaffected by this.)
if ('serviceWorker' in navigator && !Capacitor.isNativePlatform()) {
  navigator.serviceWorker.getRegistrations()
    .then((regs) => regs.forEach((r) => r.unregister()))
    .catch(() => {})
  if ('caches' in window) {
    caches.keys()
      .then((keys) => keys.forEach((k) => caches.delete(k)))
      .catch(() => {})
  }
}

// In the native Capacitor shell the hardware/gesture back button checks the
// WebView's native navigation stack — which doesn't include pushState entries.
// Override it to use window.history instead so the SPA router (useRoute)
// receives the popstate and navigates back.
//
// useBackButtonDismiss leaves stale history entries after a modal is closed
// via X / backdrop (it replaceState's the sentinel but can't remove the
// entry). Each stale entry silently consumes a back press with no visible
// route change. To avoid that we loop: after each history.back(), check
// whether the popstate actually changed the route or dismissed a modal.
// If neither, it was a stale entry — skip it and try again. If popstate
// never fires (bottom of stack), exit the app.
if (Capacitor.isNativePlatform()) {
  let backToken = 0
  CapApp.addListener('backButton', () => {
    const token = ++backToken
    const before = window.location.pathname
    const stackBefore = getModalStackSize()

    const tryBack = () => {
      if (token !== backToken) return

      const timeoutId = setTimeout(() => {
        if (token !== backToken) return
        // popstate never fired — we're at the bottom of the stack
        CapApp.exitApp()
      }, 100)

      const onPop = () => {
        clearTimeout(timeoutId)
        if (token !== backToken) return

        const routeChanged = window.location.pathname !== before
        const modalDismissed = getModalStackSize() < stackBefore
        if (routeChanged || modalDismissed) return // consumed

        // Stale history entry — skip and keep going
        tryBack()
      }

      window.addEventListener('popstate', onPop, { once: true })
      window.history.back()
    }

    tryBack()
  })
}
