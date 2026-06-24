import { useEffect, useRef } from "react";

/** Shared module-scope stack of active modal ids. Only the top-of-stack
 *  hook responds to a popstate so a single back press closes the
 *  innermost modal — not every active hook at once. */
const STACK: number[] = [];
let MODAL_SEQ = 0;

/** Visible to the Capacitor back-button handler so it can tell whether a
 *  popstate closed a modal (skip further back-nav) vs. a stale entry
 *  (keep going). */
export const getModalStackSize = () => STACK.length;

/** Marker shape we push onto `history.state` for every open modal.
 *  `__prev` preserves whatever the route layer (or any other writer)
 *  had on the state slot so the cleanup `replaceState` round-trips it. */
export interface ModalHistoryState {
  __modalId: number;
  __prev: unknown;
}

/** Wire a modal to the browser/Android-hardware back button.
 *
 *  Lifecycle:
 *  - When `open` flips to `true` the hook pushes a sentinel onto the
 *    history stack (`pushState({ __modalId, __prev })`) and starts
 *    listening for `popstate`. A popstate that lands on our id
 *    invokes `onClose`.
 *  - When `open` flips to `false` via the parent (e.g. X button,
 *    Escape, backdrop click, programmatic dismiss) the cleanup neutralizes
 *    our sentinel in place via `replaceState(__prev ?? null, "")` — it
 *    does NOT call `history.back()`. Avoiding `back()` here kills two
 *    races: StrictMode double-invoke (the second mount would push a new
 *    sentinel before the queued `back()` lands, popping the wrong entry
 *    and instantly closing the modal) and sibling-unmount ordering
 *    (two co-unmounting modals would race over which one pops the top).
 *
 *  Accepted trade-off: each open/close cycle leaves one stale history
 *  entry behind (state replaced to `__prev`, but the entry itself stays
 *  in the back stack). After N modal cycles on the same page the user
 *  needs N+1 back presses to leave. Bounded and harmless — pathname is
 *  unchanged, no listener responds.
 *
 *  `onClose` is captured via ref so the effect deps stay `[open]` —
 *  parent re-renders with a fresh `onClose` identity don't churn the
 *  listener / push a duplicate sentinel.
 */
export function useBackButtonDismiss(open: boolean, onClose: () => void) {
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const myIdRef = useRef<number | null>(null);

  useEffect(() => {
    if (!open) return;
    // StrictMode-safe guard: if a prior effect on this same hook
    // instance is still mid-flight (no cleanup has run), don't
    // push a second sentinel.
    if (myIdRef.current !== null) return;

    const id = ++MODAL_SEQ;
    myIdRef.current = id;
    STACK.push(id);
    const prev = window.history.state;
    const sentinel: ModalHistoryState = { __modalId: id, __prev: prev };
    window.history.pushState(sentinel, "");

    let closedByPop = false;
    const onPop = () => {
      // Multiple hook instances all subscribe — only the top-of-stack
      // one owns this popstate. Everyone else bails.
      if (STACK[STACK.length - 1] !== id) return;
      STACK.pop();
      closedByPop = true;
      onCloseRef.current();
    };
    window.addEventListener("popstate", onPop);

    return () => {
      window.removeEventListener("popstate", onPop);
      myIdRef.current = null;
      if (closedByPop) return;
      const idx = STACK.indexOf(id);
      if (idx >= 0) STACK.splice(idx, 1);
      // Only neutralize if we are actually still on top — a higher
      // sibling sentinel sitting above us means our entry is no longer
      // the current one; touching it via replaceState would clobber
      // the sibling's state.
      const currentState = window.history.state as ModalHistoryState | null;
      if (currentState?.__modalId === id) {
        window.history.replaceState(currentState.__prev ?? null, "");
      }
    };
  }, [open]);
}

/** Called once at app boot — wipes a sentinel that survived a page
 *  reload (browsers restore `history.state` on reload, but no React
 *  component has re-claimed it yet, so the next back-press would be a
 *  silent no-op against a phantom listener). Restores the carried
 *  `__prev` for `__modalId` sentinels; clears `__cancelInFlight`
 *  absorbers pushed by async-onCancel working-mode overlays that may
 *  have leaked an entry if the overlay
 *  unmounted mid-cancel. */
export function cleanupRestoredModalSentinel() {
  const state = window.history.state as
    | (ModalHistoryState | { __cancelInFlight?: boolean })
    | null;
  if (!state || typeof state !== "object") return;
  if ("__modalId" in state) {
    window.history.replaceState(
      (state as ModalHistoryState).__prev ?? null,
      "",
      window.location.href,
    );
    return;
  }
  if ("__cancelInFlight" in state) {
    window.history.replaceState(null, "", window.location.href);
  }
}
