import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import {
  modesForKind,
  availableModesForForm,
  apiEnvCopyForKind,
  showConfigDirForKind,
} from "../src/components/providerFormShape";

const settingsPageSource = readFileSync("src/components/SettingsPage.tsx", "utf8");

describe("modesForKind", () => {
  it("restricts openai and gemini to api_key", () => {
    expect(modesForKind("openai")).toEqual(["api_key"]);
    expect(modesForKind("gemini")).toEqual(["api_key"]);
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
    expect(settingsPageSource).toContain('if (kind === "fugu") return ["native", "better_agent_runner"]');
  });

  it("routes Better Agent runner form behavior through openai semantics", () => {
    expect(settingsPageSource).toContain('runner === "better_agent_runner" ? "openai" : kind');
    expect(settingsPageSource).toContain('setMode("api_key")');
    expect(settingsPageSource).toContain('SAKANA_FUGU_API_BASE_URL');
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
