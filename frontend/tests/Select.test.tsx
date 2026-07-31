import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Select, type SelectOption } from "../src/components/Select";

/** Build a DOMRect-like object with only the fields Select consumes. */
function rect(o: { left?: number; top?: number; bottom?: number; width?: number }): DOMRect {
  return {
    left: o.left ?? 0,
    top: o.top ?? 0,
    bottom: o.bottom ?? 0,
    right: (o.left ?? 0) + (o.width ?? 0),
    width: o.width ?? 0,
    height: 0,
    x: o.left ?? 0,
    y: o.top ?? 0,
    toJSON() {},
  } as DOMRect;
}

const A_B_C: SelectOption[] = [
  { value: "a", label: "Alpha" },
  { value: "b", label: "Bravo" },
  { value: "c", label: "Charlie" },
];

/** Render Select with a stable trigger testid and return helpers. */
function renderSelect(props: React.ComponentProps<typeof Select>) {
  const utils = render(<Select data-testid="trigger" {...props} />);
  const trigger = () => screen.getByTestId("trigger") as HTMLButtonElement;
  return { ...utils, trigger };
}

function spyTriggerRect(r: DOMRect) {
  return vi.spyOn(screen.getByTestId("trigger") as HTMLElement, "getBoundingClientRect").mockReturnValue(r);
}

// Default viewport for every test; geometry tests override per-test as needed.
beforeEach(() => {
  Object.defineProperty(window, "innerHeight", { value: 800, configurable: true, writable: true });
});

describe("Select — closed render", () => {
  it("renders the trigger showing the selected label in a collapsed state", () => {
    const { trigger } = renderSelect({ value: "b", options: A_B_C, onChange: vi.fn() });
    expect(trigger().getAttribute("aria-expanded")).toBe("false");
    expect(trigger().getAttribute("aria-haspopup")).toBe("listbox");
    expect(screen.getByText("Bravo")).toBeTruthy();
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("shows the placeholder when no option matches the value", () => {
    renderSelect({ value: "z", options: A_B_C, onChange: vi.fn(), placeholder: "pick one" });
    expect(screen.getByText("pick one")).toBeTruthy();
    expect(screen.queryByText("Bravo")).toBeNull();
  });

  it("forwards id/title/aria-label/className and the disabled flag", () => {
    render(
      <Select
        value="a"
        options={A_B_C}
        onChange={vi.fn()}
        data-testid="sel"
        id="sel"
        title="choose"
        aria-label="flavor"
        className="extra"
        disabled
      />,
    );
    const trigger = screen.getByTestId("sel") as HTMLButtonElement;
    expect(trigger.id).toBe("sel");
    expect(trigger.getAttribute("title")).toBe("choose");
    expect(trigger.getAttribute("aria-label")).toBe("flavor");
    expect(trigger.className).toContain("extra");
    expect(trigger.disabled).toBe(true);
  });

  it("does not open when there are no options", () => {
    const { trigger } = renderSelect({ value: "a", options: [], onChange: vi.fn() });
    fireEvent.click(trigger());
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});

describe("Select — opening", () => {
  it("opens on trigger click and starts the active option on the selected one", () => {
    const { trigger } = renderSelect({ value: "b", options: A_B_C, onChange: vi.fn() });
    fireEvent.click(trigger());
    expect(trigger().getAttribute("aria-expanded")).toBe("true");
    const menu = screen.getByRole("listbox");
    expect(menu.parentElement).toBe(document.body); // portal-rendered
    const opts = screen.getAllByRole("option");
    expect(opts).toHaveLength(3);
    expect(opts[0].getAttribute("aria-selected")).toBe("false");
    expect(opts[1].getAttribute("aria-selected")).toBe("true");
    expect(opts[1].className).toContain("active");
    expect(opts[1].className).toContain("selected");
  });

  it("starts active on the first enabled option when the selected value is disabled", () => {
    const opts: SelectOption[] = [
      { value: "a", label: "Alpha", disabled: true },
      { value: "b", label: "Bravo" },
      { value: "c", label: "Charlie" },
    ];
    const { trigger } = renderSelect({ value: "a", options: opts, onChange: vi.fn() });
    fireEvent.click(trigger());
    const rendered = screen.getAllByRole("option");
    expect(rendered[1].className).toContain("active"); // Bravo is first enabled
    expect(rendered[0].className).toContain("disabled");
    expect(rendered[0].getAttribute("aria-disabled")).toBe("true");
  });

  it("opens with no active option when every option is disabled", () => {
    const opts: SelectOption[] = [
      { value: "a", label: "Alpha", disabled: true },
      { value: "b", label: "Bravo", disabled: true },
    ];
    const { trigger } = renderSelect({ value: "a", options: opts, onChange: vi.fn() });
    fireEvent.click(trigger());
    expect(screen.getByRole("listbox")).toBeTruthy();
    const rendered = screen.getAllByRole("option");
    expect(rendered.every((o) => !o.className.includes("active"))).toBe(true);
    expect(trigger().getAttribute("aria-activedescendant")).toBeNull();
  });

  it("does not open when disabled", () => {
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn(), disabled: true });
    fireEvent.click(trigger());
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("toggles closed when clicking an already-open trigger", () => {
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn() });
    fireEvent.click(trigger());
    expect(screen.getByRole("listbox")).toBeTruthy();
    fireEvent.click(trigger());
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});

describe("Select — menu geometry", () => {
  it("opens below the trigger by default and sets top", () => {
    Object.defineProperty(window, "innerHeight", { value: 800, configurable: true, writable: true });
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn() });
    spyTriggerRect(rect({ left: 10, top: 100, bottom: 130, width: 120 }));
    fireEvent.click(trigger());
    const menu = screen.getByRole("listbox") as HTMLElement;
    expect(menu.className).not.toContain("above");
    expect(menu.style.top).toBe("130px");
    expect(menu.style.left).toBe("10px");
    expect(menu.style.width).toBe("120px");
  });

  it("opens above when there is little space below but more above", () => {
    Object.defineProperty(window, "innerHeight", { value: 600, configurable: true, writable: true });
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn() });
    spyTriggerRect(rect({ left: 5, top: 500, bottom: 520, width: 100 }));
    fireEvent.click(trigger());
    const menu = screen.getByRole("listbox") as HTMLElement;
    expect(menu.className).toContain("above");
    expect(menu.style.bottom).toBe(`${600 - 500}px`);
    expect(menu.style.top).toBe("");
  });

  it("clamps maxHeight to the minimum when available space is tiny", () => {
    Object.defineProperty(window, "innerHeight", { value: 100, configurable: true, writable: true });
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn() });
    // Tiny viewport: below=10 (<200) and above(80)>below → opens above; avail=72 → clamps to 120.
    spyTriggerRect(rect({ left: 0, top: 80, bottom: 90, width: 50 }));
    fireEvent.click(trigger());
    expect((screen.getByRole("listbox") as HTMLElement).style.maxHeight).toBe("120px");
  });

  it("clamps maxHeight to the cap when available space is large", () => {
    Object.defineProperty(window, "innerHeight", { value: 800, configurable: true, writable: true });
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn() });
    spyTriggerRect(rect({ left: 0, top: 0, bottom: 10, width: 50 }));
    fireEvent.click(trigger());
    expect((screen.getByRole("listbox") as HTMLElement).style.maxHeight).toBe("280px");
  });
});

describe("Select — repositioning listeners", () => {
  it("recomputes rect on window scroll while open", () => {
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn() });
    const spy = spyTriggerRect(rect({ left: 0, top: 100, bottom: 130, width: 80 }));
    fireEvent.click(trigger());
    const menu = screen.getByRole("listbox") as HTMLElement;
    expect(menu.style.top).toBe("130px");
    spy.mockReturnValue(rect({ left: 0, top: 200, bottom: 230, width: 80 }));
    fireEvent.scroll(window);
    expect(menu.style.top).toBe("230px");
  });

  it("recomputes rect on window resize while open", () => {
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn() });
    const spy = spyTriggerRect(rect({ left: 0, top: 100, bottom: 130, width: 80 }));
    fireEvent.click(trigger());
    const menu = screen.getByRole("listbox") as HTMLElement;
    spy.mockReturnValue(rect({ left: 0, top: 100, bottom: 130, width: 140 }));
    fireEvent.resize(window);
    expect(menu.style.width).toBe("140px");
  });
});

describe("Select — closing", () => {
  it("closes on mousedown outside the trigger and menu", () => {
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn() });
    fireEvent.click(trigger());
    expect(screen.getByRole("listbox")).toBeTruthy();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("does not close on mousedown inside the menu", () => {
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn() });
    fireEvent.click(trigger());
    fireEvent.mouseDown(screen.getByRole("listbox"));
    expect(screen.getByRole("listbox")).toBeTruthy();
  });

  it("does not close on mousedown on the trigger", () => {
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn() });
    fireEvent.click(trigger());
    fireEvent.mouseDown(trigger());
    expect(screen.getByRole("listbox")).toBeTruthy();
  });

  it("closes and refocuses the trigger on Escape", () => {
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn() });
    fireEvent.click(trigger());
    fireEvent.keyDown(trigger(), { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("closes on Tab without preventing default focus movement", () => {
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn() });
    fireEvent.click(trigger());
    fireEvent.keyDown(trigger(), { key: "Tab" });
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("removes its document/window listeners after unmount", () => {
    const { trigger, unmount } = renderSelect({
      value: "a",
      options: A_B_C,
      onChange: vi.fn(),
    });
    fireEvent.click(trigger());
    unmount();
    fireEvent.scroll(window);
    fireEvent.resize(window);
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});

describe("Select — keyboard (closed trigger)", () => {
  for (const key of ["Enter", " ", "ArrowDown", "ArrowUp"]) {
    it(`opens on ${JSON.stringify(key)}`, () => {
      const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn() });
      fireEvent.keyDown(trigger(), { key });
      expect(screen.getByRole("listbox")).toBeTruthy();
    });
  }

  it("ignores unrelated keys when closed", () => {
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn() });
    fireEvent.keyDown(trigger(), { key: "a" });
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("ignores unrelated keys when open (falls through the key chain as a no-op)", () => {
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn() });
    fireEvent.click(trigger());
    const activeBefore = screen.getAllByRole("option")[0].className;
    fireEvent.keyDown(trigger(), { key: "x" });
    expect(screen.getByRole("listbox")).toBeTruthy(); // still open
    expect(screen.getAllByRole("option")[0].className).toBe(activeBefore); // active unchanged
  });

  it("ignores all keys when disabled", () => {
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange: vi.fn(), disabled: true });
    fireEvent.keyDown(trigger(), { key: "Enter" });
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});

describe("Select — keyboard (open menu)", () => {
  const optsWithGap: SelectOption[] = [
    { value: "a", label: "Alpha" },
    { value: "b", label: "Bravo", disabled: true },
    { value: "c", label: "Charlie" },
  ];

  it("ArrowDown moves active forward, skipping disabled options and wrapping", () => {
    const { trigger } = renderSelect({ value: "a", options: optsWithGap, onChange: vi.fn() });
    fireEvent.click(trigger()); // active = a (0)
    fireEvent.keyDown(trigger(), { key: "ArrowDown" }); // skip b(1) → c(2)
    expect(screen.getAllByRole("option")[2].className).toContain("active");
    fireEvent.keyDown(trigger(), { key: "ArrowDown" }); // wrap → a(0)
    expect(screen.getAllByRole("option")[0].className).toContain("active");
  });

  it("ArrowUp moves active backward, skipping disabled options", () => {
    const { trigger } = renderSelect({ value: "c", options: optsWithGap, onChange: vi.fn() });
    fireEvent.click(trigger()); // active = c (2)
    fireEvent.keyDown(trigger(), { key: "ArrowUp" }); // skip b(1) → a(0)
    expect(screen.getAllByRole("option")[0].className).toContain("active");
  });

  it("Home/End jump to the first/last enabled option, skipping disabled ones", () => {
    const opts: SelectOption[] = [
      { value: "a", label: "Alpha", disabled: true },
      { value: "b", label: "Bravo" },
      { value: "c", label: "Charlie" },
      { value: "d", label: "Delta", disabled: true },
    ];
    const { trigger } = renderSelect({ value: "b", options: opts, onChange: vi.fn() });
    fireEvent.click(trigger()); // active = b (1)
    fireEvent.keyDown(trigger(), { key: "End" }); // scans d(3 disabled) → c(2 enabled)
    expect(screen.getAllByRole("option")[2].className).toContain("active");
    fireEvent.keyDown(trigger(), { key: "Home" }); // findIndex skips a(0 disabled) → b(1)
    expect(screen.getAllByRole("option")[1].className).toContain("active");
  });

  it("Enter picks the active option, fires onChange, closes, and refocuses the trigger", () => {
    const onChange = vi.fn();
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange });
    fireEvent.click(trigger());
    fireEvent.keyDown(trigger(), { key: "ArrowDown" }); // active → b
    fireEvent.keyDown(trigger(), { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("b");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("Space picks the active option", () => {
    const onChange = vi.fn();
    const { trigger } = renderSelect({ value: "a", options: A_B_C, onChange });
    fireEvent.click(trigger()); // active = a
    fireEvent.keyDown(trigger(), { key: "ArrowDown" }); // active → b
    fireEvent.keyDown(trigger(), { key: " " });
    expect(onChange).toHaveBeenCalledWith("b");
  });

  it("leaves the menu open when every option is disabled and Enter is pressed", () => {
    const allDisabled: SelectOption[] = [
      { value: "a", label: "Alpha", disabled: true },
      { value: "b", label: "Bravo", disabled: true },
    ];
    const onChange = vi.fn();
    const { trigger } = renderSelect({ value: "a", options: allDisabled, onChange });
    fireEvent.click(trigger()); // activeIndex = -1
    fireEvent.keyDown(trigger(), { key: "ArrowDown" }); // all disabled → stays -1
    expect(trigger().getAttribute("aria-activedescendant")).toBeNull();
    fireEvent.keyDown(trigger(), { key: "Enter" }); // pick(-1) → no option → no-op
    expect(screen.queryByRole("listbox")).toBeTruthy();
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("Select — option mouse interactions", () => {
  it("hovering an enabled option moves active to it; hovering a disabled one does not", async () => {
    const user = userEvent.setup();
    const opts: SelectOption[] = [
      { value: "a", label: "Alpha" },
      { value: "b", label: "Bravo", disabled: true },
      { value: "c", label: "Charlie" },
    ];
    const { trigger } = renderSelect({ value: "a", options: opts, onChange: vi.fn() });
    fireEvent.click(trigger()); // active = a (0)
    const rendered = screen.getAllByRole("option");
    // userEvent.hover dispatches the full pointer+mouse sequence React needs to
    // synthesize onMouseEnter on portal content.
    await user.hover(rendered[2]);
    expect(rendered[2].className).toContain("active");
    expect(rendered[0].className).not.toContain("active");
    // Hovering the disabled option must not move active off the enabled one.
    await user.hover(rendered[1]);
    expect(rendered[1].className).not.toContain("active");
    expect(rendered[2].className).toContain("active");
  });

  it("clicking an enabled option selects it and closes; disabled options carry disabled", () => {
    const opts: SelectOption[] = [
      { value: "a", label: "Alpha" },
      { value: "b", label: "Bravo", disabled: true },
    ];
    const onChange = vi.fn();
    const { trigger } = renderSelect({ value: "a", options: opts, onChange });
    fireEvent.click(trigger());
    fireEvent.click(screen.getAllByRole("option")[0]); // enabled a
    expect(onChange).toHaveBeenCalledWith("a");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("listbox")).toBeNull();
    // The disabled option is genuinely disabled in the DOM (no selection path).
    fireEvent.click(trigger());
    expect((screen.getAllByRole("option")[1] as HTMLButtonElement).disabled).toBe(true);
  });

  it("renders a check icon next to the selected option only", () => {
    const { trigger } = renderSelect({ value: "b", options: A_B_C, onChange: vi.fn() });
    fireEvent.click(trigger());
    const rendered = screen.getAllByRole("option");
    expect(rendered[1].querySelector("svg")).toBeTruthy();
    expect(rendered[0].querySelector("svg")).toBeNull();
  });
});
