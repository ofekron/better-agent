import { describe, it, expect, vi } from "vitest";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import {
  navigateRoute,
  ROUTE_NAVIGATE_EVENT,
  useRoute,
  sessionPath,
  type Route,
} from "../src/hooks/useRoute";

/** Mount a probe that surfaces the current route + navigate fn. */
function mountRouter() {
  let api: { route: Route; navigate: (p: string) => void } | null = null;
  function Probe() {
    api = useRoute();
    return null;
  }
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => root.render(React.createElement(Probe)));
  return {
    get: () => api!,
    cleanup: () => {
      act(() => root.unmount());
      container.remove();
    },
  };
}

describe("useRoute — /share", () => {
  it("navigate('/share') yields route.kind === 'share'", () => {
    window.history.pushState(null, "", "/");
    const r = mountRouter();
    act(() => r.get().navigate("/share"));
    expect(r.get().route.kind).toBe("share");
    r.cleanup();
  });

  it("a session path still parses as a session route", () => {
    window.history.pushState(null, "", "/");
    const r = mountRouter();
    act(() => r.get().navigate(sessionPath("abc")));
    expect(r.get().route).toEqual({ kind: "session", sessionId: "abc" });
    r.cleanup();
  });
});

describe("navigateRoute", () => {
  it("uses one internal route event and never synthesizes popstate", () => {
    window.history.pushState(null, "", "/");
    const routeEvent = vi.fn();
    const popstate = vi.fn();
    window.addEventListener(ROUTE_NAVIGATE_EVENT, routeEvent);
    window.addEventListener("popstate", popstate);

    navigateRoute("/s/session-1");

    expect(routeEvent).toHaveBeenCalledOnce();
    expect(popstate).not.toHaveBeenCalled();
    window.removeEventListener(ROUTE_NAVIGATE_EVENT, routeEvent);
    window.removeEventListener("popstate", popstate);
  });

  it("rejects cross-origin routes before mutating history or notifying", () => {
    window.history.pushState(null, "", "/");
    const pushState = vi.spyOn(window.history, "pushState");
    const routeEvent = vi.fn();
    window.addEventListener(ROUTE_NAVIGATE_EVENT, routeEvent);

    expect(() => navigateRoute("https://example.com/s/session-1")).toThrow(
      "Route navigation must stay on the current origin",
    );

    expect(pushState).not.toHaveBeenCalled();
    expect(routeEvent).not.toHaveBeenCalled();
    window.removeEventListener(ROUTE_NAVIGATE_EVENT, routeEvent);
    pushState.mockRestore();
  });

  it("deduplicates the exact URL while keeping query and hash changes distinct", () => {
    window.history.pushState(null, "", "/s/session-1?m=one#top");
    const pushState = vi.spyOn(window.history, "pushState");
    const routeEvent = vi.fn();
    window.addEventListener(ROUTE_NAVIGATE_EVENT, routeEvent);

    navigateRoute("/s/session-1?m=one#top");
    navigateRoute("/s/session-1?m=two#top");
    navigateRoute("/s/session-1?m=two#bottom");

    expect(pushState).toHaveBeenCalledTimes(2);
    expect(routeEvent).toHaveBeenCalledTimes(3);
    window.removeEventListener(ROUTE_NAVIGATE_EVENT, routeEvent);
    pushState.mockRestore();
  });
});

describe("useRoute — /provider-config-sync", () => {
  it("parses /provider-config-sync on initial load", () => {
    window.history.pushState(null, "", "/provider-config-sync");
    const r = mountRouter();
    expect(r.get().route.kind).toBe("providerConfigSync");
    r.cleanup();
  });

  it("navigate('/provider-config-sync') yields route.kind === 'providerConfigSync'", () => {
    window.history.pushState(null, "", "/");
    const r = mountRouter();
    act(() => r.get().navigate("/provider-config-sync"));
    expect(r.get().route.kind).toBe("providerConfigSync");
    r.cleanup();
  });
});

describe("useRoute — /settings", () => {
  it("parses /settings on initial load", () => {
    window.history.pushState(null, "", "/settings");
    const r = mountRouter();
    expect(r.get().route.kind).toBe("settings");
    r.cleanup();
  });
});
