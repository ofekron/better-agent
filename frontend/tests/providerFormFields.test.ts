// Replaces the deleted providerFormShape.test.ts — providerFormShape.ts
// itself is deleted (Package D, ADR 0007: SettingsPage/ProviderForm are
// descriptor-driven now, sourced from the backend's `GET
// /api/v2/surface/providers/installable`, not a static frontend table).
// Tests the pure helpers that replaced it, `modesFromSchema`/
// `schemaHasField` (src/components/ProviderForm.tsx) and
// `formSchemaForProviderKind`/`templateFromDescriptor`
// (src/hooks/useInstallableProviders.ts), plus the still-applicable
// sub-assertions carried over from the deleted file.

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fieldHintKey, fieldLabelKey, fieldPlaceholderKey, modesFromSchema, schemaHasField } from "../src/components/providerFormFields";
import {
  formSchemaForProviderKind,
  templateFromDescriptor,
} from "../src/hooks/useInstallableProviders";
import type { FormFieldWire, InstallableDescriptorWire } from "../src/adapter/wire";
import { runtimeKindForRunner } from "../src/components/modelPicker";

const wizardSource = readFileSync("src/components/RuntimeProfileWizard.tsx", "utf8");
const providerFormSource = readFileSync("src/components/ProviderForm.tsx", "utf8");
const modelPickerSource = readFileSync("src/components/modelPicker.ts", "utf8");
const settingsPageSource = readFileSync("src/components/SettingsPage.tsx", "utf8");

function field(over: Partial<FormFieldWire> & Pick<FormFieldWire, "name" | "kind">): FormFieldWire {
  return {
    label_key: "",
    required: false,
    choices: [],
    default: null,
    pattern: null,
    max_length: null,
    placeholder_key: null,
    hint_key: null,
    ...over,
  };
}

describe("modesFromSchema", () => {
  it("restricts to the schema's single mode when there is no mode field (e.g. openai)", () => {
    expect(modesFromSchema([], "create", "api_key")).toEqual(["api_key"]);
  });
  it("offers both modes when the schema has a multi-choice mode field", () => {
    const schema = [field({ name: "mode", kind: "enum", choices: ["subscription", "api_key"] })];
    expect(modesFromSchema(schema, "create", "subscription")).toEqual(["subscription", "api_key"]);
  });
  it("edit preserves a now-invalid persisted mode (no silent rewrite)", () => {
    // legacy openai-shaped record saved as subscription must still be
    // visible/selectable even though the current schema only offers api_key.
    const schema = [field({ name: "mode", kind: "enum", choices: ["api_key"] })];
    expect(modesFromSchema(schema, "edit", "subscription")).toEqual(["api_key", "subscription"]);
  });
  it("edit of a valid record does not duplicate", () => {
    expect(modesFromSchema([], "edit", "api_key")).toEqual(["api_key"]);
  });
});

describe("fieldPlaceholderKey / fieldHintKey (closure 4: form-copy regression)", () => {
  it("resolves the schema's per-kind placeholder_key/hint_key when present", () => {
    const schema = [
      field({
        name: "config_dir", kind: "path", label_key: "setup.configDirLabelCodex",
        placeholder_key: "setup.configDirPlaceholderCodex", hint_key: "setup.configDirHintCodex",
      }),
    ];
    expect(fieldLabelKey(schema, "config_dir", "fallback-label")).toBe("setup.configDirLabelCodex");
    expect(fieldPlaceholderKey(schema, "config_dir", "fallback-placeholder")).toBe("setup.configDirPlaceholderCodex");
    expect(fieldHintKey(schema, "config_dir", "fallback-hint")).toBe("setup.configDirHintCodex");
  });

  it("falls back when the field is absent from the schema (never fabricates)", () => {
    expect(fieldPlaceholderKey([], "config_dir", "fallback-placeholder")).toBe("fallback-placeholder");
    expect(fieldHintKey([], "config_dir", "fallback-hint")).toBe("fallback-hint");
    expect(fieldHintKey([], "config_dir", null)).toBeNull();
  });

  it("falls back when the field is present but its placeholder_key/hint_key is null", () => {
    // e.g. base_url — has a label_key but no per-kind placeholder/hint copy.
    const schema = [field({ name: "base_url", kind: "text", label_key: "setup.baseUrlLabel" })];
    expect(fieldPlaceholderKey(schema, "base_url", "fallback")).toBe("fallback");
    expect(fieldHintKey(schema, "base_url", null)).toBeNull();
  });
});

describe("schemaHasField", () => {
  it("reflects field presence directly (config_dir hidden for in-process kinds like openai)", () => {
    const withConfigDir = [field({ name: "config_dir", kind: "path" })];
    expect(schemaHasField(withConfigDir, "config_dir")).toBe(true);
    expect(schemaHasField([], "config_dir")).toBe(false);
  });
});

function descriptor(over: Partial<InstallableDescriptorWire>): InstallableDescriptorWire {
  return {
    kind: "custom",
    display: { label: "Custom", icon_id: "claude", config_copy_key: "provider.config_copy.claude" },
    form_schema: [],
    defaults: { name: "Custom", kind: "claude", mode: "api_key", base_url: "", config_dir: "", default_model: "", default_reasoning_effort: "" },
    auth_flows: ["api_key"],
    ...over,
  };
}

describe("templateFromDescriptor", () => {
  it("maps the backend descriptor onto the Template shape ProviderForm/TemplateGrid consume", () => {
    const d = descriptor({
      kind: "meta-muse",
      display: { label: "Meta Muse Spark", icon_id: "openai", config_copy_key: "provider.config_copy.openai" },
      defaults: {
        name: "Meta Muse Spark", kind: "openai", mode: "api_key",
        base_url: "https://api.meta.ai/v1", config_dir: "", default_model: "muse-spark-1.1",
        default_reasoning_effort: "",
      },
    });
    const tpl = templateFromDescriptor(d);
    expect(tpl.id).toBe("meta-muse");
    expect(tpl.defaults.kind).toBe("openai");
    expect(tpl.defaults.base_url).toBe("https://api.meta.ai/v1");
    expect(tpl.defaults.default_model).toBe("muse-spark-1.1");
    expect(tpl.formSchema).toBe(d.form_schema);
  });
});

describe("formSchemaForProviderKind", () => {
  it("resolves an edit-mode form's schema via any template sharing the underlying kind", () => {
    const schema = [field({ name: "api_key", kind: "secret" })];
    const templates = [
      templateFromDescriptor(descriptor({ kind: "zai", form_schema: schema, defaults: { name: "Z.AI", kind: "claude", mode: "api_key", base_url: "", config_dir: "", default_model: "", default_reasoning_effort: "" } })),
    ];
    expect(formSchemaForProviderKind(templates, "claude")).toBe(schema);
  });
  it("returns an empty array (never throws) when no installed template shares the kind", () => {
    expect(formSchemaForProviderKind([], "unknown-kind")).toEqual([]);
  });
});

describe("runner selection lives on runtime profiles, not the provider form", () => {
  it("keeps the provider form auth-shaped (no runner/model/effort inputs)", () => {
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

describe("installable catalog is backend-owned, not a static frontend table", () => {
  it("no static TEMPLATES/per-kind mode-restriction table lives in the frontend anymore", () => {
    expect(providerFormSource).not.toContain("export const TEMPLATES");
    expect(providerFormSource).not.toContain("PROVIDER_MODES");
    expect(providerFormSource).not.toContain("export function configDirCopyForKind");
    expect(providerFormSource).not.toContain("export function availableModesForForm");
    expect(providerFormSource).not.toContain("export function apiEnvCopyForKind");
  });
  it("TemplateGrid/ProviderForm/SettingsPage source the catalog from useInstallableProviders", () => {
    expect(settingsPageSource).toContain("useInstallableProviders");
    expect(wizardSource).toContain("useInstallableProviders");
  });
});

// B7 ruling (parity audit): dev's deleted providerFormShape.test.ts pinned
// the Meta Muse Spark / Hetzner templates' literal field values (now
// backend-owned, see test_adapter_provider.py's own pins) AND their
// frontend-only presentation copy — `_TEMPLATE_COPY_KEYS`' labelKey/
// blurbKey mapping (the backend descriptor carries no i18n key, only a raw
// label, see ProviderForm.tsx's module docstring) plus the actual en.json
// strings those keys resolve to. That half never moved to the backend, so
// it stays pinned here.
describe("Meta Muse Spark / Hetzner template presentation copy (frontend-owned half)", () => {
  const englishSource = readFileSync("src/i18n/en.json", "utf8");

  it("meta-muse maps to its labelKey/blurbKey and both resolve in en.json", () => {
    expect(providerFormSource).toContain(
      '"meta-muse": { labelKey: "setup.templateMetaMuseLabel", blurbKey: "setup.templateMetaMuseBlurb" }',
    );
    expect(englishSource).toContain('"setup.templateMetaMuseLabel"');
    expect(englishSource).toContain('"setup.templateMetaMuseBlurb"');
  });

  it("hetzner maps to its labelKey/blurbKey and both resolve in en.json", () => {
    expect(providerFormSource).toContain(
      'hetzner: { labelKey: "setup.templateHetznerLabel", blurbKey: "setup.templateHetznerBlurb" }',
    );
    expect(englishSource).toContain('"setup.templateHetznerLabel": "Hetzner Inference"');
    expect(englishSource).toContain('"setup.templateHetznerBlurb"');
  });
});
