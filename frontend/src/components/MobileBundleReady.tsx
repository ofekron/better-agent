import { useEffect, useRef } from "react";

export function MobileBundleReady({ onReady }: { onReady: () => void }) {
  const signaled = useRef(false);

  useEffect(() => {
    if (signaled.current) return;
    signaled.current = true;
    onReady();
  }, [onReady]);

  return null;
}
