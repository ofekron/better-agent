import { useEffect } from "react";
import { eventBus } from "src/lib/eventBus";
import { subscribeProviderFrames } from "./useProviderSurfaceSocket";

/** "Something about the provider roster changed, refetch" — `cb` is
 * expected to be a full `GET /api/providers` refetch (e.g. App.tsx's
 * `syncProvider`), same contract as before this migration.
 *
 * Dual-subscribed to both the legacy `provider_changed` broadcast (kept —
 * `backend/providers_api.py`'s `_broadcast_provider_changed` fires it
 * unconditionally from every mutation route, so it is a correct,
 * unconditional trigger regardless of the v2 flag/connection state) AND
 * the v2 `"providers"` feed's four per-concern frames
 * (`provider_upsert`/`credential_state`/`model_catalog_changed`/
 * `login_flow_frame`) — traced 1:1 against `_broadcast_provider_changed`,
 * which calls `_publish_provider_facts` with the SAME state snapshot on
 * every one of its 8 call sites (create/patch/suspend/retry-credential/
 * login/cancel-login/delete/periodic-auth-refresh), so the v2 side never
 * misses a change the legacy side would have caught. Kept dual rather than
 * cut over fully: this is purely the READ/refetch-trigger side, which is
 * safe to migrate; provider MUTATIONS stay on legacy REST for now (see
 * SettingsPage.tsx's mutation call sites) because
 * `backend/adapter_api.py`'s `ProviderCommandPort` swallows business-logic
 * `HTTPException`/`ValueError` into a log line with no compensating
 * `IntentRejected`/frame — so as long as mutations themselves are REST,
 * the legacy `provider_changed` broadcast they trigger remains the only
 * unconditionally-reliable refetch signal; the v2 frames are added
 * coverage, not a replacement, until that gap closes. */
export function useProviderChanged(cb: () => void): void {
  useEffect(() => {
    return eventBus.subscribe("provider_changed", cb);
  }, [cb]);

  useEffect(() => {
    return subscribeProviderFrames(() => cb());
  }, [cb]);
}
