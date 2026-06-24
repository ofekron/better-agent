import { describe, it, expect } from "vitest";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { useRoute, sessionPath, type Route } from "../src/hooks/useRoute";

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
