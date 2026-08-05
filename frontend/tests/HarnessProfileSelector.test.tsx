import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const trackedFetch = vi.hoisted(() => vi.fn());
const bus = vi.hoisted(() => ({ callback: null as null | (() => void) }));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock("../src/api", () => ({ API: "" }));

vi.mock("../src/lib/eventBus", () => ({
  eventBus: {
    subscribe: (_topic: string, callback: () => void) => {
      bus.callback = callback;
      return () => {
        bus.callback = null;
      };
    },
  },
}));

vi.mock("../src/progress/store", () => ({
  trackedFetch: (...args: unknown[]) => trackedFetch(...args),
}));

import { HarnessProfileSelector } from "../src/components/HarnessProfileSelector";

function response(profiles: Array<{ id: string; name: string }>): Response {
  return { json: async () => ({ profiles }) } as Response;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("HarnessProfileSelector", () => {
  beforeEach(() => {
    trackedFetch.mockReset();
    bus.callback = null;
  });

  it("reconciles a deleted saved profile to Default after a successful load", async () => {
    trackedFetch.mockResolvedValue(response([{ id: "default", name: "Default" }]));
    const onChange = vi.fn();

    render(<HarnessProfileSelector value="deleted-profile" onChange={onChange} />);

    await waitFor(() => expect(onChange).toHaveBeenCalledWith("default"));
    expect((screen.getByRole("combobox", { name: "harnessProfile.label" }) as HTMLSelectElement).value)
      .toBe("default");
  });

  it("preserves an available named profile", async () => {
    trackedFetch.mockResolvedValue(response([
      { id: "default", name: "Default" },
      { id: "saved-profile", name: "Saved" },
    ]));
    const onChange = vi.fn();

    render(<HarnessProfileSelector value="saved-profile" onChange={onChange} />);

    await waitFor(() => expect(trackedFetch).toHaveBeenCalledOnce());
    expect(onChange).not.toHaveBeenCalled();
    expect((screen.getByRole("combobox", { name: "harnessProfile.label" }) as HTMLSelectElement).value)
      .toBe("saved-profile");
  });

  it("preserves the saved profile when the live list cannot be loaded", async () => {
    trackedFetch.mockRejectedValue(new Error("offline"));
    const onChange = vi.fn();
    const onReadyChange = vi.fn();

    render(
      <HarnessProfileSelector
        value="saved-profile"
        onChange={onChange}
        onReadyChange={onReadyChange}
      />,
    );

    await waitFor(() => expect(screen.getByTitle("offline")).toBeTruthy());
    expect(onChange).not.toHaveBeenCalled();
    expect(onReadyChange.mock.calls[0]).toEqual([false]);
    expect(onReadyChange.mock.calls.at(-1)).toEqual([true]);
    expect((screen.getByRole("combobox", { name: "harnessProfile.label" }) as HTMLSelectElement).value)
      .toBe("saved-profile");
  });

  it("ignores an older response after a newer profile refresh completes", async () => {
    const initial = deferred<Response>();
    const refresh = deferred<Response>();
    trackedFetch
      .mockImplementationOnce(() => initial.promise)
      .mockImplementationOnce(() => refresh.promise);
    const onChange = vi.fn();

    render(<HarnessProfileSelector value="new-profile" onChange={onChange} />);
    await waitFor(() => expect(bus.callback).toBeTypeOf("function"));
    bus.callback?.();

    refresh.resolve(response([
      { id: "default", name: "Default" },
      { id: "new-profile", name: "New" },
    ]));
    await waitFor(() => {
      expect((screen.getByRole("combobox", { name: "harnessProfile.label" }) as HTMLSelectElement).value)
        .toBe("new-profile");
    });

    initial.resolve(response([{ id: "default", name: "Default" }]));
    await Promise.resolve();
    await Promise.resolve();

    expect(onChange).not.toHaveBeenCalled();
    expect((screen.getByRole("combobox", { name: "harnessProfile.label" }) as HTMLSelectElement).value)
      .toBe("new-profile");
  });
});
