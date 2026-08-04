import { useEffect } from "react";
import { eventBus } from "src/lib/eventBus";

export function useProviderChanged(cb: () => void): void {
  useEffect(() => {
    return eventBus.subscribe("provider_changed", cb);
  }, [cb]);
}
