import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Login } from "../src/components/Login";

const mocks = vi.hoisted(() => ({
  setTokens: vi.fn(),
}));

vi.mock("../src/bearerAuth", () => ({
  setStoredToken: vi.fn(),
  setTokens: mocks.setTokens,
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
  }),
}));

beforeEach(() => {
  mocks.setTokens.mockClear();
  window.history.replaceState(null, "", "/s/team-session#qr=grant-fragment");
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    if (String(url).endsWith("/api/auth/qr_redeem")) {
      return new Response(JSON.stringify({
        access_token: "access-token",
        refresh_token: "refresh-token",
      }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (String(url).endsWith("/api/auth/qr_grant")) {
      throw new Error("qr_grant must not run while redeeming a fragment grant");
    }
    throw new Error(`unexpected fetch ${String(url)} ${init?.method ?? "GET"}`);
  }) as unknown as typeof fetch;
});

describe("Login QR fragment handoff", () => {
  it("redeems #qr without sending the grant in the HTTP URL", async () => {
    const onSuccess = vi.fn();

    render(<Login onSuccess={onSuccess} />);

    await waitFor(() => {
      expect(mocks.setTokens).toHaveBeenCalledWith("access-token", "refresh-token");
      expect(onSuccess).toHaveBeenCalledTimes(1);
    });
    const calls = vi.mocked(globalThis.fetch).mock.calls;
    expect(String(calls[0][0])).toBe("/api/auth/qr_redeem");
    expect(JSON.parse(String(calls[0][1]?.body))).toEqual({ grant: "grant-fragment" });
    expect(window.location.hash).toBe("");
  });
});
