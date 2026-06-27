/** Opens the Settings page in a dedicated browser window. The opened window
 *  detects `?settings_window=1` at App startup and renders SettingsWindow
 *  (chrome-less) instead of the workspace. Reuses the same origin+path so the
 *  auth cookie carries over. */
export function openSettingsWindow(): void {
  const url = `${window.location.origin}${window.location.pathname}?settings_window=1`;
  window.open(
    url,
    "better-agent-settings",
    "width=1100,height=820,resizable=yes,scrollbars=yes",
  );
}
