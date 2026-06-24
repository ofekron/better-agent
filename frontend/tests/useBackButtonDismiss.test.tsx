import { describe, it, expect, vi, beforeEach } from "vitest";
import React, { act, StrictMode, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { useBackButtonDismiss } from "../src/hooks/useBackButtonDismiss";

function Modal({
  open,
  onClose,
  label = "modal",
}: {
  open: boolean;
  onClose: () => void;
  label?: string;
}) {
  useBackButtonDismiss(open, onClose);
  return open ? <div data-testid={label}>{label}</div> : null;
}

async function mount(node: React.ReactNode) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  let root: Root | null = null;
  await act(async () => {
    root = createRoot(container);
    root.render(node);
  });
  return {
    container,
    rerender: async (next: React.ReactNode) => {
      await act(async () => root?.render(next));
    },
    unmount: async () => {
      await act(async () => root?.unmount());
      container.remove();
    },
  };
}

function fireBack() {
  // Simulating "user back press". happy-dom DOES fire popstate
  // synchronously on history.back(); a manual dispatchEvent would
  // double-fire and corrupt nested-modal sequencing tests.
  window.history.back();
}

beforeEach(() => {
  // Park each test at a known fresh "home" entry so the sentinel
  // pushes/pops are observable without poisoning the next test.
  window.history.replaceState(null, "", "/test-home");
});

describe("useBackButtonDismiss", () => {
  it("popstate closes the open modal", async () => {
    const onClose = vi.fn();
    const m = await mount(<Modal open={true} onClose={onClose} />);
    expect(window.history.state).toMatchObject({ __modalId: expect.any(Number) });
    fireBack();
    expect(onClose).toHaveBeenCalledTimes(1);
    await m.unmount();
  });

  it("programmatic close neutralizes our sentinel in place (no history.back)", async () => {
    const onClose = vi.fn();
    const initialLength = window.history.length;
    function Harness() {
      const [open, setOpen] = useState(true);
      return (
        <>
          <button data-testid="close" onClick={() => setOpen(false)}>x</button>
          <Modal open={open} onClose={() => { setOpen(false); onClose(); }} />
        </>
      );
    }
    const m = await mount(<Harness />);
    expect(window.history.state).toMatchObject({ __modalId: expect.any(Number) });
    const lenWithSentinel = window.history.length;
    expect(lenWithSentinel).toBe(initialLength + 1);

    // Programmatic close via the X button.
    await act(async () => {
      m.container.querySelector<HTMLButtonElement>('[data-testid="close"]')!.click();
    });
    // We replaceState the sentinel — entry stays, but state is wiped.
    expect(window.history.state).toBeNull();
    expect(window.history.length).toBe(lenWithSentinel);
    await m.unmount();
  });

  it("preserves prior history.state via __prev on push, restores on cleanup", async () => {
    window.history.replaceState({ foo: 42 }, "", "/test-home");
    function Harness() {
      const [open, setOpen] = useState(true);
      return (
        <>
          <button data-testid="close" onClick={() => setOpen(false)}>x</button>
          <Modal open={open} onClose={() => setOpen(false)} />
        </>
      );
    }
    const m = await mount(<Harness />);
    expect(window.history.state).toMatchObject({ __modalId: expect.any(Number), __prev: { foo: 42 } });
    await act(async () => {
      m.container.querySelector<HTMLButtonElement>('[data-testid="close"]')!.click();
    });
    expect(window.history.state).toEqual({ foo: 42 });
    await m.unmount();
  });

  it("nested modals: back closes innermost only", async () => {
    const closeA = vi.fn();
    const closeB = vi.fn();
    const m = await mount(
      <>
        <Modal open={true} onClose={closeA} label="A" />
        <Modal open={true} onClose={closeB} label="B" />
      </>,
    );
    fireBack();
    expect(closeB).toHaveBeenCalledTimes(1);
    expect(closeA).not.toHaveBeenCalled();
    await m.unmount();
  });

  it("StrictMode double-invoke does not flicker the modal closed on open", async () => {
    const onClose = vi.fn();
    const m = await mount(
      <StrictMode>
        <Modal open={true} onClose={onClose} />
      </StrictMode>,
    );
    // The whole point: StrictMode's mount→cleanup→mount must NOT
    // produce a popstate that fires onClose at mount time.
    expect(onClose).not.toHaveBeenCalled();
    expect(window.history.state).toMatchObject({ __modalId: expect.any(Number) });
    await m.unmount();
  });

  it("onClose identity change does not churn the listener", async () => {
    const m = await mount(<Modal open={true} onClose={() => {}} />);
    const stateAfterFirstMount = window.history.state;
    // Re-render with a fresh onClose identity — effect deps are [open]
    // so the effect must NOT re-run and push a second sentinel.
    await m.rerender(<Modal open={true} onClose={() => { /* identity-changed */ }} />);
    expect(window.history.state).toEqual(stateAfterFirstMount);
    await m.unmount();
  });
});
