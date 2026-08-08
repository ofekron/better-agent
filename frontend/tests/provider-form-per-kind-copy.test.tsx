// Closure 4 (form-copy regression): proves the per-kind api-key copy
// (openai-flavored env-var labels/placeholder) and config-dir
// placeholder/hint text — lost when `form_schema` had one generic
// `label_key` per field — now render correctly end to end through
// `ProviderForm.tsx`, sourced from `FormFieldWire.placeholder_key`/
// `.hint_key` (backend `provider_adapter.py`'s `_form_schema_for`).

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import "../src/i18n";
import { ProviderForm } from "../src/components/ProviderForm";
import type { FormFieldWire } from "../src/adapter/wire";

function baseInitial(kind: string) {
  return {
    name: "", nickname: "", kind, mode: "api_key" as const,
    base_url: "", config_dir: "", api_key: "",
  };
}

describe("ProviderForm — per-kind form copy (closure 4)", () => {
  it("openai kind shows OPENAI_API_KEY / OPENAI_BASE_URL labels and the openai empty-key placeholder", () => {
    const formSchema: FormFieldWire[] = [
      { name: "name", kind: "text", label_key: "setup.nameLabel", required: true, choices: [], default: null, pattern: null, max_length: null, placeholder_key: null, hint_key: null },
      { name: "api_key", kind: "secret", label_key: "setup.apiKeyLabelOpenai", required: false, choices: [], default: null, pattern: null, max_length: null, placeholder_key: "setup.apiKeyPlaceholderEmptyOpenai", hint_key: null },
      { name: "base_url", kind: "text", label_key: "setup.baseUrlLabelOpenai", required: false, choices: [], default: null, pattern: null, max_length: null, placeholder_key: null, hint_key: null },
    ];
    render(
      <ProviderForm
        mode="create"
        initial={baseInitial("openai")}
        initialHasKey={false}
        formSchema={formSchema}
        onClose={() => {}}
        onBack={() => {}}
        onSubmit={async () => {}}
      />,
    );
    expect(screen.getByLabelText("OPENAI_API_KEY")).toBeTruthy();
    expect(screen.getByPlaceholderText("sk-... or provider key")).toBeTruthy();
    expect(screen.getByText("OPENAI_BASE_URL")).toBeTruthy();
    // No config_dir field for openai (openai runs in-process).
    expect(screen.queryByText("Codex config directory")).toBeNull();
  });

  it("codex kind shows the generic ANTHROPIC_* api-key copy and codex-flavored config-dir placeholder/hint", () => {
    const formSchema: FormFieldWire[] = [
      { name: "name", kind: "text", label_key: "setup.nameLabel", required: true, choices: [], default: null, pattern: null, max_length: null, placeholder_key: null, hint_key: null },
      { name: "api_key", kind: "secret", label_key: "setup.apiKeyLabel", required: false, choices: [], default: null, pattern: null, max_length: null, placeholder_key: "setup.apiKeyPlaceholderEmpty", hint_key: null },
      { name: "base_url", kind: "text", label_key: "setup.baseUrlLabel", required: false, choices: [], default: null, pattern: null, max_length: null, placeholder_key: null, hint_key: null },
      { name: "config_dir", kind: "path", label_key: "setup.configDirLabelCodex", required: false, choices: [], default: null, pattern: null, max_length: null, placeholder_key: "setup.configDirPlaceholderCodex", hint_key: "setup.configDirHintCodex" },
    ];
    render(
      <ProviderForm
        mode="create"
        initial={baseInitial("codex")}
        initialHasKey={false}
        formSchema={formSchema}
        onClose={() => {}}
        onBack={() => {}}
        onSubmit={async () => {}}
      />,
    );
    expect(screen.getByLabelText("ANTHROPIC_API_KEY")).toBeTruthy();
    expect(screen.getByPlaceholderText("sk-ant-... or provider key")).toBeTruthy();
    expect(screen.getByText("Codex config directory")).toBeTruthy();
    expect(screen.getByPlaceholderText("~/.codex (default)")).toBeTruthy();
    expect(screen.getByText(/Codex config \/ credentials directory for Provider Config Sync/)).toBeTruthy();
  });
});
