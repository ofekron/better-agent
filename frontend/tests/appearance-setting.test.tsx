import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import {
  AppearanceSetting,
  applyAppearancePrefs,
} from "../src/components/AppearanceSetting";

function response(data: unknown): Response {
  return {
    ok: true,
    json: async () => data,
  } as Response;
}

describe("AppearanceSetting", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    document.documentElement.removeAttribute("data-theme");
    document.documentElement.style.removeProperty("--app-font-scale");
    document.documentElement.style.removeProperty("--app-font-size");
  });

  it("applies valid themes and normalizes unknown themes", () => {
    applyAppearancePrefs({ appearance_theme: "nord" });
    expect(document.documentElement.dataset.theme).toBe("nord");

    applyAppearancePrefs({ appearance_theme: "unknown" as "nord" });
    expect(document.documentElement.dataset.theme).toBe("default");
  });

  it("persists a selected theme through the existing preference endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) => {
      if (init?.method === "PATCH") return Promise.resolve(response({}));
      return Promise.resolve(response({
        font_family: "system",
        font_size: 14,
        appearance_theme: "default",
      }));
    });

    render(<AppearanceSetting />);
    const nord = await screen.findByRole("radio", { name: "Nord" });
    fireEvent.click(nord);

    await waitFor(() => {
      expect(document.documentElement.dataset.theme).toBe("nord");
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/user-prefs",
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({
            font_family: "system",
            font_size: 14,
            appearance_theme: "nord",
          }),
        }),
      );
    });
  });

  it("scales typography proportionally from both size controls", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) => {
      if (init?.method === "PATCH") return Promise.resolve(response({}));
      return Promise.resolve(response({
        font_family: "system",
        font_size: 14,
        appearance_theme: "default",
      }));
    });

    render(<AppearanceSetting />);
    const slider = await screen.findByRole("slider", { name: "Font size slider" });
    fireEvent.change(slider, { target: { value: "18" } });

    await waitFor(() => {
      expect(document.documentElement.style.getPropertyValue("--app-font-size")).toBe("18px");
      expect(Number(document.documentElement.style.getPropertyValue("--app-font-scale")))
        .toBeCloseTo(18 / 14);
    });
    expect(
      (screen.getByRole("spinbutton", { name: "Font size" }) as HTMLInputElement).value,
    ).toBe("18");
  });
});
