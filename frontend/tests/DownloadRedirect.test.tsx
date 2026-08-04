import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render } from "@testing-library/react";
import { DownloadRedirect } from "../src/components/DownloadRedirect";

// t() returns the key verbatim so rendered text/accessible names are observable.
const { tMock, serverUrlFromSearch, nativeConfigUrlForServer } = vi.hoisted(() => ({
  tMock: vi.fn((key: string) => key),
  serverUrlFromSearch: vi.fn<(s: string) => string | null>(() => null),
  nativeConfigUrlForServer: vi.fn<(url: string) => string>(
    (url) => `betteragent://configure?server=${encodeURIComponent(url)}`,
  ),
}));

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: tMock }) }));
vi.mock("../src/api", () => ({ API: "/test-api" }));
vi.mock("../src/mobileServerHandoff", () => ({
  serverUrlFromSearch,
  nativeConfigUrlForServer,
}));

beforeEach(() => {
  tMock.mockClear();
  serverUrlFromSearch.mockReset();
  serverUrlFromSearch.mockReturnValue(null);
  nativeConfigUrlForServer.mockReset();
  window.history.replaceState(null, "", "/");
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("DownloadRedirect", () => {
  it("renders the manual download link and schedules the authenticated download when no server URL is present", () => {
    serverUrlFromSearch.mockReturnValue(null);
    const { getByRole, queryByText } = render(<DownloadRedirect platform="android" />);

    const manual = getByRole("link", { name: "download.manual" });
    expect(manual.getAttribute("href")).toBe("/test-api/api/download/android");
    // No native-config anchor when there is no server URL.
    expect(queryByText("download.openApp")).toBeNull();

    // Effect 2 early-returns (no appUrl); the timed nav has not fired yet.
    expect(window.location.href).not.toContain("/test-api/api/download/android");

    vi.advanceTimersByTime(500);
    expect(window.location.href).toContain("/test-api/api/download/android");
  });

  it("navigates to the native app config and renders its anchor when a server URL is present", () => {
    const appUrl = "betteragent://configure?server=https%3A%2F%2Fsrv.example";
    serverUrlFromSearch.mockReturnValue("https://srv.example");
    nativeConfigUrlForServer.mockReturnValue(appUrl);
    const { getByRole } = render(<DownloadRedirect platform="ios" />);

    // Effect 2 runs on mount and navigates to the native config URL.
    expect(window.location.href).toBe(appUrl);

    const appLink = getByRole("link", { name: "download.openApp" });
    expect(appLink.getAttribute("href")).toBe(appUrl);

    const manual = getByRole("link", { name: "download.manual" });
    expect(manual.getAttribute("href")).toBe("/test-api/api/download/ios");
  });
});
