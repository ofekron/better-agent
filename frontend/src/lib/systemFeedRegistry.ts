// ADR 0011 (System & Host Surface, Package C): the shared `/ws/v2/surface`
// connection for all 12 system feeds (extension notices/config, harness
// profiles, extension UI, extension catalog, marketplace bridge/intents,
// schedules, host startup tasks, installation capabilities, machines,
// node registrations) — built on `lib/surfaceFeedSocket.ts`'s generic
// ref-counted factory (the single shared implementation of the pattern
// `useProviderSurfaceSocket.ts` established), same "one connection, many
// subscribers" shape `lib/runSummaryRegistry.ts`/
// `hooks/useProviderSurfaceSocket.ts` use for their own planes.
//
// Per-concern state projection (cold-hydrate + live-merge) lives in each
// consuming component/hook, NOT here — mirrors Package D's own division
// of responsibility (`useProviderSurfaceSocket.ts` owns only the
// transport; `useProviderChanged.ts`/`useModelsCatalogChanged.ts` own
// their own state on top of it).

import type { SurfaceSocketHandlers } from "../adapter/client";
import type {
  DistributiveOmit,
  FeedName,
  SystemFrame,
  SystemIntentWire,
  TransportAckFrame,
} from "../adapter/wire";
import { createFeedSocketRegistry } from "./surfaceFeedSocket";
import { uuidv4 } from "./uuid";

/** All 12 system feed names (`adapter_api.py`'s `_SYSTEM_FEED_FRAME_TYPES`
 * keys) — subscribed together on ONE connection since every system frame
 * type is disjoint (see `wire.ts`'s `isSystemFrame`) and every consumer of
 * this module wants the live stream regardless of which GET route it also
 * calls; there is no per-consumer cost to over-subscribing (change-only
 * push, and an idle concern simply never emits). */
export const SYSTEM_FEED_NAMES: FeedName[] = [
  "extension_notices",
  "extension_config",
  "harness_profiles",
  "extension_ui",
  "extension_catalog",
  "marketplace_bridge",
  "marketplace_intents",
  "schedules",
  "host_startup_tasks",
  "installation_capabilities",
  "machines",
  "node_registrations",
];

const registry = createFeedSocketRegistry<SystemFrame>(SYSTEM_FEED_NAMES, (handlers, dispatch) => {
  (handlers as SurfaceSocketHandlers).onSystemFrame = dispatch;
});

export function subscribeSystemFrames(listener: (frame: SystemFrame) => void): () => void {
  return registry.subscribeFrames(listener);
}

export function subscribeSystemSocketConnection(listener: (open: boolean) => void): () => void {
  return registry.subscribeConnection(listener);
}

export function isSystemSocketOpen(): boolean {
  return registry.isOpen();
}

/** Submits a `SystemIntent` (ADR 0011) over the shared connection. `null`
 * synchronously when not open — callers fall back to REST (native-when-
 * open/legacy-fallback, same posture as `submitProviderIntent`).
 * `DistributiveOmit`, not a plain `Omit` — see `wire.ts`'s own note on
 * `ProviderIntentWire`: a plain `Omit` over a union type collapses to the
 * INTERSECTION of every variant's keys, silently erasing each variant's
 * own fields (`extension_id`, `harness_profile_id`, `device_ref`, ...). */
export function submitSystemIntent(
  intent: DistributiveOmit<SystemIntentWire, "cv" | "intent_id" | "session_id">,
): Promise<TransportAckFrame> | null {
  return registry.submitIntent(intent as { kind: string } & Record<string, unknown>);
}

/** Resolves with the first `SystemFrame` matching `matches`, or `undefined`
 * after `timeoutMs` — the "wait for a live frame" half of the event-driven
 * completion pattern below, factored out so `submitSystemIntentAwaitingUpsert`
 * and any bespoke wait (e.g. `useMachines.ts`'s credential-sync-result
 * collection, which needs to accumulate several frames rather than resolve
 * on the first) can both build on it. */
export function waitForSystemFrame<F extends SystemFrame>(
  matches: (frame: SystemFrame) => frame is F,
  timeoutMs = 1500,
): Promise<F | undefined> {
  return new Promise((resolve) => {
    let settled = false;
    const unsubscribe = subscribeSystemFrames((frame) => {
      if (settled || !matches(frame)) return;
      settled = true;
      clearTimeout(timer);
      unsubscribe();
      resolve(frame);
    });
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      unsubscribe();
      resolve(undefined);
    }, timeoutMs);
  });
}

/** Submits a `SystemIntent` and resolves once BOTH its accept/reject
 * outcome AND a live frame matching `matches` are known — the "wait for
 * the resulting upsert" completion pattern (ADR 0006 §5: acks are
 * accept/reject-only, so a create-shaped intent whose caller needs the
 * server-generated resource observes it arrive on the live plane instead
 * of a bespoke synchronous response) — same idiom
 * `lib/sessionSurfaceRegistry.ts`'s `submitIntentAwaitingRef` already
 * establishes for ADR 0008, built here on `surfaceFeedSocket.ts`'s
 * caller-supplied-`intent_id` support instead of a second hand-rolled
 * connection. `null` when the connection isn't open (REST fallback, same
 * as `submitSystemIntent` alone); `{ok:false}` on a real rejection;
 * `{ok:true, frame}` on acceptance, `frame` `undefined` if the
 * correlation window elapsed before the live plane delivered the matching
 * frame. */
export async function submitSystemIntentAwaitingUpsert<F extends SystemFrame & { intent_id?: string | null }>(
  intent: DistributiveOmit<SystemIntentWire, "cv" | "intent_id" | "session_id">,
  /** Receives the intent_id this call generated, so the predicate can
   * correlate on `frame.intent_id === intentId` (frames this intent did
   * NOT produce — e.g. another tab's concurrent create — must not match). */
  matches: (frame: SystemFrame, intentId: string) => frame is F,
  timeoutMs = 1500,
): Promise<{ ok: true; frame: F | undefined } | { ok: false; code: string; message: string } | null> {
  if (!isSystemSocketOpen()) return null;
  const intentId = uuidv4();
  const submitted = registry.submitIntent({
    ...intent,
    intent_id: intentId,
  } as { kind: string; intent_id?: string } & Record<string, unknown>);
  if (!submitted) return null;
  // Race the frame-watcher against the ack from the moment of submission —
  // a fast backend can deliver the upsert before the ack's own late-
  // rejection window resolves (mirrors `submitIntentAwaitingRef`'s own
  // comment).
  const framePromise = waitForSystemFrame(
    (frame): frame is F => matches(frame, intentId), timeoutMs,
  );
  const ack = await submitted;
  if (ack.type === "intent_rejected") {
    return { ok: false, code: ack.code, message: ack.message };
  }
  return { ok: true, frame: await framePromise };
}
