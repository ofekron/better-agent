import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";

// --- hoisted mocks -----------------------------------------------------------

const showSheet = vi.hoisted(() => vi.fn());
const isMobileViewport = vi.hoisted(() => vi.fn(() => false));
const isTouchInteractionViewport = vi.hoisted(() => vi.fn(() => false));
const copyToClipboard = vi.hoisted(() => vi.fn(async () => true));
const mobileHandlers = vi.hoisted(() => ({
  rewind: vi.fn(),
  addTag: vi.fn(),
}));

vi.mock("../src/components/MobileActionSheet", () => ({
  useMobileActionSheet: () => ({ show: showSheet }),
  isMobileViewport,
  isTouchInteractionViewport,
}));

vi.mock("../src/contexts/MobileHandlersContext", () => ({
  getMobileHandlers: () => mobileHandlers,
}));

vi.mock("../src/hooks/useBackButtonDismiss", () => ({
  useBackButtonDismiss: () => {},
}));

vi.mock("../src/utils/clipboard", () => ({ copyToClipboard }));

vi.mock("../src/components/Icon", () => ({
  default: ({ name }: { name: string }) => <span>{name}</span>,
}));

import { InvestigateContextMenu } from "../src/components/InvestigateContextMenu";
import type { InvestigationData } from "../src/components/InvestigateContextMenu";

// --- helpers -----------------------------------------------------------------

function renderMenu(children?: ReactNode, onInvestigate = vi.fn()) {
  return render(
    <InvestigateContextMenu
      onInvestigate={onInvestigate}
      activeSessionId="sess-1"
      activeSessionCwd="/proj"
    >
      {children ?? <div data-testid="blank">blank</div>}
    </InvestigateContextMenu>,
  );
}

/** fireEvent wraps dispatch in act; contextmenu on a target inside the owner. */
function ctxMenu(target: Element) {
  fireEvent.contextMenu(target, { clientX: 100, clientY: 200 });
}

function labels() {
  return screen.getAllByRole("button").map((b) => b.textContent ?? "");
}
function findBtn(substr: string) {
  return screen.getAllByRole("button").find((b) => (b.textContent ?? "").includes(substr))!;
}

/** Select the text content of `el` so buildActions sees a live selection. */
function selectText(el: Element) {
  const node = el.firstChild as ChildNode;
  const range = document.createRange();
  range.selectNodeContents(node);
  const sel = window.getSelection()!;
  sel.removeAllRanges();
  sel.addRange(range);
}

function resetMocks() {
  showSheet.mockReset();
  copyToClipboard.mockReset();
  copyToClipboard.mockResolvedValue(true);
  mobileHandlers.rewind.mockReset();
  mobileHandlers.addTag.mockReset();
  isMobileViewport.mockReturnValue(false);
  isTouchInteractionViewport.mockReturnValue(false);
}

beforeEach(() => resetMocks());
afterEach(() => {
  document.body.innerHTML = "";
  window.getSelection()?.removeAllRanges();
});

// --- desktop context-menu build ----------------------------------------------

describe("InvestigateContextMenu — desktop menu build", () => {
  it("shows an Investigate-only menu on a plain target", () => {
    renderMenu();
    ctxMenu(screen.getByTestId("blank"));
    const labs = labels();
    expect(labs.length).toBe(1);
    expect(labs[0]).toMatch(/Investigate/);
  });

  it("adds a Copy-id action on a message target", () => {
    renderMenu(
      <div data-message-id="msg-9" data-testid="msg">
        body
      </div>,
    );
    ctxMenu(screen.getByTestId("msg"));
    expect(labels().some((l) => l.includes("session.copyAction"))).toBe(true);
    expect(labels().some((l) => l.includes("Investigate"))).toBe(true);
  });

  it("adds a Rewind action on a user message when handlers.rewind is set", () => {
    mobileHandlers.rewind = vi.fn();
    renderMenu(
      <div className="user-message-box" data-message-id="msg-1" data-testid="umsg">
        hi
      </div>,
    );
    ctxMenu(screen.getByTestId("umsg"));
    expect(labels().some((l) => l.includes("Rewind"))).toBe(true);
    expect(labels().some((l) => l.includes("session.copyAction"))).toBe(true);
    expect(labels().some((l) => l.includes("Investigate"))).toBe(true);
  });

  it("does not open a menu on form elements", () => {
    renderMenu(
      <input data-testid="inp" />,
    );
    ctxMenu(screen.getByTestId("inp"));
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("does not open a menu inside a modal overlay", () => {
    renderMenu(
      <div className="modal-overlay" data-testid="modal">
        <div data-testid="inner">x</div>
      </div>,
    );
    ctxMenu(screen.getByTestId("inner"));
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("does not open a menu on mobile viewport", () => {
    isMobileViewport.mockReturnValue(true);
    renderMenu();
    ctxMenu(screen.getByTestId("blank"));
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("adds Copy and Comment actions when text is selected inside a message", () => {
    mobileHandlers.addTag = vi.fn();
    renderMenu(
      <div data-message-id="msg-s" data-testid="smsg">
        hello selected world
      </div>,
    );
    selectText(screen.getByTestId("smsg"));
    ctxMenu(screen.getByTestId("smsg"));
    expect(labels().some((l) => l.endsWith("Copy"))).toBe(true);
    expect(labels().some((l) => l.endsWith("Comment"))).toBe(true);
  });

  it("adds Copy but not Comment when selection anchor is outside a message", () => {
    mobileHandlers.addTag = vi.fn();
    renderMenu(<p data-testid="para">some selected text here</p>);
    selectText(screen.getByTestId("para"));
    ctxMenu(screen.getByTestId("para"));
    expect(labels().some((l) => l.endsWith("Copy"))).toBe(true);
    expect(labels().some((l) => l.endsWith("Comment"))).toBe(false);
  });
});

// --- menu positioning --------------------------------------------------------

describe("InvestigateContextMenu — positioning & clamp", () => {
  it("clamps the menu within the viewport", () => {
    renderMenu();
    // click near the bottom-right corner
    fireEvent.contextMenu(screen.getByTestId("blank"), {
      clientX: window.innerWidth - 5,
      clientY: window.innerHeight - 5,
    });
    const menu = document.querySelector(".ctx-menu") as HTMLElement;
    expect(menu).toBeTruthy();
    const top = parseInt(menu.style.top, 10);
    const left = parseInt(menu.style.left, 10);
    expect(top).toBeGreaterThanOrEqual(8);
    expect(left).toBeLessThanOrEqual(window.innerWidth - 200 - 8);
  });
});

// --- menu interactions -------------------------------------------------------

describe("InvestigateContextMenu — menu interactions", () => {
  it("clicking Copy-id closes the menu and copies the ids", async () => {
    renderMenu(
      <div data-message-id="msg-c" data-testid="cmsg">
        body
      </div>,
    );
    ctxMenu(screen.getByTestId("cmsg"));
    const copyBtn = findBtn("session.copyAction");
    await act(async () => {
      fireEvent.click(copyBtn);
    });
    expect(copyToClipboard).toHaveBeenCalledWith("msg-c sess-1 /proj");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("Copy-id omits session/cwd when they are absent", async () => {
    render(
      <InvestigateContextMenu onInvestigate={vi.fn()}>
        <div data-message-id="solo" data-testid="solo">
          body
        </div>
      </InvestigateContextMenu>,
    );
    ctxMenu(screen.getByTestId("solo"));
    const copyBtn = findBtn("session.copyAction");
    await act(async () => {
      fireEvent.click(copyBtn);
    });
    expect(copyToClipboard).toHaveBeenCalledWith("solo");
  });

  it("Rewind action invokes handlers.rewind with the message id and coords", async () => {
    const rewind = vi.fn();
    mobileHandlers.rewind = rewind;
    renderMenu(
      <div className="user-message-box" data-message-id="msg-r" data-testid="rmsg">
        hi
      </div>,
    );
    ctxMenu(screen.getByTestId("rmsg"));
    const rewindBtn = findBtn("Rewind");
    await act(async () => {
      fireEvent.click(rewindBtn);
    });
    expect(rewind).toHaveBeenCalledWith("msg-r", { x: 100, y: 200 });
  });

  it("clicking the selection Copy action copies the selected text", async () => {
    renderMenu(<p data-testid="para">some selected text here</p>);
    selectText(screen.getByTestId("para"));
    ctxMenu(screen.getByTestId("para"));
    const copyBtn = findBtn("Copy");
    await act(async () => {
      fireEvent.click(copyBtn);
    });
    expect(copyToClipboard).toHaveBeenCalledWith("some selected text here");
  });

  it("clicking the Comment action tags the selected text on its message", async () => {
    const addTag = vi.fn();
    mobileHandlers.addTag = addTag;
    renderMenu(
      <div data-message-id="msg-cm" data-testid="cm">
        the selected text
      </div>,
    );
    selectText(screen.getByTestId("cm"));
    ctxMenu(screen.getByTestId("cm"));
    const commentBtn = findBtn("Comment");
    await act(async () => {
      fireEvent.click(commentBtn);
    });
    expect(addTag).toHaveBeenCalledWith("the selected text", "", "msg-cm");
  });

  it("Investigate action surfaces an error when screenshot capture is unavailable", async () => {
    const onInvestigate = vi.fn();
    renderMenu(<div data-testid="inv">x</div>, onInvestigate);
    ctxMenu(screen.getByTestId("inv"));
    const invBtn = findBtn("Investigate");
    await act(async () => {
      fireEvent.click(invBtn);
    });
    // captureAnnotatedScreenshot throws (no mediaDevices) → onInvestigate never called
    expect(onInvestigate).not.toHaveBeenCalled();
  });

  it("Escape closes an open menu", async () => {
    renderMenu(<div data-testid="x">x</div>);
    ctxMenu(screen.getByTestId("x"));
    expect(screen.getAllByRole("button").length).toBeGreaterThan(0);
    // the document close/keydown listeners attach after a setTimeout(0)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 5));
    });
    await act(async () => {
      fireEvent.keyDown(document, { key: "Escape" });
    });
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("a non-Escape key does not close the menu", async () => {
    renderMenu(<div data-testid="x">x</div>);
    ctxMenu(screen.getByTestId("x"));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 5));
    });
    await act(async () => {
      fireEvent.keyDown(document, { key: "a" });
    });
    expect(screen.getAllByRole("button").length).toBeGreaterThan(0);
  });

  it("an outside click closes the menu", async () => {
    renderMenu(<div data-testid="x">x</div>);
    ctxMenu(screen.getByTestId("x"));
    expect(screen.getAllByRole("button").length).toBeGreaterThan(0);
    // let the setTimeout(0) attach the document click listener
    await act(async () => {
      await new Promise((r) => setTimeout(r, 5));
    });
    await act(async () => {
      fireEvent.click(document.body);
    });
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("clicking inside the menu does not propagate to the document closer", async () => {
    renderMenu(
      <div data-message-id="msg-p" data-testid="pmsg">
        body
      </div>,
    );
    ctxMenu(screen.getByTestId("pmsg"));
    const menu = document.querySelector(".ctx-menu") as HTMLElement;
    // the menu container's onClick calls stopPropagation; a click on the
    // container itself must not bubble to the document "close" listener.
    const stop = vi.fn();
    await act(async () => {
      const ev = new MouseEvent("click", { bubbles: true });
      Object.defineProperty(ev, "stopPropagation", { value: stop });
      menu.dispatchEvent(ev);
    });
    expect(stop).toHaveBeenCalled();
  });
});

// --- onInvestigate prompt shape (success path is env-blocked; assert prompt builder via copy) ---

describe("InvestigateContextMenu — investigate with mocked mediaDevices", () => {
  it("builds the prompt and calls onInvestigate when capture resolves a data url", async () => {
    // Inject a fake mediaDevices whose getDisplayMedia resolves a stream with
    // a video track. The component still needs onloadeddata + a 2d canvas
    // context to finish; happy-dom provides neither, so we drive the video
    // `loadeddata` event manually and stub canvas.getContext.
    const stopTrack = vi.fn();
    const videoEl = document.createElement("video");
    const realCreate = document.createElement.bind(document);
    const createElementSpy = vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
      if (tag === "video") return videoEl;
      if (tag === "canvas") {
        const c = realCreate("canvas");
        // no-op 2d context so the drawing calls execute without throwing
        const ctx = new Proxy(
          {},
          { get: () => () => undefined },
        ) as unknown as CanvasRenderingContext2D;
        c.getContext = () => ctx;
        c.toDataURL = () => "data:image/png;base64,AAAA";
        return c;
      }
      return realCreate(tag);
    });

    Object.defineProperty(navigator, "mediaDevices", {
      value: {
        getDisplayMedia: vi.fn().mockResolvedValue({
          getVideoTracks: () => [{ stop: stopTrack }],
          getTracks: () => [{ stop: stopTrack }],
        }),
      },
      configurable: true,
    });

    // make the video "load" synchronously; happy-dom's srcObject setter throws
    // on non-MediaStream input, so neutralize it.
    Object.defineProperty(videoEl, "srcObject", { set() {}, get: () => null, configurable: true });
    Object.defineProperty(videoEl, "play", { value: () => Promise.resolve(), configurable: true });
    Object.defineProperty(videoEl, "videoWidth", { value: 100, configurable: true });
    Object.defineProperty(videoEl, "videoHeight", { value: 100, configurable: true });

    const onInvestigate = vi.fn();
    // happy-dom ships no requestAnimationFrame; the capture flow awaits one rAF.
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => setTimeout(() => cb(0), 0));
    renderMenu(<div data-testid="inv2">x</div>, onInvestigate);
    ctxMenu(screen.getByTestId("inv2"));
    const invBtn = findBtn("Investigate");

    await act(async () => {
      fireEvent.click(invBtn);
    });
    // fire loadeddata so the capture promise resolves, then flush the rAF
    // setTimeout + downstream microtasks that complete the capture chain.
    await act(async () => {
      videoEl.dispatchEvent(new Event("loadeddata"));
      await new Promise((r) => setTimeout(r, 20));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(onInvestigate).toHaveBeenCalledTimes(1);
    const data: InvestigationData = onInvestigate.mock.calls[0][0];
    expect(data.images[0].base64).toBe("AAAA");
    expect(data.images[0].mediaType).toBe("image/png");
    expect(data.prompt).toContain("sess-1");
    expect(data.prompt).toContain("(100, 200)");
    expect(stopTrack).toHaveBeenCalled();

    createElementSpy.mockRestore();
    vi.unstubAllGlobals();
  });
});

// --- mobile touch long-press -------------------------------------------------

function makeTouch(target: Element, x = 50, y = 50, id = 1) {
  const touch = new Touch({ identifier: id, target, clientX: x, clientY: y });
  return touch;
}

function dispatchTouch(type: string, target: Element, touches: Touch[], changed: Touch[] = touches) {
  const ev = new TouchEvent(type, { touches, changedTouches: changed, bubbles: true });
  act(() => {
    target.dispatchEvent(ev);
  });
}

describe("InvestigateContextMenu — mobile long-press", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    isMobileViewport.mockReturnValue(true);
    isTouchInteractionViewport.mockReturnValue(true);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("fires the action sheet after a long press on a valid target", async () => {
    renderMenu(
      <div data-message-id="msg-t" data-testid="tmsg">
        hi
      </div>,
    );
    const target = screen.getByTestId("tmsg");
    dispatchTouch("touchstart", target, [makeTouch(target)]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).toHaveBeenCalledTimes(1);
    const items = showSheet.mock.calls[0][0];
    expect(items.length).toBeGreaterThan(0);
  });

  it("cancels when a second finger lands", async () => {
    renderMenu(<div data-testid="t">x</div>);
    const target = screen.getByTestId("t");
    dispatchTouch("touchstart", target, [
      makeTouch(target, 0, 0, 1),
      makeTouch(target, 5, 5, 2),
    ]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("cancels when the viewport is no longer touch", async () => {
    renderMenu(<div data-testid="t">x</div>);
    const target = screen.getByTestId("t");
    dispatchTouch("touchstart", target, [makeTouch(target)]);
    isTouchInteractionViewport.mockReturnValue(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("cancels on a form element target", async () => {
    renderMenu(<input data-testid="inp" />);
    const target = screen.getByTestId("inp");
    dispatchTouch("touchstart", target, [makeTouch(target)]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("cancels when the finger moves beyond the threshold", async () => {
    renderMenu(<div data-testid="t">x</div>);
    const target = screen.getByTestId("t");
    dispatchTouch("touchstart", target, [makeTouch(target, 0, 0, 1)]);
    dispatchTouch("touchmove", target, [makeTouch(target, 50, 50, 1)]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("cancels on touchend before the timer fires", async () => {
    renderMenu(<div data-testid="t">x</div>);
    const target = screen.getByTestId("t");
    const touch = makeTouch(target, 0, 0, 7);
    dispatchTouch("touchstart", target, [touch]);
    dispatchTouch("touchend", target, [], [touch]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("cancels on touchend with no changed touches", async () => {
    renderMenu(<div data-testid="t">x</div>);
    const target = screen.getByTestId("t");
    dispatchTouch("touchstart", target, [makeTouch(target, 0, 0, 1)]);
    dispatchTouch("touchend", target, [], []);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("ignores touchend when no long-press gesture is in progress", async () => {
    renderMenu(<div data-testid="t">x</div>);
    dispatchTouch("touchend", screen.getByTestId("t"), [
      makeTouch(screen.getByTestId("t"), 0, 0, 1),
    ]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("keeps the gesture when touchend is a different finger, then fires the sheet", async () => {
    renderMenu(
      <div data-message-id="m-k" data-testid="k">
        x
      </div>,
    );
    const target = screen.getByTestId("k");
    dispatchTouch("touchstart", target, [makeTouch(target, 0, 0, 1)]);
    dispatchTouch("touchend", target, [], [makeTouch(target, 0, 0, 99)]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).toHaveBeenCalledTimes(1);
  });

  it("does not cancel when the finger moves within the threshold, then fires", async () => {
    renderMenu(
      <div data-message-id="m-th" data-testid="th">
        x
      </div>,
    );
    const target = screen.getByTestId("th");
    dispatchTouch("touchstart", target, [makeTouch(target, 0, 0, 1)]);
    dispatchTouch("touchmove", target, [makeTouch(target, 3, 4, 1)]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).toHaveBeenCalledTimes(1);
  });

  it("cancels when touchmove sees a second finger", async () => {
    renderMenu(<div data-testid="t">x</div>);
    const target = screen.getByTestId("t");
    dispatchTouch("touchstart", target, [makeTouch(target, 0, 0, 1)]);
    dispatchTouch("touchmove", target, [
      makeTouch(target, 0, 0, 1),
      makeTouch(target, 5, 5, 2),
    ]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("cancels when touchmove loses the tracking finger", async () => {
    renderMenu(<div data-testid="t">x</div>);
    const target = screen.getByTestId("t");
    dispatchTouch("touchstart", target, [makeTouch(target, 0, 0, 1)]);
    dispatchTouch("touchmove", target, [makeTouch(target, 0, 0, 99)]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("cancels at touchstart when the viewport is not touch-capable", async () => {
    renderMenu(<div data-testid="t">x</div>);
    const target = screen.getByTestId("t");
    isTouchInteractionViewport.mockReturnValue(false);
    dispatchTouch("touchstart", target, [makeTouch(target)]);
    isTouchInteractionViewport.mockReturnValue(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("does not suppress the native context menu inside a modal on mobile", () => {
    renderMenu(
      <div className="modal-overlay" data-testid="modal">
        <div data-testid="inner">x</div>
      </div>,
    );
    const target = screen.getByTestId("inner");
    const ev = new Event("contextmenu", { bubbles: true, cancelable: true });
    const preventDefault = vi.spyOn(ev, "preventDefault");
    act(() => {
      target.dispatchEvent(ev);
    });
    expect(preventDefault).not.toHaveBeenCalled();
  });

  it("cancels the long-press if the active session changes mid-press", async () => {
    const onInvestigate = vi.fn();
    const { rerender } = render(
      <InvestigateContextMenu onInvestigate={onInvestigate} activeSessionId="s1" activeSessionCwd="/p">
        <div data-message-id="m-d" data-testid="d">
          x
        </div>
      </InvestigateContextMenu>,
    );
    const target = screen.getByTestId("d");
    dispatchTouch("touchstart", target, [makeTouch(target)]);
    rerender(
      <InvestigateContextMenu onInvestigate={onInvestigate} activeSessionId="s2" activeSessionCwd="/p">
        <div data-message-id="m-d" data-testid="d">
          x
        </div>
      </InvestigateContextMenu>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("cancels the long-press if the target is removed mid-press", async () => {
    const onInvestigate = vi.fn();
    render(
      <InvestigateContextMenu onInvestigate={onInvestigate} activeSessionId="s1" activeSessionCwd="/p">
        <div data-message-id="m-r" data-testid="r">
          x
        </div>
      </InvestigateContextMenu>,
    );
    const target = screen.getByTestId("r");
    dispatchTouch("touchstart", target, [makeTouch(target)]);
    act(() => target.remove());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("cancels the long-press if the message className drifts mid-press", async () => {
    const onInvestigate = vi.fn();
    render(
      <InvestigateContextMenu onInvestigate={onInvestigate} activeSessionId="s1" activeSessionCwd="/p">
        <div data-message-id="m-cn" data-testid="cn" className="was">
          x
        </div>
      </InvestigateContextMenu>,
    );
    const target = screen.getByTestId("cn");
    dispatchTouch("touchstart", target, [makeTouch(target)]);
    act(() => target.classList.add("changed"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("does not fire the sheet when long-pressing a message inside a modal overlay", async () => {
    renderMenu(
      <div className="modal-overlay" data-testid="modal">
        <div data-message-id="m-mo" data-testid="inner">
          x
        </div>
      </div>,
    );
    const target = screen.getByTestId("inner");
    dispatchTouch("touchstart", target, [makeTouch(target)]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("fires the sheet when long-pressing an image inside a message", async () => {
    renderMenu(
      <div data-message-id="m-img" data-testid="wrap">
        <img data-testid="img" alt="" />
      </div>,
    );
    const target = screen.getByTestId("img");
    dispatchTouch("touchstart", target, [makeTouch(target)]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).toHaveBeenCalled();
  });

  it("does not long-press an element marked data-mobile-context-owner", async () => {
    renderMenu(<div data-mobile-context-owner data-testid="owner" />);
    const target = screen.getByTestId("owner");
    dispatchTouch("touchstart", target, [makeTouch(target)]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("does not long-press a contentEditable element (native text selection)", async () => {
    renderMenu(
      <div data-testid="ce" contentEditable suppressContentEditableWarning>
        x
      </div>,
    );
    const target = screen.getByTestId("ce");
    dispatchTouch("touchstart", target, [makeTouch(target)]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("ignores a touchmove with no gesture in progress", async () => {
    renderMenu(<div data-testid="t">x</div>);
    dispatchTouch("touchmove", screen.getByTestId("t"), [makeTouch(screen.getByTestId("t"))]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("suppresses the native context menu on a mobile long-press target", () => {
    renderMenu(<div data-testid="t">x</div>);
    const target = screen.getByTestId("t");
    const ev = new Event("contextmenu", { bubbles: true, cancelable: true });
    const preventDefault = vi.spyOn(ev, "preventDefault");
    act(() => {
      target.dispatchEvent(ev);
    });
    expect(preventDefault).toHaveBeenCalled();
  });

  it("does not suppress the native context menu for a form target on mobile", () => {
    renderMenu(<input data-testid="inp" />);
    const target = screen.getByTestId("inp");
    const ev = new Event("contextmenu", { bubbles: true, cancelable: true });
    const preventDefault = vi.spyOn(ev, "preventDefault");
    act(() => {
      target.dispatchEvent(ev);
    });
    expect(preventDefault).not.toHaveBeenCalled();
  });
});
