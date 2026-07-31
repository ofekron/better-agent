import { describe, beforeEach, afterEach, it, expect, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

// Hoisted mock state. vi.hoisted lifts these above the vi.mock factories so
// the factories (which run before imports) can reference the live objects,
// and tests can flip `native` / set per-call return values.
const state = vi.hoisted(() => ({ native: false }));

const sendIntent = vi.hoisted(() => ({
  checkSendIntentReceived: vi.fn(),
  finish: vi.fn(),
}));
const filesystem = vi.hoisted(() => ({ readFile: vi.fn() }));
const capApp = vi.hoisted(() => ({ addListener: vi.fn() }));
const fileToPastedImage = vi.hoisted(() => vi.fn());

vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => state.native },
}));
vi.mock("send-intent", () => ({ SendIntent: sendIntent }));
vi.mock("@capacitor/filesystem", () => ({ Filesystem: filesystem }));
vi.mock("@capacitor/app", () => ({ App: capApp }));
vi.mock("../src/utils/imageAttach", () => ({ fileToPastedImage }));

import { useShareTarget } from "../src/hooks/useShareTarget";
import type { PastedImage } from "../src/types";

// Capture the listener callbacks CapApp.addListener received so we can
// re-deliver appUrlOpen / resume events.
function listenerFor(event: string): (() => void) | undefined {
  const call = capApp.addListener.mock.calls.find((c) => c[0] === event);
  return call?.[1] as (() => void) | undefined;
}

function img(id: string): PastedImage {
  return { id, src: `blob:${id}`, name: `${id}.png`, type: "image/png", size: 4 } as PastedImage;
}

describe("useShareTarget", () => {
  const onImages = vi.fn();

  beforeEach(() => {
    state.native = false;
    vi.clearAllMocks();
    onImages.mockClear();
    // addListener resolves to a handle whose remove() is a spy; tests read it.
    capApp.addListener.mockImplementation((event: string, cb: () => void) =>
      Promise.resolve({ remove: vi.fn() }),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // --- web no-op gate ---

  it("is a no-op on web (never registers listeners or reads intents)", async () => {
    const { unmount } = renderHook(() => useShareTarget(onImages));
    await Promise.resolve();

    expect(capApp.addListener).not.toHaveBeenCalled();
    expect(sendIntent.checkSendIntentReceived).not.toHaveBeenCalled();
    expect(onImages).not.toHaveBeenCalled();
    unmount();
  });

  // --- base64ToBlob + extractItems via the native cold-start path ---

  it("reads a single shared image on cold start and hands it to onImages", async () => {
    state.native = true;
    sendIntent.checkSendIntentReceived.mockResolvedValue({
      url: "file%3A%2F%2F%2Ftmp%2Fshot.png",
      type: "image/png",
    });
    filesystem.readFile.mockResolvedValue({ data: "AAAA" });
    fileToPastedImage.mockResolvedValue(img("a"));

    renderHook(() => useShareTarget(onImages));

    await waitFor(() => expect(onImages).toHaveBeenCalledWith([img("a")]));
    // The shared URL is URI-decoded before being handed to Filesystem.
    expect(filesystem.readFile).toHaveBeenCalledWith({ path: "file:///tmp/shot.png" });
    // The intent is released so it isn't re-delivered.
    expect(sendIntent.finish).toHaveBeenCalledTimes(1);
    // base64ToBlob honored the image/* MIME type on the Blob.
    const blob = fileToPastedImage.mock.calls[0][0] as Blob;
    expect(blob).toBeInstanceOf(Blob);
    expect(blob.type).toBe("image/png");
  });

  it("gathers multiple items and defaults non-image types to image/png", async () => {
    state.native = true;
    sendIntent.checkSendIntentReceived.mockResolvedValue({
      url: "u1",
      type: "image/jpeg",
      additionalItems: [
        { url: "u2", type: undefined },
        { url: "u3", type: "text/plain" },
        { url: "" }, // no url → skipped by extractItems
      ],
    });
    filesystem.readFile.mockImplementation(({ path }: { path: string }) =>
      Promise.resolve({ data: "QQ==" }),
    );
    fileToPastedImage.mockImplementation(async (blob: Blob) => img(blob.type));

    renderHook(() => useShareTarget(onImages));

    await waitFor(() => expect(onImages).toHaveBeenCalledTimes(1));
    const passed = onImages.mock.calls[0][0] as PastedImage[];
    expect(passed.map((p) => p.id)).toEqual(["image/jpeg", "image/png", "image/png"]);
    expect(filesystem.readFile).toHaveBeenCalledTimes(3);
    expect(sendIntent.finish).toHaveBeenCalledTimes(1);
  });

  it("skips processing when the intent carries no file urls", async () => {
    state.native = true;
    sendIntent.checkSendIntentReceived.mockResolvedValue({ additionalItems: [{ url: "" }] });

    renderHook(() => useShareTarget(onImages));
    await waitFor(() => expect(sendIntent.finish).not.toHaveBeenCalled());

    expect(filesystem.readFile).not.toHaveBeenCalled();
    expect(onImages).not.toHaveBeenCalled();
  });

  it("returns early when checkSendIntentReceived rejects (no pending intent)", async () => {
    state.native = true;
    sendIntent.checkSendIntentReceived.mockRejectedValue(new Error("none"));

    renderHook(() => useShareTarget(onImages));
    await waitFor(() => expect(sendIntent.checkSendIntentReceived).toHaveBeenCalled());

    expect(filesystem.readFile).not.toHaveBeenCalled();
    expect(sendIntent.finish).not.toHaveBeenCalled();
    expect(onImages).not.toHaveBeenCalled();
  });

  // --- listener re-delivery ---

  it("re-processes on appUrlOpen and resume events", async () => {
    state.native = true;
    sendIntent.checkSendIntentReceived
      .mockResolvedValueOnce({ url: "u1", type: "image/png" })
      .mockResolvedValueOnce({ url: "u2", type: "image/png" })
      .mockResolvedValueOnce({ url: "u3", type: "image/png" });
    filesystem.readFile.mockResolvedValue({ data: "AA==" });
    fileToPastedImage.mockResolvedValue(img("x"));

    renderHook(() => useShareTarget(onImages));
    await waitFor(() => expect(onImages).toHaveBeenCalledTimes(1));

    const open = listenerFor("appUrlOpen");
    const resume = listenerFor("resume");
    expect(open).toBeTruthy();
    expect(resume).toBeTruthy();

    await act(async () => {
      open!();
    });
    await waitFor(() => expect(onImages).toHaveBeenCalledTimes(2));

    await act(async () => {
      resume!();
    });
    await waitFor(() => expect(onImages).toHaveBeenCalledTimes(3));
    expect(sendIntent.finish).toHaveBeenCalledTimes(3);
  });

  // --- cancelled guard + cleanup ---

  it("does not call onImages when unmounted before the read resolves", async () => {
    state.native = true;
    sendIntent.checkSendIntentReceived.mockResolvedValue({ url: "u1", type: "image/png" });
    let releaseRead!: () => void;
    filesystem.readFile.mockReturnValue(
      new Promise<{ data: string }>((resolve) => (releaseRead = () => resolve({ data: "AA==" }))),
    );
    fileToPastedImage.mockResolvedValue(img("late"));

    const { unmount } = renderHook(() => useShareTarget(onImages));
    await waitFor(() => expect(filesystem.readFile).toHaveBeenCalled());
    // Unmount flips `cancelled`; resolving the read afterwards must NOT deliver.
    unmount();
    await act(async () => {
      releaseRead();
      await Promise.resolve();
    });

    expect(onImages).not.toHaveBeenCalled();
  });

  it("removes every CapApp listener on unmount", async () => {
    state.native = true;
    sendIntent.checkSendIntentReceived.mockResolvedValue({});
    const handles = { appUrlOpen: { remove: vi.fn() }, resume: { remove: vi.fn() } };
    capApp.addListener.mockImplementation((event: "appUrlOpen" | "resume") =>
      Promise.resolve(handles[event]),
    );

    const { unmount } = renderHook(() => useShareTarget(onImages));
    // addListener returns a promise; let both .then handlers populate removers.
    await waitFor(() => expect(capApp.addListener).toHaveBeenCalledTimes(2));
    await act(async () => {
      await Promise.resolve();
    });
    unmount();
    await act(async () => {
      await Promise.resolve();
    });

    expect(handles.appUrlOpen.remove).toHaveBeenCalledTimes(1);
    expect(handles.resume.remove).toHaveBeenCalledTimes(1);
  });
});
