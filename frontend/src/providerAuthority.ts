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
