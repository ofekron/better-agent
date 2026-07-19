import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { OpenRecoveryAppButton } from "../src/components/OpenRecoveryAppButton";
import { writeLineSwitchConnection } from "../src/lineSwitchClient";

vi.mock("@capacitor/core", () => ({
  Capacitor: { getPlatform: () => "web", isNativePlatform: () => false },
}));
vi.mock("@capacitor/browser", () => ({ Browser: { open: vi.fn() } }));

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("Open recovery app", () => {
  it("opens the paired BAS PWA without exposing the token to the request URL", async () => {
    const token = "x".repeat(43);
    writeLineSwitchConnection({ baseUrl: "https://switch.example.test", token });
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ version: 1, apps: [
        { id: "pwa", label: "Better Agent Switch", kind: "pwa", platforms: ["web"], url: "/" },
      ] }),
    } as Response);
    const open = vi.spyOn(window, "open").mockImplementation(() => null);

    render(<OpenRecoveryAppButton />);
    fireEvent.click(screen.getByRole("button", { name: "Open recovery app" }));

    await waitFor(() => expect(open).toHaveBeenCalledWith(
      `https://switch.example.test/#${token}`, "_blank", "noopener,noreferrer",
    ));
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "https://switch.example.test/api/apps",
      expect.objectContaining({ headers: expect.objectContaining({ Authorization: `Bearer ${token}` }) }),
    );
  });
});
