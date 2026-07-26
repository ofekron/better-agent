import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { eventBus } from "../lib/eventBus";
import {
  marketplaceBridgeSnapshotFrom,
  type MarketplaceBridgeSnapshot,
  type MarketplaceDevice,
  type MarketplaceIntent,
  type MarketplaceIntentStatus,
} from "../marketplaceBridgeSnapshot";
import { MarketplaceConfirmationModal } from "./MarketplaceConfirmationModal";

const TERMINAL = new Set<MarketplaceIntentStatus>([
  "redeemed", "succeeded", "rejected", "failed", "failed_unknown", "expired", "cancelled",
]);

async function responseJson<T>(response: Response): Promise<T> {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = (payload as { detail?: unknown }).detail;
    throw new Error(typeof detail === "string" ? detail : `HTTP ${response.status}`);
  }
  return payload as T;
}

export function MarketplaceBridgeCenter() {
  const { t } = useTranslation();
  const [snapshot, setSnapshot] = useState<MarketplaceBridgeSnapshot | null>(null);
  const [busyIntentId, setBusyIntentId] = useState<string | null>(null);
  const [busyDeviceId, setBusyDeviceId] = useState<string | null>(null);
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [revokeError, setRevokeError] = useState("");
  const [fresh, setFresh] = useState(false);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const requestVersion = useRef(0);

  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    setFresh(false);
    try {
      const response = await fetch(`${API}/api/marketplace-bridge`);
      const next = marketplaceBridgeSnapshotFrom(await responseJson<unknown>(response));
      if (version !== requestVersion.current) return;
      setSnapshot(next);
      const nextAwaitsConfirmation = next.intents.some(
        (intent) => intent.status === "awaiting_confirmation",
      );
      setSelectedDeviceId((current) => (
        current
        && !nextAwaitsConfirmation
        && next.paired_devices.some((device) => device.device_id === current)
          ? current
          : null
      ));
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
  const selectedDevice = useMemo(
    () => awaiting
      ? null
      : snapshot?.paired_devices.find((device) => device.device_id === selectedDeviceId) ?? null,
    [awaiting, selectedDeviceId, snapshot],
  );
  const visibleStatuses = useMemo(
    () => (snapshot?.intents ?? []).filter(
      (intent) => !dismissed.has(intent.intent_id) && intent !== awaiting,
    ),
    [awaiting, dismissed, snapshot],
  );
  const canDecide = fresh
    && snapshot?.connection_state !== "offline";
  const canRevoke = fresh
    && !awaiting
    && !snapshot?.revocation_pending
    && snapshot?.connection_state !== "offline"
    && busyDeviceId === null;
  const connectionNotice = snapshot
    && !["connected", "unpaired"].includes(snapshot.connection_state)
    ? snapshot.connection_state
    : null;
  const globalError = awaiting || selectedDevice ? "" : error;

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
      const next = marketplaceBridgeSnapshotFrom(await responseJson<unknown>(response));
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

  const revoke = useCallback(async (device: MarketplaceDevice) => {
    if (!canRevoke) return;
    const version = ++requestVersion.current;
    setBusyDeviceId(device.device_id);
    setFresh(false);
    setRevokeError("");
    try {
      const response = await fetch(
        `${API}/api/marketplace-bridge/devices/${encodeURIComponent(device.device_id)}`,
        { method: "DELETE" },
      );
      const next = marketplaceBridgeSnapshotFrom(await responseJson<unknown>(response));
      if (version !== requestVersion.current) return;
      setSnapshot(next);
      setSelectedDeviceId(null);
      setFresh(true);
    } catch (caught) {
      if (version !== requestVersion.current) return;
      setRevokeError(caught instanceof Error ? caught.message : String(caught));
      await load();
    } finally {
      setBusyDeviceId((current) => current === device.device_id ? null : current);
    }
  }, [canRevoke, load]);

  return (
    <>
      {selectedDevice ? (
        <MarketplaceConfirmationModal
          open
          title={t("marketplaceBridge.revokeTitle")}
          eyebrow={t("marketplaceBridge.connectedDevice")}
          busy={busyDeviceId === selectedDevice.device_id}
          confirmDisabled={!canRevoke}
          error={revokeError || error}
          confirmLabel={busyDeviceId === selectedDevice.device_id
            ? t("marketplaceBridge.revoking")
            : t("marketplaceBridge.revoke")}
          cancelLabel={t("app.cancel")}
          onCancel={() => {
            setSelectedDeviceId(null);
            setRevokeError("");
          }}
          onConfirm={() => void revoke(selectedDevice)}
        >
          <p>{t("marketplaceBridge.revokeHelp")}</p>
          <dl className="marketplace-intent-facts">
            <div>
              <dt>{t("marketplaceBridge.device")}</dt>
              <dd>{selectedDevice.label}</dd>
            </div>
          </dl>
        </MarketplaceConfirmationModal>
      ) : awaiting ? (
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
      {(globalError
        || connectionNotice
        || snapshot?.revocation_pending
        || (snapshot?.paired_devices.length ?? 0) > 0
        || visibleStatuses.length > 0) ? (
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
          {!globalError && snapshot?.revocation_pending ? (
            <article
              className="marketplace-bridge-status marketplace-bridge-status--committing"
              role="status"
            >
              <span className="marketplace-bridge-status__pulse" aria-hidden="true" />
              <div>
                <strong>{t("marketplaceBridge.revokingTitle")}</strong>
                <span>{t("marketplaceBridge.revokingHelp")}</span>
              </div>
            </article>
          ) : null}
          {!snapshot?.revocation_pending
            ? snapshot?.paired_devices.map((device) => (
              <article
                className="marketplace-bridge-status marketplace-bridge-device"
                key={device.device_id}
                role="status"
              >
                <span className="marketplace-bridge-status__pulse" aria-hidden="true" />
                <div>
                  <strong>{t("marketplaceBridge.connectedDevice")}</strong>
                  <span>{device.label}</span>
                </div>
                <button
                  type="button"
                  className="marketplace-bridge-device__revoke"
                  disabled={!canRevoke}
                  onClick={() => {
                    setRevokeError("");
                    setSelectedDeviceId(device.device_id);
                  }}
                >
                  {t("marketplaceBridge.revoke")}
                </button>
              </article>
            ))
            : null}
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
