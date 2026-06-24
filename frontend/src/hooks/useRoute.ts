import { useCallback, useEffect, useState } from "react";
import { ASK_SINGLETON_ID } from "../askSession";

/** Path patterns the app cares about. */
export type Route =
  | { kind: "session"; sessionId: string }
  | { kind: "machines" }
  | { kind: "settings" }
  | { kind: "share" }
  | { kind: "providerConfigSync" }
  | { kind: "analytics" };

function parse(pathname: string): Route {
  if (pathname === "/machines" || pathname === "/machines/") {
    return { kind: "machines" };
  }
  if (pathname === "/settings" || pathname === "/settings/") {
    return { kind: "settings" };
  }
  if (pathname === "/analytics" || pathname === "/analytics/") {
    return { kind: "analytics" };
  }
  if (pathname === "/share" || pathname === "/share/") {
    return { kind: "share" };
  }
  if (pathname === "/provider-config-sync" || pathname === "/provider-config-sync/") {
    return { kind: "providerConfigSync" };
  }
  const m = pathname.match(/^\/s\/([^/]+)\/?$/);
  if (m) return { kind: "session", sessionId: decodeURIComponent(m[1]) };
  // `/` (and any unknown path) lands on the Ask singleton — the app's
  // entry point. We don't 404 client-side; the backend serves the SPA
  // on every path so the SPA gets a chance to route.
  return { kind: "session", sessionId: ASK_SINGLETON_ID };
}

/** Hand-rolled router. Replaces a full react-router dependency for
 * the two routes this app needs (`/` and `/s/:id`). Browser back/
 * forward fires `popstate` which we listen for; `navigate(path)`
 * pushes a history entry and updates local state in one tick. */
export function useRoute(): {
  route: Route;
  navigate: (path: string) => void;
} {
  const [route, setRoute] = useState<Route>(() => parse(window.location.pathname));

  useEffect(() => {
    const onPop = () => setRoute(parse(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback((path: string) => {
    // Idempotent: skip pushState when the URL is already there
    // (prevents history spam from React Strict Mode double-invoke
    // and from inner components that "navigate to current" defensively).
    if (window.location.pathname === path) {
      setRoute(parse(path));
      return;
    }
    window.history.pushState(null, "", path);
    setRoute(parse(path));
  }, []);

  return { route, navigate };
}

/** Convenience: build a session URL from an id. Centralized so a
 * future change (e.g. `/s/:rootId/f/:forkId`) only edits one place. */
export function sessionPath(sessionId: string): string {
  return `/s/${encodeURIComponent(sessionId)}`;
}
