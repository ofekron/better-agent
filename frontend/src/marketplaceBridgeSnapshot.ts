export type MarketplaceIntentStatus =
  | "pending" | "leased" | "awaiting_confirmation" | "committing"
  | "redeemed" | "succeeded" | "rejected" | "failed" | "failed_unknown"
  | "expired" | "cancelled";

export interface MarketplaceIntent {
  intent_id: string;
  action: "pair" | "install" | "enable" | "disable" | "update" | "uninstall";
  status: MarketplaceIntentStatus;
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

export interface MarketplaceDevice {
  device_id: string;
  label: string;
  public_key: string;
}

export interface MarketplaceBridgeSnapshot {
  revision: number;
  connection_state: "unpaired" | "connecting" | "connected" | "offline";
  revocation_pending: boolean;
  paired_devices: MarketplaceDevice[];
  intents: MarketplaceIntent[];
}

const ACTIONS = new Set<MarketplaceIntent["action"]>([
  "pair", "install", "enable", "disable", "update", "uninstall",
]);
const PAIR_STATUSES = new Set<MarketplaceIntentStatus>([
  "pending", "awaiting_confirmation", "redeemed", "rejected", "cancelled", "expired",
]);
const ACTION_STATUSES = new Set<MarketplaceIntentStatus>([
  "pending", "leased", "awaiting_confirmation", "committing", "succeeded",
  "rejected", "failed", "failed_unknown", "expired", "cancelled",
]);
const DEVICE_ID_PATTERN = /^badvc_[a-f0-9]{32}$/;
const PAIR_INTENT_ID_PATTERN = /^pair_[a-f0-9]{32}$/;
const ACTION_INTENT_ID_PATTERN = /^baact_[a-f0-9]{32}$/;
const PUBLIC_KEY_PATTERN = /^[A-Za-z0-9_-]{43}$/;

function boundedString(value: unknown, minimum: number, maximum: number): value is string {
  if (typeof value !== "string") return false;
  const length = Array.from(value).length;
  return length >= minimum && length <= maximum;
}

export function marketplaceBridgeSnapshotFrom(value: unknown): MarketplaceBridgeSnapshot {
  if (!value || typeof value !== "object") throw new Error("invalid marketplace snapshot");
  const item = value as Record<string, unknown>;
  if (
    typeof item.revision !== "number"
    || !Number.isSafeInteger(item.revision)
    || item.revision < 0
    || !["unpaired", "connecting", "connected", "offline"].includes(
      String(item.connection_state),
    )
    || typeof item.revocation_pending !== "boolean"
    || !Array.isArray(item.paired_devices)
    || item.paired_devices.length > 1
    || !Array.isArray(item.intents)
  ) {
    throw new Error("invalid marketplace snapshot");
  }
  for (const device of item.paired_devices) {
    if (!device || typeof device !== "object") {
      throw new Error("invalid marketplace device");
    }
    const row = device as Record<string, unknown>;
    if (
      typeof row.device_id !== "string"
      || !DEVICE_ID_PATTERN.test(row.device_id)
      || !boundedString(row.label, 1, 500)
      || typeof row.public_key !== "string"
      || !PUBLIC_KEY_PATTERN.test(row.public_key)
    ) {
      throw new Error("invalid marketplace device");
    }
  }
  for (const intent of item.intents) {
    if (!intent || typeof intent !== "object") throw new Error("invalid marketplace intent");
    const row = intent as Record<string, unknown>;
    if (
      typeof row.intent_id !== "string"
      || !ACTIONS.has(row.action as MarketplaceIntent["action"])
      || (
        row.action === "pair"
          ? !PAIR_STATUSES.has(row.status as MarketplaceIntentStatus)
          : !ACTION_STATUSES.has(row.status as MarketplaceIntentStatus)
      )
      || (
        row.action === "pair"
          ? !PAIR_INTENT_ID_PATTERN.test(row.intent_id)
          : !ACTION_INTENT_ID_PATTERN.test(row.intent_id)
      )
    ) {
      throw new Error("invalid marketplace intent");
    }
    const bounded = (field: unknown, required = false) => (
      boundedString(field, required ? 1 : 0, 500)
      && (!required || field.trim().length > 0)
    );
    if (row.error !== undefined && !bounded(row.error)) {
      throw new Error("invalid marketplace intent");
    }
    if (row.action === "pair") {
      for (const field of ["site_label", "account_label", "device_label"] as const) {
        if (row[field] !== undefined && !bounded(row[field], true)) {
          throw new Error("invalid marketplace pair intent");
        }
      }
      if (
        row.status === "awaiting_confirmation"
        && (
          !bounded(row.site_label, true)
          || !bounded(row.account_label, true)
          || !bounded(row.device_label, true)
        )
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
