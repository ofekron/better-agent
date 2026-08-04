// OpenRecoveryAppButton launches the desktop recovery companion: on web it
// opens the local recovery URL, on a Capacitor native platform it fires the
// companion deep link. It surfaces an inline error on failure and latches a
// busy state while the open is in flight. Icon, i18n, Capacitor, and
// openExternalLink have their own concerns; mock them so this suite isolates
// the button's own render + interaction logic.
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const { tMock, openExternalLinkMock, isNativeMock } = vi.hoisted(() => ({
  tMock: vi.fn((key: string) => key),
  openExternalLinkMock: vi.fn<(url: string) => Promise<void>>(async () => {}),
  isNativeMock: vi.fn<() => boolean>(() => false),
}));

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: tMock }) }));
vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: isNativeMock },
}));
vi.mock("../src/utils/externalLink", () => ({
  openExternalLink: openExternalLinkMock,
}));
vi.mock("../src/components/Icon", () => ({
  default: ({ name, size }: { name?: string; size?: number }) =>
    React.createElement("span", { "data-icon": name ?? "", "data-size": size ?? "" }),
}));

import { OpenRecoveryAppButton } from "../src/components/OpenRecoveryAppButton";

const LOCAL_RECOVERY_URL = "http://127.0.0.1:18768/";
const COMPANION_DEEP_LINK = "betteragentswitch://open";

function button(): HTMLButtonElement {
  return screen.getByRole("button") as HTMLButtonElement;
}

beforeEach(() => {
  tMock.mockClear();
  openExternalLinkMock.mockReset();
  isNativeMock.mockReset();
  isNativeMock.mockReturnValue(false);
});

afterEach(() => vi.useRealTimers());

describe("OpenRecoveryAppButton", () => {
  it("renders with the default className and opens the local recovery URL on web", async () => {
    openExternalLinkMock.mockResolvedValue();
    const { container } = render(<OpenRecoveryAppButton />);

    const btn = button();
    expect(btn.className).toBe("login-submit");
    expect(btn.getAttribute("disabled")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(container.querySelector(".open-recovery-app")).not.toBeNull();
    // Icon is rendered.
    expect(container.querySelector('[data-icon="target"]')).not.toBeNull();
    expect(tMock).toHaveBeenCalledWith("settings.openRecoveryApp");

    fireEvent.click(btn);
    await waitFor(() => expect(openExternalLinkMock).toHaveBeenCalledTimes(1));
    expect(openExternalLinkMock).toHaveBeenCalledWith(LOCAL_RECOVERY_URL);

    // Idle again after success: no error, not disabled, idle label re-rendered.
    await waitFor(() => expect(btn.getAttribute("disabled")).toBeNull());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("applies a custom className and fires the companion deep link on a native platform", async () => {
    isNativeMock.mockReturnValue(true);
    openExternalLinkMock.mockResolvedValue();

    render(<OpenRecoveryAppButton className="custom-btn" />);
    const btn = button();
    expect(btn.className).toBe("custom-btn");

    fireEvent.click(btn);
    await waitFor(() => expect(openExternalLinkMock).toHaveBeenCalledTimes(1));
    expect(openExternalLinkMock).toHaveBeenCalledWith(COMPANION_DEEP_LINK);
  });

  it("latches busy state and shows the loading label while the open is in flight", async () => {
    let resolveOpen: () => void = () => {};
    openExternalLinkMock.mockImplementation(
      () => new Promise<void>((resolve) => void (resolveOpen = resolve)),
    );

    render(<OpenRecoveryAppButton />);
    const btn = button();

    fireEvent.click(btn);
    // While openExternalLink is pending the button is disabled and the
    // loading label is shown; the call used the web (local) URL.
    await waitFor(() => expect(btn.getAttribute("disabled")).not.toBeNull());
    expect(tMock).toHaveBeenCalledWith("app.loading");
    expect(openExternalLinkMock).toHaveBeenCalledWith(LOCAL_RECOVERY_URL);

    // Resolving releases the busy latch back to idle.
    resolveOpen();
    await waitFor(() => expect(btn.getAttribute("disabled")).toBeNull());
    expect(tMock).toHaveBeenCalledWith("settings.openRecoveryApp");
  });

  it("shows the message when openExternalLink rejects with an Error", async () => {
    openExternalLinkMock.mockRejectedValue(new Error("boom"));
    render(<OpenRecoveryAppButton />);

    fireEvent.click(button());
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("boom");
    expect(alert.className).toBe("settings-error");
    // Busy latch released even on failure.
    await waitFor(() => expect(button().getAttribute("disabled")).toBeNull());
  });

  it("stringifies a non-Error rejection cause into the error alert", async () => {
    openExternalLinkMock.mockRejectedValue("plain string cause");
    render(<OpenRecoveryAppButton />);

    fireEvent.click(button());
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("plain string cause");
  });
});
