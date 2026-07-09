import { useCallback, useEffect, useState } from "react";
import { ASK_SINGLETON_ID } from "../askSession";

/** Path patterns the app cares about. */
export type Route =
  | { kind: "session"; sessionId: string }
  | { kind: "emptyProject" }
  | { kind: "machines" }
  | { kind: "settings" }
  | { kind: "share" }
  | { kind: "providerConfigSync" }
  | { kind: "analytics" }
  | { kind: "communications" }
  | { kind: "schedules" }
  | { kind: "extensionPanel"; extensionId: string; panelId: string; resourceId: string };

function decodePathSegment(value: string): string | null {
  try {
    return decodeURIComponent(value);
  } catch {
    return null;
  }
}

export function parseRoutePath(pathname: string): Route {
  if (pathname === "/machines" || pathname === "/machines/") {
    return { kind: "machines" };
  }
  if (pathname === "/settings" || pathname === "/settings/") {
    return { kind: "settings" };
  }
  if (pathname === "/analytics" || pathname === "/analytics/") {
    return { kind: "analytics" };
  }
  if (pathname === "/communications" || pathname === "/communications/") {
    return { kind: "communications" };
  }
  if (pathname === "/schedules" || pathname === "/schedules/") {
    return { kind: "schedules" };
  }
  const extensionPanelMatch = pathname.match(/^\/extensions\/([^/]+)\/panels\/([^/]+)(?:\/([^/]+))?\/?$/);
  if (extensionPanelMatch) {
    const extensionId = decodePathSegment(extensionPanelMatch[1]);
    const panelId = decodePathSegment(extensionPanelMatch[2]);
    const resourceId = extensionPanelMatch[3] ? decodePathSegment(extensionPanelMatch[3]) : "";
    if (!extensionId || !panelId || resourceId === null) {
      return { kind: "session", sessionId: ASK_SINGLETON_ID };
    }
    return {
      kind: "extensionPanel",
      extensionId,
      panelId,
      resourceId,
    };
  }
  if (pathname === "/share" || pathname === "/share/") {
    return { kind: "share" };
  }
  if (pathname === "/provider-config-sync" || pathname === "/provider-config-sync/") {
    return { kind: "providerConfigSync" };
  }
  const m = pathname.match(/^\/s\/([^/]+)\/?$/);
  if (m) {
    const sessionId = decodePathSegment(m[1]);
    if (sessionId) return { kind: "session", sessionId };
  }
  // Selecting a (machine, project) that has no sessions lands here rather
  // than on the Ask singleton — Ask is reachable only via its explicit
  // button. The selected project/machine is read from the sidebar state.
  if (pathname === "/empty-project" || pathname === "/empty-project/") {
    return { kind: "emptyProject" };
  }
  // `/` (and any unknown path) lands on the Ask singleton — the app's
  // entry point. We don't 404 client-side; the backend serves the SPA
  // on every path so the SPA gets a chance to route.
  return { kind: "session", sessionId: ASK_SINGLETON_ID };
}

/** Hand-rolled router. Replaces a full react-router dependency for
 * the app-owned top-level surfaces. Browser back/forward fires
 * `popstate` which we listen for; `navigate(path)` pushes a history
 * entry and updates local state in one tick. */
export function useRoute(): {
  route: Route;
  navigate: (path: string) => void;
} {
  const [route, setRoute] = useState<Route>(() => parseRoutePath(window.location.pathname));

  useEffect(() => {
    const onPop = () => setRoute(parseRoutePath(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback((path: string) => {
    // Idempotent: skip pushState when the URL is already there
    // (prevents history spam from React Strict Mode double-invoke
    // and from inner components that "navigate to current" defensively).
    if (window.location.pathname === path) {
      setRoute(parseRoutePath(path));
      return;
    }
    window.history.pushState(null, "", path);
    setRoute(parseRoutePath(path));
  }, []);

  return { route, navigate };
}

/** Convenience: build a session URL from an id. Centralized so a
 * future change (e.g. `/s/:rootId/f/:forkId`) only edits one place. */
export function sessionPath(sessionId: string): string {
  return `/s/${encodeURIComponent(sessionId)}`;
}

export function extensionPanelPath(extensionId: string, panelId: string, resourceId = ""): string {
  const base = `/extensions/${encodeURIComponent(extensionId)}/panels/${encodeURIComponent(panelId)}`;
  return resourceId ? `${base}/${encodeURIComponent(resourceId)}` : base;
}
