// `lifecycle_notice` node — dispatches by `LifecycleNoticeKindWire` to the
// matching pill leaf, one canonical visual per kind (chat-panel.md:
// Retrying(until) | Detached | Recovering | AutoRetried(count) |
// RateLimited(fallback options)).

import type { LifecycleNoticePayloadWire, NodeWire } from "../../adapter/wire";
import { AutoRetryPill, DetachedPill, RateLimitBox, RecoveringPill, RetryingPill } from "../leaf/Pills";

export function LifecycleNoticeView({ node }: { node: NodeWire }) {
  const payload = node.payload as LifecycleNoticePayloadWire | null;
  if (!payload) return null;
  const data = payload.data ?? {};
  switch (payload.kind) {
    case "retrying": {
      const retryAt = typeof data.retry_at === "string" ? data.retry_at : null;
      // Keyed on retryAt: RetryingPill computes its countdown once at
      // mount (see leaf/Pills.tsx) — a changed retryAt should restart
      // the countdown from a fresh instance, not resync via an effect.
      return retryAt ? <RetryingPill key={retryAt} retryAt={retryAt} /> : null;
    }
    case "detached":
      return <DetachedPill />;
    case "recovering":
      return <RecoveringPill />;
    case "auto_retried": {
      const retryKind = typeof data.retry_kind === "string" ? data.retry_kind : undefined;
      const count = typeof data.count === "number" ? data.count : 1;
      return <AutoRetryPill kind={retryKind} count={count} />;
    }
    case "rate_limited": {
      const text = typeof data.text === "string" ? data.text : undefined;
      return <RateLimitBox label="Retrying" errorText={text} />;
    }
    default:
      return null;
  }
}
