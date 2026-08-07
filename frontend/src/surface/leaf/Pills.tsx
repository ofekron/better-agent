// Lifecycle-notice pills — native-props equivalents of MessageBubble.tsx's
// RetryingPill/DetachedPill/RecoveringPill/AutoRetryPill/RateLimitBox/
// ContinuationPill, reusing the SAME CSS classes and i18n keys so the
// native path renders identically without importing legacy ChatMessage
// types. One canonical visual per lifecycle kind, referenced from
// surface/nodes/LifecycleNotice.tsx and surface/nodes/ContinuationSession.tsx.

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

function secondsUntil(target: number): number {
  return Math.max(0, Math.ceil((target - Date.now()) / 1000));
}

/** Callers key this on `retryAt` (see nodes/LifecycleNotice.tsx) so a
 * changed `retryAt` remounts fresh instead of needing an effect to
 * resync `target`-derived state — the lazy `useState` initializer and the
 * interval callback are the only two places `Date.now()` is read, both
 * exempt from the render-purity rule (only a bare call in the render
 * body itself is impure), and the interval callback's setState runs
 * inside a genuine async/deferred context, not synchronously in the
 * effect body. */
export function RetryingPill({ retryAt }: { retryAt: string }) {
  const { t } = useTranslation();
  const target = useMemo(() => new Date(retryAt).getTime(), [retryAt]);
  const [secondsLeft, setSecondsLeft] = useState<number>(() => secondsUntil(target));
  useEffect(() => {
    const id = window.setInterval(() => setSecondsLeft(secondsUntil(target)), 1000);
    return () => window.clearInterval(id);
  }, [target]);
  const label =
    secondsLeft < 90
      ? t("message.retryingIn", { seconds: secondsLeft })
      : secondsLeft < 5400
        ? `Retrying in ${Math.ceil(secondsLeft / 60)}m`
        : `Retrying in ${Math.ceil(secondsLeft / 3600)}h`;
  return (
    <div className="retrying-pill" data-testid="surface-retrying-pill" role="status" aria-live="polite">
      <span className="retrying-spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function DetachedPill() {
  return (
    <div className="detached-pill" role="status" aria-live="polite">
      <span className="detached-spinner" aria-hidden="true" />
      <span>Reconnecting — agent still running…</span>
    </div>
  );
}

export function RecoveringPill() {
  return (
    <div className="recovering-pill" data-testid="surface-recovering-pill" role="status" aria-live="polite">
      <span className="recovering-spinner" aria-hidden="true" />
      <span>Updating state…</span>
    </div>
  );
}

export function AutoRetryPill({ kind, count }: { kind?: string; count: number }) {
  return (
    <div className="auto-retry-pill" data-testid="surface-auto-retry-pill" role="status">
      <span aria-hidden="true">↻</span>
      <span>
        {kind === "rate_limit"
          ? "Auto-retried after rate limit"
          : kind === "transient"
            ? "Auto-retried after a transient error"
            : "Auto-retried"}
        {count > 1 ? ` ×${count}` : ""} — recovered
      </span>
    </div>
  );
}

export function RateLimitBox({ label, errorText }: { label: string; errorText?: string }) {
  return (
    <div className="message-status status-warning" data-testid="surface-retry-warning">
      <div className="error-block-header">
        <span className="status-dot" />
        <span className="error-block-label">{label}</span>
        <span className="status-error-text">{errorText ?? "Rate limit exceeded"}</span>
      </div>
    </div>
  );
}

export function ContinuationPill({ chainDepth }: { chainDepth: number }) {
  const { t } = useTranslation();
  return (
    <div className="continuation-pill" data-testid="surface-continuation-pill" role="status" aria-live="polite">
      <span className="continuation-spinner" aria-hidden="true" />
      <span>
        {t("message.autoContinuing")}
        {chainDepth > 1 ? ` (${chainDepth})` : ""}
      </span>
    </div>
  );
}
