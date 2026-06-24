export const PROVIDER_CONFIG_SYNC_PATH = "/provider-config-sync";

export function providerConfigSyncUrl(baseUrl = ""): string {
  return `${baseUrl.replace(/\/+$/, "")}${PROVIDER_CONFIG_SYNC_PATH}`;
}

export function openProviderConfigSyncPage(baseUrl = ""): void {
  window.open(providerConfigSyncUrl(baseUrl), "_blank", "noopener,noreferrer");
}
