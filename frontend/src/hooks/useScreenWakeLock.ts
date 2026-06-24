import { useEffect, useRef } from "react";

type ScreenWakeLockSentinel = EventTarget & {
  released: boolean;
  release: () => Promise<void>;
};

type WakeLockNavigator = Navigator & {
  wakeLock?: {
    request: (type: "screen") => Promise<ScreenWakeLockSentinel>;
  };
};

export function useScreenWakeLock() {
  const sentinelRef = useRef<ScreenWakeLockSentinel | null>(null);
  const requestTokenRef = useRef(0);

  useEffect(() => {
    let mounted = true;

    const releaseLock = async () => {
      requestTokenRef.current += 1;
      const sentinel = sentinelRef.current;
      sentinelRef.current = null;
      if (!sentinel || sentinel.released) return;
      await sentinel.release().catch(() => {});
    };

    const acquireLock = async () => {
      if (sentinelRef.current && !sentinelRef.current.released) return;
      if (document.visibilityState !== "visible") return;

      const wakeLock = (navigator as WakeLockNavigator).wakeLock;
      if (!wakeLock) return;

      const token = requestTokenRef.current + 1;
      requestTokenRef.current = token;

      try {
        const sentinel = await wakeLock.request("screen");
        if (!mounted || token !== requestTokenRef.current || document.visibilityState !== "visible") {
          await sentinel.release().catch(() => {});
          return;
        }

        sentinelRef.current = sentinel;
        sentinel.addEventListener("release", () => {
          if (sentinelRef.current === sentinel) {
            sentinelRef.current = null;
          }
        });
      } catch {
        sentinelRef.current = null;
      }
    };

    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        void acquireLock();
        return;
      }
      void releaseLock();
    };

    void acquireLock();
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      mounted = false;
      document.removeEventListener("visibilitychange", onVisibilityChange);
      void releaseLock();
    };
  }, []);
}
