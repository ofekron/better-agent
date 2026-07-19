import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { BasCompanionAppsSetting } from "../src/components/BasCompanionAppsSetting";
import { writeLineSwitchConnection } from "../src/lineSwitchClient";

vi.mock("@capacitor/core", () => ({
  Capacitor: {
    getPlatform: () => "web",
    isNativePlatform: () => false,
  },
}));

vi.mock("@capacitor/browser", () => ({ Browser: { open: vi.fn() } }));

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("BAS companion apps setting", () => {
  it("shows only catalog apps available for this platform", async () => {
    writeLineSwitchConnection({ baseUrl: "https://switch.example.test", token: "x".repeat(43) });
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({
        version: 1,
        apps: [
          { id: "pwa", label: "Better Agent Switch", kind: "pwa", platforms: ["android", "ios", "macos", "windows", "web"], url: "/" },
          { id: "android", label: "Android app", kind: "native", platforms: ["android"], url: "https://downloads.example/android" },
        ],
      }),
    } as Response);
    const open = vi.spyOn(window, "open").mockImplementation(() => null);

    render(<BasCompanionAppsSetting />);

    expect(await screen.findByText("Better Agent Switch")).toBeTruthy();
    expect(screen.queryByText("Android app")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open install page" }));
    expect(open).toHaveBeenCalledWith(
      `https://switch.example.test/#${"x".repeat(43)}`,
      "_blank",
      "noopener,noreferrer",
    );
  });

  it("pairs directly with BAS before loading installers", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ version: 1, apps: [] }),
    } as Response);
    render(<BasCompanionAppsSetting />);

    fireEvent.change(screen.getByLabelText("Server IP"), {
      target: { value: `https://switch.example.test/#${"x".repeat(43)}` },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
    expect(localStorage.getItem("better_agent_line_switch")).toContain("switch.example.test");
  });
});
