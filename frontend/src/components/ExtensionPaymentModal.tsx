import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "src/api";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";

export interface ExtensionPaymentResult {
  status: "active" | "failed" | "cancelled";
  entitlementToken?: string;
  error?: string;
}

interface Props {
  open: boolean;
  extensionId: string;
  productId: string;
  onDone: (result: ExtensionPaymentResult) => void;
}

interface CheckoutInfo {
  clientSecret: string;
  publishableKey: string;
  name: string;
  amount: number;
  currency: string;
  interval: string;
}

type StripeLike = {
  elements: (options: { clientSecret: string }) => {
    create: (kind: string) => { mount: (el: HTMLElement) => void; unmount: () => void };
  };
  confirmPayment: (options: {
    elements: unknown;
    redirect: "if_required";
  }) => Promise<{ error?: { message?: string } }>;
};

declare global {
  interface Window {
    Stripe?: (key: string) => StripeLike;
  }
}

const STRIPE_JS_URL = "https://js.stripe.com/v3";
const ENTITLEMENT_POLL_MS = 2000;
const ENTITLEMENT_POLL_TIMEOUT_MS = 3 * 60 * 1000;

async function loadStripe(publishableKey: string): Promise<StripeLike> {
  if (!window.Stripe) {
    await new Promise<void>((resolve, reject) => {
      const existing = document.querySelector(`script[src="${STRIPE_JS_URL}"]`);
      if (existing) {
        existing.addEventListener("load", () => resolve());
        existing.addEventListener("error", () => reject(new Error("Stripe.js failed to load")));
        if (window.Stripe) resolve();
        return;
      }
      const script = document.createElement("script");
      script.src = STRIPE_JS_URL;
      script.addEventListener("load", () => resolve());
      script.addEventListener("error", () => reject(new Error("Stripe.js failed to load")));
      document.head.appendChild(script);
    });
  }
  if (!window.Stripe) throw new Error("Stripe.js failed to load");
  return window.Stripe(publishableKey);
}

async function fetchJson(url: string, options: RequestInit = {}): Promise<Record<string, unknown>> {
  const response = await fetch(url, { credentials: "include", ...options });
  if (!response.ok) throw new Error(await response.text());
  return (await response.json()) as Record<string, unknown>;
}

function formatAmount(amount: number, currency: string): string {
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency: currency.toUpperCase() }).format(
      amount / 100,
    );
  } catch {
    return `${(amount / 100).toFixed(2)} ${currency.toUpperCase()}`;
  }
}

export function ExtensionPaymentModal({ open, extensionId, productId, onDone }: Props) {
  const { t } = useTranslation();
  const [phase, setPhase] = useState<"loading" | "ready" | "confirming" | "finalizing" | "error">("loading");
  const [error, setError] = useState("");
  const [checkout, setCheckout] = useState<CheckoutInfo | null>(null);
  const paymentElementRef = useRef<HTMLDivElement | null>(null);
  const stripeRef = useRef<StripeLike | null>(null);
  const elementsRef = useRef<ReturnType<StripeLike["elements"]> | null>(null);
  const mountedElementRef = useRef<{ unmount: () => void } | null>(null);
  const cancel = () => onDone({ status: "cancelled" });
  useBackButtonDismiss(open, cancel);

  const backendBase = `${API}/api/extensions/${encodeURIComponent(extensionId)}/backend`;

  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    setPhase("loading");
    setError("");
    setCheckout(null);

    async function prepare() {
      try {
        const [config, session] = await Promise.all([
          fetchJson(`${backendBase}/billing/config`),
          fetchJson(`${backendBase}/billing/checkout`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ product_id: productId }),
          }),
        ]);
        if (cancelled) return;
        const product = (session.product ?? {}) as Record<string, unknown>;
        const info: CheckoutInfo = {
          clientSecret: String(session.client_secret ?? ""),
          publishableKey: String(config.publishable_key ?? ""),
          // Displayed price/name come from the marketplace server via the
          // extension backend — never from the requesting iframe's message.
          name: String(product.name ?? ""),
          amount: Number(product.amount ?? 0),
          currency: String(product.currency ?? ""),
          interval: String(product.interval ?? ""),
        };
        if (!info.clientSecret || !info.publishableKey) {
          throw new Error(t("extensionPayment.unavailable"));
        }
        const stripe = await loadStripe(info.publishableKey);
        if (cancelled) return;
        stripeRef.current = stripe;
        const elements = stripe.elements({ clientSecret: info.clientSecret });
        elementsRef.current = elements;
        setCheckout(info);
        setPhase("ready");
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
        setPhase("error");
      }
    }

    void prepare();
    return () => {
      cancelled = true;
      mountedElementRef.current?.unmount();
      mountedElementRef.current = null;
      stripeRef.current = null;
      elementsRef.current = null;
    };
  }, [open, backendBase, productId, t]);

  useEffect(() => {
    if (phase !== "ready" || !paymentElementRef.current || !elementsRef.current) return;
    if (mountedElementRef.current) return;
    const element = elementsRef.current.create("payment");
    element.mount(paymentElementRef.current);
    mountedElementRef.current = element;
  }, [phase]);

  async function pollEntitlement(): Promise<ExtensionPaymentResult> {
    const startedAt = Date.now();
    while (Date.now() - startedAt < ENTITLEMENT_POLL_TIMEOUT_MS) {
      const payload = await fetchJson(`${backendBase}/billing/entitlement/${encodeURIComponent(productId)}`);
      const status = String(payload.status ?? "pending");
      if (status === "active") {
        return { status: "active", entitlementToken: String(payload.entitlement_token ?? "") };
      }
      if (status === "failed") {
        return { status: "failed", error: t("extensionPayment.failed") };
      }
      await new Promise((resolve) => setTimeout(resolve, ENTITLEMENT_POLL_MS));
    }
    return { status: "failed", error: t("extensionPayment.timeout") };
  }

  async function confirm() {
    const stripe = stripeRef.current;
    const elements = elementsRef.current;
    if (!stripe || !elements) return;
    setPhase("confirming");
    setError("");
    try {
      const result = await stripe.confirmPayment({ elements, redirect: "if_required" });
      if (result.error) {
        setError(result.error.message || t("extensionPayment.failed"));
        setPhase("ready");
        return;
      }
      setPhase("finalizing");
      onDone(await pollEntitlement());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPhase("ready");
    }
  }

  if (!open) return null;

  return (
    <div className="modal-overlay" onClick={cancel}>
      <div className="modal-content" style={{ maxWidth: "440px" }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{t("extensionPayment.title")}</h2>
          <button className="modal-close" onClick={cancel}>
            &times;
          </button>
        </div>
        <div className="modal-body">
          {phase === "loading" && (
            <p style={{ margin: "16px 0", color: "var(--text-secondary)" }}>{t("extensionPayment.loading")}</p>
          )}
          {phase === "error" && <div className="setup-error">{error}</div>}
          {(phase === "ready" || phase === "confirming" || phase === "finalizing") && checkout && (
            <>
              <p style={{ margin: "8px 0 16px", color: "var(--text-secondary)" }}>
                {checkout.name}
                {" — "}
                {formatAmount(checkout.amount, checkout.currency)}
                {checkout.interval &&
                  ` / ${t(`extensionPayment.interval.${checkout.interval}`, checkout.interval)}`}
              </p>
              <div ref={paymentElementRef} data-testid="stripe-payment-element" />
              {phase === "finalizing" && (
                <p style={{ margin: "16px 0 0", color: "var(--text-secondary)" }}>
                  {t("extensionPayment.finalizing")}
                </p>
              )}
              {error && <div className="setup-error">{error}</div>}
            </>
          )}
        </div>
        <div className="modal-footer">
          <button type="button" className="btn-secondary" onClick={cancel} disabled={phase === "finalizing"}>
            {t("app.cancel")}
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={() => void confirm()}
            disabled={phase !== "ready" || !checkout}
          >
            {phase === "confirming" || phase === "finalizing"
              ? t("extensionPayment.processing")
              : checkout
                ? t("extensionPayment.pay", { amount: formatAmount(checkout.amount, checkout.currency) })
                : t("extensionPayment.loading")}
          </button>
        </div>
      </div>
    </div>
  );
}
