import { Capacitor } from "@capacitor/core";
import {
  CapacitorUpdater,
  type BundleInfo,
} from "@capgo/capacitor-updater";
import { API } from "../api";
import { getStoredToken } from "../bearerAuth";

interface BundleManifest {
  version: string;
  checksum: string;
  download_path: string;
}

type ActivationPhase = "scheduling" | "scheduled";

interface ActivationAttempt {
  version: string;
  id: string;
  sourceCurrentId: string;
  phase: ActivationPhase;
}

interface QuarantinedBundle {
  version: string;
  id: string;
}

interface MobileUpdaterState {
  schema: 1;
  attempt: ActivationAttempt | null;
  quarantined: QuarantinedBundle | null;
}

interface StoredState {
  state: MobileUpdaterState | null;
  valid: boolean;
}

const MANIFEST_FETCH_TIMEOUT_MS = 4_000;
const UPDATE_CHECK_DELAY_MS = 3_000;
const STORAGE_KEY = "better_agent_mobile_ota_state";
const EMPTY_STATE: MobileUpdaterState = {
  schema: 1,
  attempt: null,
  quarantined: null,
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: string[]): boolean {
  const expected = new Set(keys);
  const actual = Object.keys(value);
  return actual.length === expected.size && actual.every((key) => expected.has(key));
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function parseAttempt(value: unknown): ActivationAttempt | null | undefined {
  if (value === null) return null;
  if (!isRecord(value) || !hasExactKeys(value, ["version", "id", "sourceCurrentId", "phase"])) {
    return undefined;
  }
  if (
    !isNonEmptyString(value.version)
    || !isNonEmptyString(value.id)
    || !isNonEmptyString(value.sourceCurrentId)
    || (value.phase !== "scheduling" && value.phase !== "scheduled")
  ) {
    return undefined;
  }
  return {
    version: value.version,
    id: value.id,
    sourceCurrentId: value.sourceCurrentId,
    phase: value.phase,
  };
}

function parseQuarantined(value: unknown): QuarantinedBundle | null | undefined {
  if (value === null) return null;
  if (!isRecord(value) || !hasExactKeys(value, ["version", "id"])) return undefined;
  if (!isNonEmptyString(value.version) || !isNonEmptyString(value.id)) return undefined;
  return { version: value.version, id: value.id };
}

function readState(): StoredState {
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return { state: null, valid: false };
  }
  if (raw === null) return { state: { ...EMPTY_STATE }, valid: true };

  try {
    const value: unknown = JSON.parse(raw);
    if (!isRecord(value) || !hasExactKeys(value, ["schema", "attempt", "quarantined"])) {
      return { state: null, valid: false };
    }
    const attempt = parseAttempt(value.attempt);
    const quarantined = parseQuarantined(value.quarantined);
    if (value.schema !== 1 || attempt === undefined || quarantined === undefined) {
      return { state: null, valid: false };
    }
    return {
      state: { schema: 1, attempt, quarantined },
      valid: true,
    };
  } catch {
    return { state: null, valid: false };
  }
}

function writeState(state: MobileUpdaterState): void {
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

function discardInvalidState(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {}
}

function parseManifest(value: unknown): BundleManifest | null {
  if (!isRecord(value) || !hasExactKeys(value, ["version", "checksum", "download_path"])) {
    return null;
  }
  if (
    !isNonEmptyString(value.version)
    || !isNonEmptyString(value.checksum)
    || !isNonEmptyString(value.download_path)
    || !value.download_path.startsWith("/api/mobile/bundle/download?")
  ) {
    return null;
  }
  return {
    version: value.version,
    checksum: value.checksum,
    download_path: value.download_path,
  };
}

function sameBundle(bundle: BundleInfo | null | undefined, id: string): boolean {
  return bundle?.id === id;
}

function quarantineAttempt(
  state: MobileUpdaterState,
  attempt: ActivationAttempt,
): void {
  writeState({
    ...state,
    attempt: null,
    quarantined: { version: attempt.version, id: attempt.id },
  });
}

async function reconcileRunningBundle(): Promise<boolean> {
  const ready = await CapacitorUpdater.notifyAppReady();
  const stored = readState();
  if (!stored.valid || !stored.state) {
    discardInvalidState();
    return false;
  }

  const [current, next, listed] = await Promise.all([
    CapacitorUpdater.current(),
    CapacitorUpdater.getNextBundle(),
    CapacitorUpdater.list(),
  ]);
  const attempt = stored.state.attempt;
  if (!attempt) return true;

  const currentIsTarget = sameBundle(current.bundle, attempt.id)
    && sameBundle(ready.bundle, attempt.id)
    && current.bundle.status === "success";
  if (currentIsTarget) {
    writeState({
      ...stored.state,
      attempt: null,
      quarantined: stored.state.quarantined?.id === attempt.id
        ? null
        : stored.state.quarantined,
    });
    return true;
  }

  const nextIsTarget = sameBundle(next, attempt.id);
  const listedAsFailed = listed.bundles.some(
    (bundle) => bundle.id === attempt.id && bundle.status === "error",
  );
  if (listedAsFailed) {
    quarantineAttempt(stored.state, attempt);
    return true;
  }

  const failed = await CapacitorUpdater.getFailedUpdate();
  if (sameBundle(failed?.bundle, attempt.id)) {
    quarantineAttempt(stored.state, attempt);
    return true;
  }

  if (attempt.phase === "scheduled" && !nextIsTarget) {
    quarantineAttempt(stored.state, attempt);
    return true;
  }

  if (nextIsTarget) {
    if (attempt.phase === "scheduling") {
      writeState({
        ...stored.state,
        attempt: { ...attempt, phase: "scheduled" },
      });
    }
    return false;
  }

  writeState({ ...stored.state, attempt: null });
  return true;
}

function scheduleDeferredCheck(): void {
  window.setTimeout(() => {
    void runMobileOtaCheck();
  }, UPDATE_CHECK_DELAY_MS);
}

export function initializeMobileUpdater(
  onStartupError: (error: unknown) => void,
): () => void {
  if (!Capacitor.isNativePlatform()) return () => {};

  let reconciliation: Promise<void> | null = null;
  return () => {
    reconciliation ??= reconcileRunningBundle()
      .then((mayCheck) => {
        if (mayCheck) scheduleDeferredCheck();
      })
      .catch(onStartupError);
  };
}

export async function runMobileOtaCheck(): Promise<void> {
  if (!Capacitor.isNativePlatform() || !getStoredToken()) return;

  const stored = readState();
  if (!stored.valid || !stored.state || stored.state.attempt) return;

  try {
    const controller = new AbortController();
    const timeout = window.setTimeout(
      () => controller.abort(new DOMException("bundle manifest fetch timed out", "TimeoutError")),
      MANIFEST_FETCH_TIMEOUT_MS,
    );
    let response: Response;
    try {
      response = await fetch(`${API}/api/mobile/bundle/manifest`, {
        credentials: "include",
        signal: controller.signal,
      });
    } finally {
      window.clearTimeout(timeout);
    }
    if (!response.ok) return;

    const manifest = parseManifest(await response.json());
    if (!manifest || stored.state.quarantined?.version === manifest.version) return;

    const current = await CapacitorUpdater.current();
    if (current.bundle.version === manifest.version) return;

    const bundle = await CapacitorUpdater.download({
      url: `${API}${manifest.download_path}`,
      version: manifest.version,
      checksum: manifest.checksum,
    });
    if (!isNonEmptyString(bundle.id)) return;

    const attempt: ActivationAttempt = {
      version: manifest.version,
      id: bundle.id,
      sourceCurrentId: current.bundle.id,
      phase: "scheduling",
    };
    writeState({ ...stored.state, attempt });
    try {
      await CapacitorUpdater.next({ id: bundle.id });
      writeState({
        ...stored.state,
        attempt: { ...attempt, phase: "scheduled" },
      });
    } catch (error) {
      const next = await CapacitorUpdater.getNextBundle();
      writeState({
        ...stored.state,
        attempt: sameBundle(next, bundle.id)
          ? { ...attempt, phase: "scheduled" }
          : null,
      });
      throw error;
    }
  } catch (error) {
    console.error("mobile OTA check failed", error);
  }
}
