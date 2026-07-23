import type { Provider } from "../types";

const STORAGE_KEY = "better-agent-provider-cache";
const VERSION = 4;
const EFFORTS = new Set(["none", "minimal", "low", "medium", "high", "xhigh"]);

export interface ProviderCache {
  version: typeof VERSION;
  defaultProviderId: string | null;
  providers: Provider[];
  modelsByProvider: Record<string, string[]>;
}

function isProvider(value: unknown): value is Provider {
  if (!value || typeof value !== "object") return false;
  const provider = value as Record<string, unknown>;
  return (
    typeof provider.id === "string"
    && typeof provider.name === "string"
    && typeof provider.kind === "string"
    && (provider.mode === "subscription" || provider.mode === "api_key")
    && typeof provider.base_url === "string"
    && typeof provider.config_dir === "string"
    && Array.isArray(provider.custom_models)
    && provider.custom_models.every((model) => typeof model === "string")
    && typeof provider.default_model === "string"
    && Array.isArray(provider.reasoning_effort_options)
    && provider.reasoning_effort_options.every((effort) => typeof effort === "string" && EFFORTS.has(effort))
    && (provider.default_reasoning_effort === "" || (typeof provider.default_reasoning_effort === "string" && EFFORTS.has(provider.default_reasoning_effort)))
    && (provider.last_model === undefined || typeof provider.last_model === "string")
    && (provider.last_reasoning_effort === undefined || (typeof provider.last_reasoning_effort === "string" && EFFORTS.has(provider.last_reasoning_effort)))
    && typeof provider.has_api_key === "boolean"
    && typeof provider.suspended === "boolean"
    && typeof provider.supports_fork === "boolean"
    && typeof provider.supports_manager_mode === "boolean"
    && typeof provider.supports_rewind === "boolean"
    && typeof provider.supports_steering === "boolean"
    && typeof provider.supports_native_subagents === "boolean"
    && typeof provider.supports_reasoning_effort === "boolean"
  );
}

function parseCache(value: unknown): ProviderCache | null {
  if (!value || typeof value !== "object") return null;
  const cache = value as Record<string, unknown>;
  if (cache.version !== VERSION) return null;
  if (cache.defaultProviderId !== null && typeof cache.defaultProviderId !== "string") return null;
  if (!Array.isArray(cache.providers) || !cache.providers.every(isProvider)) return null;
  if (!cache.modelsByProvider || typeof cache.modelsByProvider !== "object") return null;

  const modelsByProvider: Record<string, string[]> = {};
  for (const [providerId, models] of Object.entries(cache.modelsByProvider)) {
    if (!Array.isArray(models) || !models.every((model) => typeof model === "string")) return null;
    modelsByProvider[providerId] = models;
  }

  return {
    version: VERSION,
    defaultProviderId: cache.defaultProviderId,
    providers: cache.providers,
    modelsByProvider,
  };
}

export interface ProvidersPayload {
  providers: Provider[];
  defaultProviderId: string | null;
}

export function parseProvidersPayload(value: unknown): ProvidersPayload | null {
  if (!value || typeof value !== "object") return null;
  const payload = value as Record<string, unknown>;
  if (payload.default_provider_id !== null && typeof payload.default_provider_id !== "string") return null;
  if (!Array.isArray(payload.providers) || !payload.providers.every(isProvider)) return null;
  return {
    providers: payload.providers,
    defaultProviderId: payload.default_provider_id,
  };
}

export function readProviderCache(): ProviderCache | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? parseCache(JSON.parse(raw)) : null;
  } catch {
    return null;
  }
}

export function providerNameForId(providerId: string | null | undefined): string {
  const id = providerId?.trim();
  if (!id) return "";
  return readProviderCache()?.providers.find((provider) => provider.id === id)?.name ?? "";
}

function writeProviderCache(cache: ProviderCache): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(cache));
  } catch {
    // The cache is disposable; storage failures must not block online provider state.
  }
}

export function cacheProviders(providers: Provider[], defaultProviderId: string | null): void {
  writeProviderCache({
    version: VERSION,
    defaultProviderId,
    providers,
    modelsByProvider: readProviderCache()?.modelsByProvider ?? {},
  });
}

export function cacheProviderModels(providerId: string, models: string[]): void {
  const cached = readProviderCache();
  if (!cached) return;
  writeProviderCache({
    ...cached,
    modelsByProvider: {
      ...cached.modelsByProvider,
      [providerId]: models,
    },
  });
}
