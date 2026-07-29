import path from "node:path";

export const WEB_PLATFORM_ALIAS_TARGETS = {
  "@capacitor-community/speech-recognition": "speech-recognition.ts",
  "@capacitor/app": "capacitor-app.ts",
  "@capacitor/browser": "capacitor-browser.ts",
  "@capacitor/core": "capacitor-core.ts",
  "@capacitor/filesystem": "capacitor-filesystem.ts",
  "@capacitor/preferences": "capacitor-preferences.ts",
  "@capacitor/push-notifications": "push-notifications.ts",
  "@capgo/capacitor-updater": "capacitor-updater.ts",
  "send-intent": "send-intent.ts",
} as const;

export function createWebPlatformAliases(frontendRoot: string): Record<string, string> {
  return Object.fromEntries(
    Object.entries(WEB_PLATFORM_ALIAS_TARGETS).map(([moduleName, fileName]) => [
      moduleName,
      path.resolve(frontendRoot, "src/platform/web", fileName),
    ]),
  );
}
