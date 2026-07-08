import { render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import "../src/i18n";
import { Login } from "../src/components/Login";
import {
  clearStoredToken,
  getStoredToken,
  installBearerAuthInterceptor,
  setStoredToken,
} from "../src/bearerAuth";
import { getWsUrl } from "../src/api";
import { rawFileUrl } from "../src/utils/rawFileUrl";

const realTop = window.top;

function simulateIframe() {
  Object.defineProperty(window, "top", { configurable: true, value: {} });
}

afterEach(() => {
  Object.defineProperty(window, "top", { configurable: true, value: realTop });
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  clearStoredToken();
  window.history.replaceState(null, "", "/");
});

beforeEach(() => {
  clearStoredToken();
});

describe("bearer auth in cross-site embeds (session cookie can't travel)", () => {
  it("getWsUrl carries ?token= inside an iframe when a bearer token is stored", () => {
    setStoredToken("tok-1");
    expect(getWsUrl()).not.toContain("token=");
    simulateIframe();
    expect(getWsUrl()).toContain("token=tok-1");
  });

  it("rawFileUrl carries ?token= inside an iframe when a bearer token is stored", () => {
    setStoredToken("tok-2");
    expect(rawFileUrl("", "/a.txt", "node-1")).not.toContain("token=");
    simulateIframe();
    expect(rawFileUrl("", "/a.txt", "node-1")).toContain("token=tok-2");
  });

  it("redeems #qr= and subsequent fetches carry the bearer token via the interceptor", async () => {
    simulateIframe();
    window.history.replaceState(null, "", "/s/some-session#qr=one-time-grant");

    const calls: Array<{ url: string; auth: string | null }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const headers = new Headers(init?.headers || {});
        calls.push({ url, auth: headers.get("authorization") });
        if (url.includes("/api/auth/qr_redeem")) {
          return new Response(
            JSON.stringify({
              access_token: "access-1",
              refresh_token: "refresh-1",
              expires_in: 900,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({ username: "u" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    // main.tsx installs this unconditionally (web AND native) before the
    // app mounts; mirror that here on top of the stubbed fetch.
    installBearerAuthInterceptor();

    const onSuccess = vi.fn();
    const { unmount } = render(<Login onSuccess={onSuccess} />);
    try {
      await waitFor(() => expect(onSuccess).toHaveBeenCalled());
      expect(getStoredToken()).toBe("access-1");

      // The parent's auth re-check (cookie-less in an iframe) must now
      // authenticate via the injected bearer header.
      await window.fetch("/api/auth/me", { credentials: "include" });
      const last = calls[calls.length - 1];
      expect(last.url).toContain("/api/auth/me");
      expect(last.auth).toBe("Bearer access-1");
    } finally {
      unmount();
    }
  });
});
