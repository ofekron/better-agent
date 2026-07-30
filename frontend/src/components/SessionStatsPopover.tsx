import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import type { Provider, Session } from "../types";
import { TokenUsageDisplay } from "./TokenUsage";
import type { PopoverAnchor } from "./SessionTagPopover";
import { API } from "../api";
import { usePricing } from "../hooks/usePricing";
import { selectPrice, type PricingRequest } from "../utils/pricing";

interface Props {
  anchor: PopoverAnchor;
  session: Session;
  /** Provider that ran the session — its kind and base URL decide which
   * published price applies to the session's model. */
  provider?: Provider;
  onClose: () => void;
}

const GAP = 4;
const MARGIN = 8;

/** Fixed-position token-usage stats card anchored to its trigger. Uses
 *  viewport coords (position: fixed) so it is never clipped by the
 *  sidebar's overflow-y container; flipped/clamped to stay on screen.
 *  Modeled on SessionTagPopover. */
export function SessionStatsPopover({ anchor, session, provider, onClose }: Props) {
  const { t } = useTranslation();
  const ref = useRef<HTMLDivElement>(null);
  // The session's own model priced by the provider that ran it. Cost stays
  // unknown rather than guessed when either is missing.
  const request: PricingRequest | null =
    provider?.kind && session.model
      ? {
          provider_id: provider.id,
          kind: provider.kind,
          base_url: provider.base_url || "",
          model: session.model,
        }
      : null;
  const pricing = usePricing(API, request ? [request] : []);
  const price = selectPrice(pricing, request);
  const [stats, setStats] = useState({
    usage: session.token_usage_total ?? null,
    usageLast: session.token_usage_last ?? null,
    contextWindow: session.context_window ?? null,
  });
  const [pos, setPos] = useState<React.CSSProperties>({
    position: "fixed",
    zIndex: 1000,
    top: anchor.bottom + GAP,
    left: anchor.left,
  });

  useBackButtonDismiss(true, onClose);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API}/api/sessions/${encodeURIComponent(session.id)}/stats`, {
      credentials: "include",
    })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled || !data) return;
        setStats({
          usage: data.token_usage_total ?? null,
          usageLast: data.token_usage_last ?? null,
          contextWindow: data.context_window ?? null,
        });
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [session.id]);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let top: number | undefined = anchor.bottom + GAP;
    let bottom: number | undefined;
    if (top + r.height > vh - MARGIN) {
      bottom = vh - anchor.top + GAP;
      top = undefined;
    }
    let left = anchor.left;
    if (left + r.width > vw - MARGIN) left = vw - r.width - MARGIN;
    if (left < MARGIN) left = MARGIN;
    setPos({
      position: "fixed",
      zIndex: 1000,
      ...(bottom !== undefined ? { bottom } : { top }),
      left,
    });
  }, [anchor.top, anchor.bottom, anchor.left]);

  useEffect(() => {
    const onDocClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  return createPortal(
    <div
      ref={ref}
      className="session-stats-popover"
      role="dialog"
      aria-modal="false"
      aria-label={t("tokens.stats")}
      style={pos}
    >
      <div className="session-stats-popover-header">{t("tokens.stats")}</div>
      <TokenUsageDisplay
        usage={stats.usage}
        usageLast={stats.usageLast}
        contextWindow={stats.contextWindow}
        price={price}
      />
    </div>,
    document.body,
  );
}
