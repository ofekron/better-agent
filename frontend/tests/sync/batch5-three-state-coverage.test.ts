import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const source = (path: string) => readFileSync(`${process.cwd()}/${path}`, "utf8");

describe("batch 5 three-state coverage", () => {
  it("routes SettingsPage mutations through its canonical busy action", () => {
    const text = source("src/components/SettingsPage.tsx");
    const busyAction = text.slice(text.indexOf("async function runBusyAction"), text.indexOf("interface Props"));
    expect(busyAction).toContain("runThreeStateSync({");
    expect(busyAction).toContain("reconcile:");
    expect(text).toContain("onActivate={(p) => runBusyAction");
    expect(text).toContain("onSubmit={(payload) => runBusyAction");
    expect(text).toContain("onNetworkBindChange={(address) => runBusyAction");
  });

  it.each([
    ["src/components/AppearanceSetting.tsx", "appearance:save"],
    ["src/components/AutoRestartOnIdleSetting.tsx", "autoRestart:save"],
    ["src/components/ContextStrategySetting.tsx", "contextStrategy:save"],
    ["src/components/CrossSessionDelegateSetting.tsx", "xsessionDelegate:save"],
    ["src/components/DelegateTaskPolicySetting.tsx", "delegateTaskPolicy:save"],
    ["src/components/InternalLLMSetting.tsx", "internalLlm:save:"],
    ["src/components/LanguageSelector.tsx", "language:save"],
    ["src/components/PasswordManagerSetting.tsx", "passwordManager:store"],
    ["src/components/PasswordManagerSetting.tsx", "passwordManager:delete:"],
    ["src/components/SessionAutoDeleteSetting.tsx", "sessionAutoDelete:save"],
    ["src/components/ShortcutSettings.tsx", "shortcuts:save"],
    ["src/components/UserDisplayNameSetting.tsx", "userDisplayName:save"],
    ["src/components/VoiceSettings.tsx", "voice:save"],
  ])("routes %s %s through the canonical controller", (path, operationId) => {
    const text = source(path);
    const operation = text.indexOf(operationId);
    expect(operation).toBeGreaterThan(-1);
    expect(text.slice(Math.max(0, operation - 700), operation + 2400)).toContain("runThreeStateSync");
    expect(text.slice(operation, operation + 2400)).toContain("reconcile:");
  });
});
