import { afterEach, describe, expect, it, vi } from "vitest";

const installBearerAuthInterceptor = vi.fn();
const createRoot = vi.fn(() => ({ render: vi.fn() }));
const loadBuiltinExtensionIds = vi.fn(() => Promise.resolve());

vi.mock("../src/bearerAuth", () => ({
  installBearerAuthInterceptor,
}));

vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => false },
}));

vi.mock("@capacitor/app", () => ({
  App: { addListener: vi.fn() },
}));

vi.mock("react-dom/client", () => ({
  createRoot,
}));

vi.mock("../src/components/ErrorBoundary", () => ({
  ErrorBoundary: ({ children }: { children: unknown }) => children,
}));

vi.mock("../src/hooks/useBackButtonDismiss", () => ({
  cleanupRestoredModalSentinel: vi.fn(),
  getModalStackSize: vi.fn(() => 0),
}));

vi.mock("../src/lib/hardRefresh", () => ({
  clearHardRefreshMarker: vi.fn(),
}));

vi.mock("../src/lib/frontendLogger", () => ({
  installFrontendLogger: vi.fn(),
  logFailure: vi.fn(),
  logTiming: vi.fn(),
}));

vi.mock("../src/lib/mobileUpdater", () => ({
  runMobileOtaCheck: vi.fn(),
}));

vi.mock("../src/components/ScreenWakeLock", () => ({
  ScreenWakeLock: () => null,
}));

vi.mock("../src/extensionIds", () => ({
  loadBuiltinExtensionIds,
}));

vi.mock("../src/i18n", () => ({}));
vi.mock("../src/styles/globals.css", () => ({}));
vi.mock("../src/App", () => ({
  default: () => null,
}));

afterEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  document.body.innerHTML = "";
});

describe("main auth bootstrap", () => {
  it("installs bearer auth in web browsers so QR login tokens authenticate fetches", async () => {
    document.body.innerHTML = '<div id="root"></div>';

    await import("../src/main");

    expect(installBearerAuthInterceptor).toHaveBeenCalledTimes(1);
  });
});
