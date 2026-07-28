import type { Provider } from "./types";

export function providerAuthority(provider: Provider) {
  return {
    expected_generation: provider.generation,
    expected_revision: provider.revision,
  };
}

export function requireProvider(providers: Provider[], providerId: string) {
  const provider = providers.find((candidate) => candidate.id === providerId);
  if (!provider) throw new Error("provider is unavailable");
  return provider;
}

export function defaultProviderAuthority(
  provider: Provider,
  providers: Provider[],
  activeId: string | null,
) {
  const active = providers.find((candidate) => candidate.id === activeId);
  if (!active) throw new Error("current default provider is unavailable");
  return {
    ...providerAuthority(provider),
    expected_default_provider_id: active.id,
    expected_default_generation: active.generation,
    expected_default_revision: active.revision,
  };
}
