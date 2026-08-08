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
import { subscribeSystemFrames, submitSystemIntent } from "../lib/systemFeedRegistry";
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
  const [errorDismissed, setErrorDismissed] = useState(false);
  const [decideError, setDecideError] = useState("");
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
      setErrorDismissed(false);
      setFresh(true);
    } catch (caught) {
      if (version !== requestVersion.current) return;
      setFresh(false);
      setError(caught instanceof Error ? caught.message : String(caught));
      setErrorDismissed(false);
    }
  }, []);

  useEffect(() => {
    const refresh = () => void load();
    void Promise.resolve().then(refresh);
    const onDeepLink = () => refresh();
    window.addEventListener("better-agent:deep-link", onDeepLink);
    const off = eventBus.subscribe("marketplace_bridge_changed", refresh);
    // ADR 0011 §5 `marketplace_bridge`/`marketplace_intents` feeds —
    // additional live-refresh trigger alongside the legacy
    // `marketplace_bridge_changed` ping above (dual SIGNAL; the rendered
    // `MarketplaceBridgeSnapshot` — with its `revision`/id-pattern/
    // `public_key` validation — stays legacy REST-sourced: v2's
    // `PairedDevice` has no `public_key` field at all by design (ADR 0011
    // §5: device secrets never leave `device_ref`), so a full read
    // cutover would need a reshape this pass doesn't attempt for a
    // component this deeply coupled to the legacy validator's exact
    // shape). Mutations (`decide`/`revoke` below) DO route over the v2
    // transport when open — this trigger is what converges the legacy
    // snapshot after a native mutation's async acceptance.
    const offV2 = subscribeSystemFrames((frame) => {
      if (frame.type === "marketplace_bridge_state_changed" || frame.type === "marketplace_intent_upsert") {
        refresh();
      }
    });
    return () => {
      window.removeEventListener("better-agent:deep-link", onDeepLink);
      off();
      offV2();
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
  const globalError = awaiting || selectedDevice || errorDismissed ? "" : error;
  // A failed fetch leaves `snapshot` holding the last good response. Rendering
  // it as current state would claim a live connection we cannot verify, so the
  // cards stay hidden until a load succeeds — dismissing the banner silences
  // the message, it does not make the stale snapshot trustworthy again.
  const stale = Boolean(error);

  const decide = useCallback(async (intent: MarketplaceIntent, decision: "approve" | "reject") => {
    if (!fresh) return;
    const version = ++requestVersion.current;
    setBusyIntentId(intent.intent_id);
    setFresh(false);
    setDecideError("");
    // Native-when-open (ADR 0011 §5's `decide_marketplace_intent`, same
    // backend mutation `marketplace_bridge.bridge.approve/reject` the REST
    // route calls) — the ack carries no snapshot (fire-and-forget), so a
    // successful accept re-pulls the legacy REST snapshot rather than
    // guessing the new state, same as the REST branch below already does
    // on error.
    const native = submitSystemIntent({
      kind: "decide_marketplace_intent",
      intent_id_ref: intent.intent_id,
      decision: decision === "approve" ? "approve" : "reject",
    });
    if (native) {
      try {
        const ack = await native;
        if (version !== requestVersion.current) return;
        if (ack.type === "intent_rejected") {
          setDecideError(ack.message);
          await load();
          return;
        }
        await load();
      } finally {
        setBusyIntentId(null);
      }
      return;
    }
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
      setDecideError(caught instanceof Error ? caught.message : String(caught));
      // Re-establish a trustworthy snapshot; without it `fresh` stays false and
      // both modal buttons are inert with no way back.
      await load();
    } finally {
      setBusyIntentId(null);
    }
  }, [fresh, load]);

  const revoke = useCallback(async (device: MarketplaceDevice) => {
    if (!canRevoke) return;
    const version = ++requestVersion.current;
    setBusyDeviceId(device.device_id);
    setFresh(false);
    setRevokeError("");
    // Native-when-open (ADR 0011 §5's `revoke_marketplace_device` ->
    // `marketplace_bridge.bridge.revoke`, same function the REST route
    // calls). `device_ref` is the v2 opaque id `PairedDeviceWire` uses in
    // place of the legacy `device_id` — the SAME device, different id
    // spelling on this transport (both name the same paired-device
    // record; the mutation function itself is transport-agnostic).
    const native = submitSystemIntent({
      kind: "revoke_marketplace_device",
      device_ref: device.device_id,
    });
    if (native) {
      try {
        const ack = await native;
        if (version !== requestVersion.current) return;
        if (ack.type === "intent_rejected") {
          setRevokeError(ack.message);
          await load();
          return;
        }
        setSelectedDeviceId(null);
        await load();
      } finally {
        setBusyDeviceId((current) => current === device.device_id ? null : current);
      }
      return;
    }
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
          error={decideError || error}
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
        || (!stale && (connectionNotice
          || snapshot?.revocation_pending
          || (snapshot?.paired_devices.length ?? 0) > 0
          || visibleStatuses.length > 0))) ? (
        <aside className="marketplace-bridge-statuses" aria-live="polite">
          {globalError ? (
            <article className="marketplace-bridge-status marketplace-bridge-status--failed" role="alert">
              <span className="marketplace-bridge-status__pulse" aria-hidden="true" />
              <div>
                <strong>{t("marketplaceBridge.connectionFailed")}</strong>
                <span className="marketplace-bridge-status__error">{globalError}</span>
              </div>
              <button
                type="button"
                className="marketplace-bridge-status__dismiss"
                onClick={() => setErrorDismissed(true)}
                aria-label={t("userRequest.dismiss")}
              >
                ×
              </button>
            </article>
          ) : null}
          {!stale && connectionNotice ? (
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
          {!stale && snapshot?.revocation_pending ? (
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
          {!stale && !snapshot?.revocation_pending
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
          {(stale ? [] : visibleStatuses).map((intent) => (
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
                  className="marketplace-bridge-status__dismiss"
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
