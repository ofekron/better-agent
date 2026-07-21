const KNOWN_EXTENSION_PERMISSIONS = new Set([
  "session_state",
  "spawn_runs",
  "internal_loopback",
  "filesystem",
  "network",
  "secrets",
  "provider_config",
  "backend_routes",
  "storage",
  "marketplace_auth",
  "mutates_session_fields",
]);

export function extensionPermissionTranslationKey(
  permission: string,
  field: "label" | "risk",
): string {
  const key = KNOWN_EXTENSION_PERMISSIONS.has(permission) ? permission : "unknown";
  return `settings.extensionsPermission.${key}.${field}`;
}
