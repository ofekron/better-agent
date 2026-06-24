import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAnimatedTabMovement } from "src/hooks/useAnimatedTabMovement";

function AnimatedTabs({ keys }: { keys: string[] }) {
  const ref = useAnimatedTabMovement<HTMLDivElement>(keys);

  return (
    <div ref={ref}>
      {keys.map((key) => (
        <div key={key} data-tab-movement-key={key}>
          {key}
        </div>
      ))}
    </div>
  );
}

describe("useAnimatedTabMovement", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("animates tabs that move between renders", () => {
    const animate = vi.fn();
    const positions = new Map<string, number>([
      ["a", 0],
      ["b", 100],
      ["c", 200],
    ]);

    vi.spyOn(window, "matchMedia").mockReturnValue({
      matches: false,
    } as MediaQueryList);
    Element.prototype.animate = animate;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function () {
      const key = this.dataset.tabMovementKey ?? "";
      return {
        left: positions.get(key) ?? 0,
        top: 0,
        right: (positions.get(key) ?? 0) + 80,
        bottom: 30,
        width: 80,
        height: 30,
        x: positions.get(key) ?? 0,
        y: 0,
        toJSON: () => ({}),
      };
    });

    const { rerender } = render(<AnimatedTabs keys={["a", "b", "c"]} />);

    positions.set("a", 100);
    positions.set("b", 0);
    rerender(<AnimatedTabs keys={["b", "a", "c"]} />);

    expect(animate).toHaveBeenCalledTimes(2);
    expect(animate).toHaveBeenCalledWith(
      [
        { transform: "translate(100px, 0px)" },
        { transform: "translate(0, 0)" },
      ],
      expect.objectContaining({ duration: 180 }),
    );
    expect(animate).toHaveBeenCalledWith(
      [
        { transform: "translate(-100px, 0px)" },
        { transform: "translate(0, 0)" },
      ],
      expect.objectContaining({ duration: 180 }),
    );
  });

  it("does not animate when reduced motion is requested", () => {
    const animate = vi.fn();
    const positions = new Map<string, number>([
      ["a", 0],
      ["b", 100],
    ]);

    vi.spyOn(window, "matchMedia").mockReturnValue({
      matches: true,
    } as MediaQueryList);
    Element.prototype.animate = animate;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function () {
      const key = this.dataset.tabMovementKey ?? "";
      return {
        left: positions.get(key) ?? 0,
        top: 0,
        right: (positions.get(key) ?? 0) + 80,
        bottom: 30,
        width: 80,
        height: 30,
        x: positions.get(key) ?? 0,
        y: 0,
        toJSON: () => ({}),
      };
    });

    const { rerender } = render(<AnimatedTabs keys={["a", "b"]} />);

    positions.set("a", 100);
    positions.set("b", 0);
    rerender(<AnimatedTabs keys={["b", "a"]} />);

    expect(animate).not.toHaveBeenCalled();
  });
});
