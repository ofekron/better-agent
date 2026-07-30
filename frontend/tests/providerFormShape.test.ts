import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import {
  modesForKind,
  availableModesForForm,
  apiEnvCopyForKind,
  showConfigDirForKind,
} from "../src/components/providerFormShape";
import { runtimeKindForRunner } from "../src/components/modelPicker";

const settingsPageSource = readFileSync("src/components/SettingsPage.tsx", "utf8");
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

describe("Fugu runner selector wiring", () => {
  it("offers both native and Better Agent runners for fugu", () => {
    expect(settingsPageSource).toContain(
      'if (kind === "fugu" || kind === "codex" || kind === "claude") return ["native", "better_agent_runner"]',
    );
  });

  it("routes Better Agent runner form behavior through openai semantics", () => {
    expect(modelPickerSource).toContain('if (runner === "better_agent_runner" && providerKind !== "claude")');
    expect(settingsPageSource).toContain('runtimeKindForRunner(kind, runner)');
    expect(settingsPageSource).toContain('setMode("api_key")');
    expect(settingsPageSource).toContain('SAKANA_FUGU_API_BASE_URL');
  });
});

describe("Claude Better Agent runner (subscription-via-runner_better_agent)", () => {
  it("stays on the real claude runtime kind for both runners", () => {
    expect(runtimeKindForRunner("claude", "native")).toBe("claude");
    expect(runtimeKindForRunner("claude", "better_agent_runner")).toBe("claude");
    // regression: every other kind's better_agent_runner still collapses to openai
    expect(runtimeKindForRunner("fugu", "better_agent_runner")).toBe("openai");
    expect(runtimeKindForRunner("openai", "better_agent_runner")).toBe("openai");
  });

  it("hides api_key mode once better_agent_runner is selected", () => {
    expect(modesForKind("claude")).toEqual(["subscription", "api_key"]);
    expect(modesForKind("claude", "native")).toEqual(["subscription", "api_key"]);
    expect(modesForKind("claude", "better_agent_runner")).toEqual(["subscription"]);
  });

  it("availableModesForForm locks to subscription for better_agent_runner even on edit", () => {
    expect(
      availableModesForForm("claude", "edit", "subscription", "better_agent_runner"),
    ).toEqual(["subscription"]);
    // a legacy record somehow persisted as api_key is still surfaced (no
    // silent rewrite) rather than hidden outright.
    expect(
      availableModesForForm("claude", "edit", "api_key", "better_agent_runner"),
    ).toEqual(["subscription", "api_key"]);
  });

  it("offers claude's better_agent_runner choice without granting it to unrelated kinds", () => {
    expect(settingsPageSource).toContain('runnerOptionsForKind');
    // codex (not in the fugu/claude/openai special cases) stays native-only.
    expect(settingsPageSource).toContain('return kind === "openai" ? ["better_agent_runner"] : ["native"]');
  });
});

describe("Meta Muse Spark template", () => {
  it("uses the OpenAI-compatible Better Agent runner defaults", () => {
    expect(settingsPageSource).toContain('id: "meta-muse"');
    expect(settingsPageSource).toContain('base_url: "https://api.meta.ai/v1"');
    expect(settingsPageSource).toContain('default_model: "muse-spark-1.1"');
    expect(settingsPageSource).toContain('kind: "openai"');
  });
});

describe("Hetzner Inference template", () => {
  it("uses the OpenAI-compatible Better Agent defaults and localized copy", () => {
    expect(settingsPageSource).toContain('id: "hetzner"');
    expect(settingsPageSource).toContain('base_url: "https://inference.hetzner.com/api/v1"');
    expect(settingsPageSource).toContain('default_model: "Qwen/Qwen3.6-35B-A3B-FP8"');
    expect(settingsPageSource).toContain('kind: "openai"');
    expect(settingsPageSource).toContain(
      'hetzner: { labelKey: "setup.templateHetznerLabel", blurbKey: "setup.templateHetznerBlurb" }',
    );
    expect(englishSource).toContain('"setup.templateHetznerLabel": "Hetzner Inference"');
    expect(englishSource).toContain('"setup.templateHetznerBlurb"');
  });
});
