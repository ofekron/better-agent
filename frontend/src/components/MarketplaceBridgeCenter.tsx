import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { eventBus } from "../lib/eventBus";
import { MarketplaceConfirmationModal } from "./MarketplaceConfirmationModal";

type IntentStatus =
  | "pending" | "leased" | "awaiting_confirmation" | "committing"
  | "succeeded" | "rejected" | "failed" | "failed_unknown"
  | "expired" | "cancelled";

interface MarketplaceIntent {
  intent_id: string;
  action: "pair" | "install" | "enable" | "disable" | "update" | "uninstall";
  status: IntentStatus;
  extension?: {
    id: string;
    name: string;
    version?: string;
    publisher?: string;
    permission_delta?: string[];
  };
  account_label?: string;
  site_label?: string;
  device_label?: string;
  error?: string;
}

interface MarketplaceBridgeSnapshot {
  connection_state: "unpaired" | "connecting" | "connected" | "offline" | "error";
  intents: MarketplaceIntent[];
}

const TERMINAL = new Set<IntentStatus>([
  "succeeded", "rejected", "failed", "failed_unknown", "expired", "cancelled",
]);
const ACTIONS = new Set<MarketplaceIntent["action"]>([
  "pair", "install", "enable", "disable", "update", "uninstall",
]);
const STATUSES = new Set<IntentStatus>([
  "pending", "leased", "awaiting_confirmation", "committing", "succeeded",
  "rejected", "failed", "failed_unknown", "expired", "cancelled",
]);

async function responseJson<T>(response: Response): Promise<T> {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = (payload as { detail?: unknown }).detail;
    throw new Error(typeof detail === "string" ? detail : `HTTP ${response.status}`);
  }
  return payload as T;
}

function snapshotFrom(value: unknown): MarketplaceBridgeSnapshot {
  if (!value || typeof value !== "object") throw new Error("invalid marketplace snapshot");
  const item = value as Record<string, unknown>;
  if (
    !["unpaired", "connecting", "connected", "offline", "error"].includes(
      String(item.connection_state),
    )
    || !Array.isArray(item.intents)
  ) {
    throw new Error("invalid marketplace snapshot");
  }
  for (const intent of item.intents) {
    if (!intent || typeof intent !== "object") throw new Error("invalid marketplace intent");
    const row = intent as Record<string, unknown>;
    if (
      typeof row.intent_id !== "string"
      || row.intent_id.length === 0
      || row.intent_id.length > 128
      || !ACTIONS.has(row.action as MarketplaceIntent["action"])
      || !STATUSES.has(row.status as IntentStatus)
    ) {
      throw new Error("invalid marketplace intent");
    }
    const bounded = (field: unknown, required = false) => (
      typeof field === "string"
      && field.length <= 500
      && (!required || field.trim().length > 0)
    );
    if (row.error !== undefined && !bounded(row.error)) {
      throw new Error("invalid marketplace intent");
    }
    if (row.action === "pair") {
      if (
        !bounded(row.site_label, true)
        || !bounded(row.account_label, true)
        || !bounded(row.device_label, true)
      ) {
        throw new Error("invalid marketplace pair intent");
      }
      continue;
    }
    if (!row.extension || typeof row.extension !== "object") {
      throw new Error("invalid marketplace extension intent");
    }
    const extension = row.extension as Record<string, unknown>;
    if (!bounded(extension.id, true) || !bounded(extension.name, true)) {
      throw new Error("invalid marketplace extension intent");
    }
    for (const field of ["version", "publisher"] as const) {
      if (extension[field] !== undefined && !bounded(extension[field])) {
        throw new Error("invalid marketplace extension intent");
      }
    }
    if (
      extension.permission_delta !== undefined
      && (
        !Array.isArray(extension.permission_delta)
        || extension.permission_delta.length > 64
        || extension.permission_delta.some((entry) => !bounded(entry, true))
      )
    ) {
      throw new Error("invalid marketplace permission delta");
    }
  }
  return value as MarketplaceBridgeSnapshot;
}

export function MarketplaceBridgeCenter() {
  const { t } = useTranslation();
  const [snapshot, setSnapshot] = useState<MarketplaceBridgeSnapshot | null>(null);
  const [busyIntentId, setBusyIntentId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [fresh, setFresh] = useState(false);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const requestVersion = useRef(0);

  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    setFresh(false);
    try {
      const response = await fetch(`${API}/api/marketplace-bridge`);
      const next = snapshotFrom(await responseJson<unknown>(response));
      if (version !== requestVersion.current) return;
      setSnapshot(next);
      setError("");
      setFresh(true);
    } catch (caught) {
      if (version !== requestVersion.current) return;
      setFresh(false);
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    const refresh = () => void load();
    void Promise.resolve().then(refresh);
    const onDeepLink = () => refresh();
    window.addEventListener("better-agent:deep-link", onDeepLink);
    const off = eventBus.subscribe("marketplace_bridge_changed", refresh);
    return () => {
      window.removeEventListener("better-agent:deep-link", onDeepLink);
      off();
    };
  }, [load]);

  const awaiting = useMemo(
    () => snapshot?.intents.find((intent) => intent.status === "awaiting_confirmation") ?? null,
    [snapshot],
  );
  const visibleStatuses = useMemo(
    () => (snapshot?.intents ?? []).filter(
      (intent) => !dismissed.has(intent.intent_id) && intent !== awaiting,
    ),
    [awaiting, dismissed, snapshot],
  );
  const canDecide = fresh
    && snapshot?.connection_state !== "offline"
    && snapshot?.connection_state !== "error";
  const connectionNotice = snapshot
    && !["connected", "unpaired"].includes(snapshot.connection_state)
    ? snapshot.connection_state
    : null;
  const globalError = awaiting ? "" : error;

  const decide = useCallback(async (intent: MarketplaceIntent, decision: "approve" | "reject") => {
    if (!fresh) return;
    const version = ++requestVersion.current;
    setBusyIntentId(intent.intent_id);
    setFresh(false);
    setError("");
    try {
      const response = await fetch(
        `${API}/api/marketplace-bridge/intents/${encodeURIComponent(intent.intent_id)}/${decision}`,
        { method: "POST" },
      );
      const next = snapshotFrom(await responseJson<unknown>(response));
      if (version !== requestVersion.current) return;
      setSnapshot(next);
      setFresh(true);
    } catch (caught) {
      if (version !== requestVersion.current) return;
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusyIntentId(null);
    }
  }, [fresh]);

  return (
    <>
      {awaiting ? (
        <MarketplaceConfirmationModal
          open
          title={t(`marketplaceBridge.action.${awaiting.action}`)}
          eyebrow={t("marketplaceBridge.request")}
          busy={busyIntentId === awaiting.intent_id}
          disabled={!canDecide}
          error={error}
          confirmLabel={busyIntentId === awaiting.intent_id
            ? t("marketplaceBridge.approving")
            : t("marketplaceBridge.approve")}
          cancelLabel={t("marketplaceBridge.reject")}
          onCancel={() => void decide(awaiting, "reject")}
          onConfirm={() => void decide(awaiting, "approve")}
        >
              {awaiting.action === "pair" ? (
                <>
                  <p>{t("marketplaceBridge.pairHelp")}</p>
                  <dl className="marketplace-intent-facts">
                    <div><dt>{t("marketplaceBridge.site")}</dt><dd>{awaiting.site_label}</dd></div>
                    <div><dt>{t("marketplaceBridge.account")}</dt><dd>{awaiting.account_label}</dd></div>
                    <div><dt>{t("marketplaceBridge.device")}</dt><dd>{awaiting.device_label}</dd></div>
                  </dl>
                </>
              ) : (
                <>
                  <p>{t("marketplaceBridge.actionHelp")}</p>
                  <dl className="marketplace-intent-facts">
                    <div><dt>{t("marketplaceBridge.extension")}</dt><dd>{awaiting.extension?.name}</dd></div>
                    {awaiting.extension?.publisher ? (
                      <div><dt>{t("marketplaceBridge.publisher")}</dt><dd>{awaiting.extension.publisher}</dd></div>
                    ) : null}
                    {awaiting.extension?.version ? (
                      <div><dt>{t("marketplaceBridge.version")}</dt><dd>{awaiting.extension.version}</dd></div>
                    ) : null}
                  </dl>
                  {awaiting.extension?.permission_delta?.length ? (
                    <div className="marketplace-intent-permissions">
                      <strong>{t("marketplaceBridge.permissionChanges")}</strong>
                      <ul>{awaiting.extension.permission_delta.map((item) => <li key={item}>{item}</li>)}</ul>
                    </div>
                  ) : null}
                </>
              )}
        </MarketplaceConfirmationModal>
      ) : null}
      {(globalError || connectionNotice || visibleStatuses.length > 0) ? (
        <aside className="marketplace-bridge-statuses" aria-live="polite">
          {globalError ? (
            <article className="marketplace-bridge-status marketplace-bridge-status--failed" role="alert">
              <strong>{t("marketplaceBridge.connectionFailed")}</strong>
              <span>{globalError}</span>
            </article>
          ) : null}
          {!globalError && connectionNotice ? (
            <article
              className={`marketplace-bridge-status marketplace-bridge-status--${connectionNotice}`}
              role="status"
            >
              <span className="marketplace-bridge-status__pulse" aria-hidden="true" />
              <div>
                <strong>{t("marketplaceBridge.connectionTitle")}</strong>
                <span>{t(`marketplaceBridge.connection.${connectionNotice}`)}</span>
              </div>
            </article>
          ) : null}
          {visibleStatuses.map((intent) => (
            <article
              className={`marketplace-bridge-status marketplace-bridge-status--${intent.status}`}
              key={intent.intent_id}
              role="status"
            >
              <span className="marketplace-bridge-status__pulse" aria-hidden="true" />
              <div>
                <strong>{t(`marketplaceBridge.action.${intent.action}`)}</strong>
                <span>{t(`marketplaceBridge.status.${intent.status}`)}</span>
                {intent.error ? <span className="marketplace-bridge-status__error">{intent.error}</span> : null}
              </div>
              {TERMINAL.has(intent.status) ? (
                <button
                  type="button"
                  onClick={() => setDismissed((current) => new Set(current).add(intent.intent_id))}
                  aria-label={t("userRequest.dismiss")}
                >
                  ×
                </button>
              ) : null}
            </article>
          ))}
        </aside>
      ) : null}
    </>
  );
}
