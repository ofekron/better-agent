import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { BasCompanionAppsSetting } from "../src/components/BasCompanionAppsSetting";

vi.mock("@capacitor/core", () => ({
  Capacitor: { getPlatform: () => "web", isNativePlatform: () => false },
}));
vi.mock("@capacitor/browser", () => ({ Browser: { open: vi.fn() } }));

describe("BAS recovery setting", () => {
  it("shows only the recovery app launcher", () => {
    render(<BasCompanionAppsSetting />);
    expect(screen.getByRole("button", { name: "Open recovery app" })).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByText("Connect")).toBeNull();
  });
});
