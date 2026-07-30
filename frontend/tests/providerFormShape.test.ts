import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import {
  modesForKind,
  availableModesForForm,
  apiEnvCopyForKind,
  showConfigDirForKind,
} from "../src/components/providerFormShape";
import { runtimeKindForRunner } from "../src/components/modelPicker";

const providerFormSource = readFileSync("src/components/ProviderForm.tsx", "utf8");
const settingsPageSource = readFileSync("src/components/SettingsPage.tsx", "utf8");
const wizardSource = readFileSync("src/components/RuntimeProfileWizard.tsx", "utf8");
const modelPickerSource = readFileSync("src/components/modelPicker.ts", "utf8");
const englishSource = readFileSync("src/i18n/en.json", "utf8");

describe("modesForKind", () => {
  it("restricts openai to api_key", () => {
    expect(modesForKind("openai")).toEqual(["api_key"]);
  });
  it("offers both modes for claude/codex/unknown", () => {
    expect(modesForKind("claude")).toEqual(["subscription", "api_key"]);
    expect(modesForKind("codex")).toEqual(["subscription", "api_key"]);
    expect(modesForKind("whatever")).toEqual(["subscription", "api_key"]);
  });
});

describe("availableModesForForm", () => {
  it("create only offers the kind's valid modes", () => {
    expect(availableModesForForm("openai", "create", "api_key")).toEqual(["api_key"]);
  });
  it("edit preserves a now-invalid persisted mode (no silent rewrite)", () => {
    // legacy openai record saved as subscription must still be visible/selectable
    expect(availableModesForForm("openai", "edit", "subscription")).toEqual([
      "api_key",
      "subscription",
    ]);
  });
  it("edit of a valid record does not duplicate", () => {
    expect(availableModesForForm("openai", "edit", "api_key")).toEqual(["api_key"]);
    expect(availableModesForForm("claude", "edit", "subscription")).toEqual([
      "subscription",
      "api_key",
    ]);
  });
});

describe("apiEnvCopyForKind", () => {
  it("uses OPENAI_* labels + placeholder for openai", () => {
    expect(apiEnvCopyForKind("openai")).toEqual({
      keyLabelKey: "setup.apiKeyLabelOpenai",
      urlLabelKey: "setup.baseUrlLabelOpenai",
      keyPlaceholderKey: "setup.apiKeyPlaceholderEmptyOpenai",
    });
  });
  it("uses ANTHROPIC_* labels for claude-env kinds", () => {
    expect(apiEnvCopyForKind("claude").keyLabelKey).toBe("setup.apiKeyLabel");
    expect(apiEnvCopyForKind("codex").urlLabelKey).toBe("setup.baseUrlLabel");
  });
});

describe("showConfigDirForKind", () => {
  it("hides config_dir for in-process openai, shows for others", () => {
    expect(showConfigDirForKind("openai")).toBe(false);
    expect(showConfigDirForKind("claude")).toBe(true);
    expect(showConfigDirForKind("codex")).toBe(true);
  });
});

describe("runner selection lives on runtime profiles, not the provider form", () => {
  it("keeps the provider form auth-shaped (no runner/model/effort inputs)", () => {
    // Templates still SEED profile defaults (default_model/effort strings),
    // but the form renders no runner/model/effort controls.
    expect(providerFormSource).not.toContain("runnerOptionsForKind");
    expect(providerFormSource).not.toContain("setup.runnerLabel");
    expect(providerFormSource).not.toContain("setup.defaultModelLabel");
    expect(providerFormSource).not.toContain("setup.defaultReasoningEffortLabel");
    expect(providerFormSource).not.toContain("useProviderModelCatalog");
  });

  it("drives wizard runner cards from the backend-derived runner_options", () => {
    expect(wizardSource).toContain("provider?.runner_options");
  });

  it("keeps runtime-kind resolution for better_agent_runner in modelPicker", () => {
    expect(modelPickerSource).toContain('if (runner === "better_agent_runner" && providerKind !== "claude")');
    expect(runtimeKindForRunner("claude", "native")).toBe("claude");
    expect(runtimeKindForRunner("claude", "better_agent_runner")).toBe("claude");
    // regression: every other kind's better_agent_runner still collapses to openai
    expect(runtimeKindForRunner("fugu", "better_agent_runner")).toBe("openai");
    expect(runtimeKindForRunner("openai", "better_agent_runner")).toBe("openai");
  });
});

describe("provider-level default surface is gone", () => {
  it("has no set-default endpoint references left", () => {
    expect(settingsPageSource).not.toContain("set-default");
    expect(settingsPageSource).not.toContain("setup.setDefaultButton");
  });
  it("activation goes through the runtime-profile activate route", () => {
    const editorSource = readFileSync("src/components/RuntimeProfilesEditor.tsx", "utf8");
    expect(editorSource).toContain("activateRuntimeProfile");
  });
});

describe("Meta Muse Spark template", () => {
  it("uses the OpenAI-compatible Better Agent runner defaults", () => {
    expect(providerFormSource).toContain('id: "meta-muse"');
    expect(providerFormSource).toContain('base_url: "https://api.meta.ai/v1"');
    expect(providerFormSource).toContain('default_model: "muse-spark-1.1"');
    expect(providerFormSource).toContain('kind: "openai"');
  });
});

describe("Hetzner Inference template", () => {
  it("uses the OpenAI-compatible Better Agent defaults and localized copy", () => {
    expect(providerFormSource).toContain('id: "hetzner"');
    expect(providerFormSource).toContain('base_url: "https://inference.hetzner.com/api/v1"');
    expect(providerFormSource).toContain('default_model: "Qwen/Qwen3.6-35B-A3B-FP8"');
    expect(providerFormSource).toContain('kind: "openai"');
    expect(providerFormSource).toContain(
      'hetzner: { labelKey: "setup.templateHetznerLabel", blurbKey: "setup.templateHetznerBlurb" }',
    );
    expect(englishSource).toContain('"setup.templateHetznerLabel": "Hetzner Inference"');
    expect(englishSource).toContain('"setup.templateHetznerBlurb"');
  });
});
