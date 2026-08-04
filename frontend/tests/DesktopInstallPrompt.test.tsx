import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import type { DesktopInstallOffer } from "../src/hooks/useDesktopInstallOffer";
import { DesktopInstallPrompt } from "../src/components/DesktopInstallPrompt";

// t() spy returns the key verbatim so rendered text is observable while the
// interpolation args (offer.label) stay inspectable.
const { tMock } = vi.hoisted(() => ({ tMock: vi.fn((key: string) => key) }));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: tMock }),
}));

const OFFER: DesktopInstallOffer = {
  platform: "macos",
  label: "macOS",
  url: "https://example.test/darwin/arm64",
  version: "1.2.3",
};

beforeEach(() => {
  tMock.mockClear();
  window.history.replaceState(null, "", "/");
});

describe("DesktopInstallPrompt", () => {
  it("renders title, body, and both action labels and passes platform through", () => {
    const { getByRole, getByText } = render(
      <DesktopInstallPrompt offer={OFFER} onDismiss={() => {}} />,
    );

    // role=status wraps the whole prompt.
    getByRole("status");

    // The title interpolates offer.label; the body is a plain key lookup.
    expect(tMock).toHaveBeenCalledWith("desktopInstall.title", {
      platform: "macOS",
    });
    expect(tMock).toHaveBeenCalledWith("desktopInstall.body");
    getByText("desktopInstall.title");
    getByText("desktopInstall.body");

    const later = getByText("desktopInstall.later").closest("button")!;
    const install = getByText("desktopInstall.install").closest("button")!;
    expect(later.className).toBe("desktop-install-secondary");
    expect(install.className).toBe("desktop-install-primary");
  });

  it("secondary button calls onDismiss without navigating", () => {
    const onDismiss = vi.fn();
    const { getByText } = render(
      <DesktopInstallPrompt offer={OFFER} onDismiss={onDismiss} />,
    );
    fireEvent.click(getByText("desktopInstall.later"));
    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(window.location.href).not.toContain("example.test");
  });

  it("primary button dismisses then navigates to offer.url", () => {
    const onDismiss = vi.fn();
    const { getByText } = render(
      <DesktopInstallPrompt offer={OFFER} onDismiss={onDismiss} />,
    );
    fireEvent.click(getByText("desktopInstall.install"));
    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(window.location.href).toBe(OFFER.url);
  });
});
