import { render } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ScreenWakeLock } from "../src/components/ScreenWakeLock";

const useScreenWakeLock = vi.fn();

vi.mock("../src/hooks/useScreenWakeLock", () => ({
  useScreenWakeLock: () => useScreenWakeLock(),
}));

describe("ScreenWakeLock", () => {
  it("mounts the hook and renders nothing", () => {
    const { container } = render(<ScreenWakeLock />);
    expect(useScreenWakeLock).toHaveBeenCalledTimes(1);
    expect(container.firstChild).toBeNull();
  });
});
