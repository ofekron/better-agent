import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/components/extensionModuleLoader", () => ({
  loadExtensionModule: vi.fn(),
}));

vi.mock("react-dom/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-dom/client")>();
  return { ...actual, createRoot: vi.fn(actual.createRoot) };
});

import { createRoot } from "react-dom/client";
import { ExtensionModuleSlot } from "../src/components/ExtensionSlots";
import type { ExtensionFrontendModule } from "../src/components/ExtensionSlots";
import { loadExtensionModule } from "../src/components/extensionModuleLoader";
import { eventBus } from "../src/lib/eventBus";

const createRootMock = vi.mocked(createRoot);
const loadMock = vi.mocked(loadExtensionModule);
const defaultCreateRoot = createRootMock.getMockImplementation();

const TEST_MODULE: ExtensionFrontendModule = {
  extension_id: "ext.test",
  extension_name: "Test",
  slot: "test-slot",
  id: "test-module",
  label: "Test",
  kind: "module",
  module_url: "/api/extensions/ext.test/frontend/ui/x.entry.js",
};

beforeEach(() => {
  createRootMock.mockClear();
  if (defaultCreateRoot) createRootMock.mockImplementation(defaultCreateRoot);
  loadMock.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("ExtensionModuleSlot context stability", () => {
  it("does not remount when context identity changes but values are equal", async () => {
    const mountFn = vi.fn(async () => () => {});
    loadMock.mockResolvedValue({ mount: mountFn });

    const { rerender } = render(
      <ExtensionModuleSlot module={TEST_MODULE} context={{ value: "x" }} />,
    );
    await vi.waitFor(() => expect(mountFn).toHaveBeenCalledTimes(1));

    rerender(<ExtensionModuleSlot module={TEST_MODULE} context={{ value: "x" }} />);
    rerender(<ExtensionModuleSlot module={TEST_MODULE} context={{ value: "x" }} />);

    expect(loadMock).toHaveBeenCalledTimes(1);
    expect(mountFn).toHaveBeenCalledTimes(1);
  });

  it("still remounts when module identity changes", async () => {
    const mountFn = vi.fn(async () => () => {});
    loadMock.mockResolvedValue({ mount: mountFn });

    const { rerender } = render(
      <ExtensionModuleSlot module={TEST_MODULE} context={{ value: "x" }} />,
    );
    await vi.waitFor(() => expect(mountFn).toHaveBeenCalledTimes(1));

    const otherModule: ExtensionFrontendModule = {
      ...TEST_MODULE,
      id: "other-module",
      module_url: "/api/extensions/ext.test/frontend/ui/y.entry.js",
    };
    rerender(<ExtensionModuleSlot module={otherModule} context={{ value: "x" }} />);
    await vi.waitFor(() => expect(mountFn).toHaveBeenCalledTimes(2));
    expect(loadMock).toHaveBeenCalledTimes(2);
  });

  it("uses the latest context at mount, not a stale snapshot from before the async load", async () => {
    const mountFn = vi.fn(async () => () => {});
    let resolveLoad!: (mod: unknown) => void;
    loadMock.mockReturnValue(
      new Promise((resolve) => {
        resolveLoad = resolve;
      }),
    );

    const { rerender } = render(
      <ExtensionModuleSlot module={TEST_MODULE} context={{ value: "stale" }} />,
    );
    rerender(<ExtensionModuleSlot module={TEST_MODULE} context={{ value: "fresh" }} />);

    resolveLoad({ mount: mountFn });
    await vi.waitFor(() => expect(mountFn).toHaveBeenCalledTimes(1));

    expect((mountFn.mock.calls[0][0] as { context: { value: string } }).context.value).toBe(
      "fresh",
    );
    expect(loadMock).toHaveBeenCalledTimes(1);
  });

  it("delivers changed context to a live Component module via re-render, not remount", async () => {
    const seen: string[] = [];
    const Comp = (props: { context: { value?: string } }) => {
      seen.push(props.context.value ?? "");
      return null;
    };
    loadMock.mockResolvedValue({ Component: Comp });

    const { rerender, unmount } = render(
      <ExtensionModuleSlot module={TEST_MODULE} context={{ value: "a" }} />,
    );
    await vi.waitFor(() => expect(seen).toContain("a"));

    rerender(<ExtensionModuleSlot module={TEST_MODULE} context={{ value: "b" }} />);
    await vi.waitFor(() => expect(seen).toContain("b"));

    expect(loadMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      unmount();
    });
  });

  it("passes a narrow realtime event subscription into Component modules", async () => {
    const seen: unknown[] = [];
    let subscribed = false;
    const Comp = (props: {
      context: { subscribeToEvent?: (type: string, handler: (payload: unknown) => void) => () => void };
    }) => {
      props.context.subscribeToEvent?.("assistant.board_updated", (payload) => {
        seen.push(payload);
      });
      props.context.subscribeToEvent?.("session.created", (payload) => {
        seen.push(payload);
      });
      props.context.subscribeToEvent?.("websocket.connected", (payload) => {
        seen.push(payload);
      });
      subscribed = true;
      return null;
    };
    loadMock.mockResolvedValue({ Component: Comp });

    const assistantModule = {
      ...TEST_MODULE,
      extension_id: "ofek-dev.assistant",
      module_url: "/api/extensions/ofek-dev.assistant/frontend/ui/assistant-view.entry.js",
    };
    const { unmount } = render(<ExtensionModuleSlot module={assistantModule} />);
    await vi.waitFor(() => expect(subscribed).toBe(true));

    eventBus.publish("session.created", { session_id: "ignored" });
    eventBus.publish("assistant.board_updated", { source_session_id: "s1" });
    eventBus.publish("websocket.connected", {});
    expect(seen).toEqual([{ source_session_id: "s1" }, {}]);
    unmount();
  });

  it("does not grant assistant board events to unrelated assistant-named extensions", async () => {
    const seen: unknown[] = [];
    let subscribed = false;
    const Comp = (props: {
      context: { subscribeToEvent?: (type: string, handler: (payload: unknown) => void) => () => void };
    }) => {
      props.context.subscribeToEvent?.("assistant.board_updated", (payload) => {
        seen.push(payload);
      });
      subscribed = true;
      return null;
    };
    loadMock.mockResolvedValue({ Component: Comp });

    const unrelatedModule = {
      ...TEST_MODULE,
      extension_id: "evil.assistant",
      module_url: "/api/extensions/evil.assistant/frontend/ui/x.entry.js",
    };
    const { unmount } = render(<ExtensionModuleSlot module={unrelatedModule} />);
    await vi.waitFor(() => expect(subscribed).toBe(true));

    eventBus.publish("assistant.board_updated", { source_session_id: "s1" });
    expect(seen).toEqual([]);
    unmount();
  });

  it("unmounts Component roots before their host slot is detached", async () => {
    const unmountConnectedStates: boolean[] = [];
    const replaceChildrenSpy = vi.spyOn(HTMLElement.prototype, "replaceChildren");
    createRootMock.mockImplementation((container, options) => {
      if (
        container instanceof HTMLElement &&
        container.classList.contains("extension-module-slot")
      ) {
        return {
          render: vi.fn(),
          unmount: vi.fn(() => {
            unmountConnectedStates.push(container.isConnected);
            if (!container.isConnected) {
              throw new DOMException(
                "Failed to execute 'removeChild' on 'Node': The node to be removed is not a child of this node.",
                "NotFoundError",
              );
            }
          }),
        } as unknown as ReturnType<typeof createRoot>;
      }
      if (!defaultCreateRoot) throw new Error("missing default createRoot");
      return defaultCreateRoot(container, options);
    });

    const Comp = () => null;
    loadMock.mockResolvedValue({ Component: Comp });

    try {
      const { unmount } = render(<ExtensionModuleSlot module={TEST_MODULE} />);
      await vi.waitFor(() => {
        expect(
          createRootMock.mock.calls.some(
            ([container]) =>
              container instanceof HTMLElement &&
              container.classList.contains("extension-module-slot"),
          ),
        ).toBe(true);
      });

      expect(() => unmount()).not.toThrow();
      expect(unmountConnectedStates).toEqual([true]);
      expect(replaceChildrenSpy).not.toHaveBeenCalled();
    } finally {
      replaceChildrenSpy.mockRestore();
    }
  });
});
