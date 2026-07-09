// Per-kind shape of the New/Edit provider form. Kept pure (no React) so the
// rules are unit-testable and live in one place instead of scattered across
// the form component.

export type ProviderMode = "subscription" | "api_key";

// Auth modes a kind supports. openai (BA-owned agent loop) and gemini
// (subscription auth dropped) are api_key only. pi/cursor/kimi/opencode
// self-auth through their CLI's own login/env keys — no BA-managed api_key
// record, so subscription only. The rest offer both.
const PROVIDER_MODES: Record<string, ProviderMode[]> = {
  openai: ["api_key"],
  gemini: ["api_key"],
  pi: ["subscription"],
  cursor: ["subscription"],
  kimi: ["subscription"],
  opencode: ["subscription"],
};

export function modesForKind(kind: string): ProviderMode[] {
  return PROVIDER_MODES[kind] ?? ["subscription", "api_key"];
}

// Modes the form should offer. On create, only the kind's valid modes. On
// edit, also surface the record's persisted mode even if now-invalid, so the
// form never silently rewrites it — the user sees it and changes it
// explicitly (and the backend rejects an invalid save).
export function availableModesForForm(
  kind: string,
  formMode: "create" | "edit",
  initialMode: ProviderMode,
): ProviderMode[] {
  const base = modesForKind(kind);
  if (formMode === "edit" && !base.includes(initialMode)) {
    return [...base, initialMode];
  }
  return base;
}

// Env-var labels + key placeholder for the api_key fields. openai's runner
// reads OPENAI_API_KEY / OPENAI_BASE_URL; Claude-env kinds use ANTHROPIC_*.
export function apiEnvCopyForKind(kind: string): {
  keyLabelKey: string;
  urlLabelKey: string;
  keyPlaceholderKey: string;
} {
  if (kind === "openai") {
    return {
      keyLabelKey: "setup.apiKeyLabelOpenai",
      urlLabelKey: "setup.baseUrlLabelOpenai",
      keyPlaceholderKey: "setup.apiKeyPlaceholderEmptyOpenai",
    };
  }
  return {
    keyLabelKey: "setup.apiKeyLabel",
    urlLabelKey: "setup.baseUrlLabel",
    keyPlaceholderKey: "setup.apiKeyPlaceholderEmpty",
  };
}

// openai runs in-process — no CLI config/credentials dir, so hide the field.
export function showConfigDirForKind(kind: string): boolean {
  return kind !== "openai";
}
