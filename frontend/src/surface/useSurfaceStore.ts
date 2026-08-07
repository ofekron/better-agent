// React binding for state.ts's SurfaceStore — owns one store instance per
// mounted sessionId (created/disposed alongside the subscription, exactly
// like adapter/useSurfaceSession.ts's effect does for its own per-session
// closure state) and exposes it via useSyncExternalStore so every surface/
// component re-renders on the same tearing-safe subscription React uses
// for every other external store in this app.

import { useEffect, useState, useSyncExternalStore } from "react";
import { SurfaceStore, type SurfaceStoreSnapshot } from "./state";

export interface UseSurfaceStoreResult {
  store: SurfaceStore | null;
  snapshot: SurfaceStoreSnapshot | null;
}

export function useSurfaceStore(sessionId: string | null): UseSurfaceStoreResult {
  const [store, setStore] = useState<SurfaceStore | null>(null);

  useEffect(() => {
    // Creating/disposing the store IS the side effect (opens a WebSocket,
    // issues the initial REST fetch) — there is no render-time value to
    // derive `store` from instead, so synchronizing the exposed instance
    // to `sessionId` necessarily happens here, not during render.
    if (!sessionId) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- external resource lifecycle sync, not a derivable render value
      setStore(null);
      return;
    }
    const next = new SurfaceStore(sessionId);
    setStore(next);
    return () => next.dispose();
  }, [sessionId]);

  const snapshot = useSyncExternalStore(
    store?.subscribe ?? (() => () => {}),
    store?.getSnapshot ?? (() => null),
    store?.getSnapshot ?? (() => null),
  );

  return { store, snapshot };
}
