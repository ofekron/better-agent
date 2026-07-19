import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { OpenRecoveryAppButton } from "../src/components/OpenRecoveryAppButton";

vi.mock("@capacitor/core", () => ({
  Capacitor: { getPlatform: () => "web", isNativePlatform: () => false },
}));
vi.mock("@capacitor/browser", () => ({ Browser: { open: vi.fn() } }));

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("Open recovery app", () => {
  it("opens the fixed local companion without reading credentials or calling its API", async () => {
    localStorage.setItem("better_agent_line_switch", JSON.stringify({
      baseUrl: "https://attacker.example.test",
      token: "x".repeat(43),
    }));
    const fetch = vi.spyOn(globalThis, "fetch");
    const open = vi.spyOn(window, "open").mockImplementation(() => null);

    render(<OpenRecoveryAppButton />);
    fireEvent.click(screen.getByRole("button", { name: "Open recovery app" }));

    await waitFor(() => expect(open).toHaveBeenCalledWith(
      "http://127.0.0.1:18768/", "_blank", "noopener,noreferrer",
    ));
    expect(fetch).not.toHaveBeenCalled();
  });
});
