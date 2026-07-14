import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "src/api";
import { runThreeStateSync } from "src/progress/store";
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
  transactionId: string;
  name: string;
  amount: number;
  currency: string;
  interval: string;
}

type PaddleLike = {
  Environment: { set: (env: string) => void };
  Initialize: (options: { token: string; eventCallback?: (event: { name?: string }) => void }) => void;
  Checkout: {
    open: (options: {
      transactionId: string;
      settings: {
        displayMode: "inline";
        frameTarget: string;
        frameInitialHeight: number;
        frameStyle: string;
      };
    }) => void;
    close: () => void;
  };
};

declare global {
  interface Window {
    Paddle?: PaddleLike;
  }
}

const PADDLE_JS_URL = "https://cdn.paddle.com/paddle/v2/paddle.js";
const CHECKOUT_FRAME_CLASS = "extension-paddle-checkout-frame";
const ENTITLEMENT_POLL_MS = 2000;
const ENTITLEMENT_POLL_TIMEOUT_MS = 3 * 60 * 1000;

async function loadPaddle(): Promise<PaddleLike> {
  if (!window.Paddle) {
    await new Promise<void>((resolve, reject) => {
      const existing = document.querySelector(`script[src="${PADDLE_JS_URL}"]`);
      if (existing) {
        existing.addEventListener("load", () => resolve());
        existing.addEventListener("error", () => reject(new Error("Paddle.js failed to load")));
        if (window.Paddle) resolve();
        return;
      }
      const script = document.createElement("script");
      script.src = PADDLE_JS_URL;
      script.addEventListener("load", () => resolve());
      script.addEventListener("error", () => reject(new Error("Paddle.js failed to load")));
      document.head.appendChild(script);
    });
  }
  if (!window.Paddle) throw new Error("Paddle.js failed to load");
  return window.Paddle;
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
  const [phase, setPhase] = useState<"loading" | "ready" | "finalizing" | "error">("loading");
  const [error, setError] = useState("");
  const [checkout, setCheckout] = useState<CheckoutInfo | null>(null);
  const finalizingRef = useRef(false);
  const cancel = () => {
    if (!finalizingRef.current) onDone({ status: "cancelled" });
  };
  useBackButtonDismiss(open, cancel);

  const backendBase = `${API}/api/extensions/${encodeURIComponent(extensionId)}/backend`;

  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    finalizingRef.current = false;
    setPhase("loading");
    setError("");
    setCheckout(null);

    async function pollEntitlement(): Promise<ExtensionPaymentResult> {
      const startedAt = Date.now();
      while (Date.now() - startedAt < ENTITLEMENT_POLL_TIMEOUT_MS) {
        const payload = await fetchJson(
          `${backendBase}/billing/entitlement/${encodeURIComponent(productId)}`,
        );
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

    function onCheckoutEvent(event: { name?: string }) {
      if (cancelled || event?.name !== "checkout.completed" || finalizingRef.current) return;
      finalizingRef.current = true;
      setPhase("finalizing");
      void pollEntitlement().then((result) => {
        if (!cancelled) onDone(result);
      });
    }

    async function prepare() {
      try {
        const [config, session] = await Promise.all([
          fetchJson(`${backendBase}/billing/config`),
          runThreeStateSync({
            operationId: `extensions:payment:${extensionId}:${productId}`,
            action: t("extensionPayment.title"),
            reconcile: () => undefined,
            mutate: () => fetchJson(`${backendBase}/billing/checkout`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ product_id: productId }),
            }),
          }).then(({ result }) => result),
        ]);
        if (cancelled) return;
        const product = (session.product ?? {}) as Record<string, unknown>;
        const info: CheckoutInfo = {
          transactionId: String(session.transaction_id ?? ""),
          // Displayed price/name come from the marketplace server via the
          // extension backend — never from the requesting iframe's message.
          name: String(product.name ?? ""),
          amount: Number(product.amount ?? 0),
          currency: String(product.currency ?? ""),
          interval: String(product.interval ?? ""),
        };
        const clientToken = String(config.client_token ?? "");
        const environment = String(config.environment ?? "production");
        if (!info.transactionId || !clientToken) {
          throw new Error(t("extensionPayment.unavailable"));
        }
        const paddle = await loadPaddle();
        if (cancelled) return;
        if (environment === "sandbox") paddle.Environment.set("sandbox");
        // Initialize on every open so completion events route to THIS modal.
        paddle.Initialize({ token: clientToken, eventCallback: onCheckoutEvent });
        setCheckout(info);
        setPhase("ready");
        paddle.Checkout.open({
          transactionId: info.transactionId,
          settings: {
            displayMode: "inline",
            frameTarget: CHECKOUT_FRAME_CLASS,
            frameInitialHeight: 450,
            frameStyle: "width:100%;min-width:312px;background-color:transparent;border:none;",
          },
        });
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
        setPhase("error");
      }
    }

    void prepare();
    return () => {
      cancelled = true;
      try {
        window.Paddle?.Checkout.close();
      } catch {
        // checkout may not have opened
      }
    };
  }, [open, backendBase, productId, t, onDone]);

  if (!open) return null;

  return (
    <div className="modal-overlay" onClick={cancel}>
      <div className="modal-content" style={{ maxWidth: "480px" }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{t("extensionPayment.title")}</h2>
          <button className="modal-close" onClick={cancel} disabled={phase === "finalizing"}>
            &times;
          </button>
        </div>
        <div className="modal-body">
          {phase === "loading" && (
            <p style={{ margin: "16px 0", color: "var(--text-secondary)" }}>{t("extensionPayment.loading")}</p>
          )}
          {phase === "error" && <div className="setup-error">{error}</div>}
          {(phase === "ready" || phase === "finalizing") && checkout && (
            <p style={{ margin: "8px 0 16px", color: "var(--text-secondary)" }}>
              {checkout.name}
              {" — "}
              {formatAmount(checkout.amount, checkout.currency)}
              {checkout.interval &&
                ` / ${t(`extensionPayment.interval.${checkout.interval}`, checkout.interval)}`}
            </p>
          )}
          <div className={CHECKOUT_FRAME_CLASS} data-testid="paddle-checkout-frame" />
          {phase === "finalizing" && (
            <p style={{ margin: "16px 0 0", color: "var(--text-secondary)" }}>
              {t("extensionPayment.finalizing")}
            </p>
          )}
        </div>
        <div className="modal-footer">
          <button type="button" className="btn-secondary" onClick={cancel} disabled={phase === "finalizing"}>
            {t("app.cancel")}
          </button>
        </div>
      </div>
    </div>
  );
}
