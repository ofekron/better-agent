import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { Provider, ReasoningEffort, Permission } from "../types";
import {
  availableModesForForm,
  apiEnvCopyForKind,
  showConfigDirForKind,
} from "./providerFormShape";
import { Select } from "./Select";
import Icon from "./Icon";

// ---------------------------------------------------------------------------
// Provider ACCOUNT templates + creation/edit form (auth-shaped). Execution
// selection (runner, default model/effort) lives on runtime profiles — see
// RuntimeProfileWizard / RuntimeProfilesEditor.
// ---------------------------------------------------------------------------

export interface Template {
  id: string;
  label: string;
  blurb: string;
  defaults: {
    name: string;
    kind: string;
    mode: Provider["mode"];
    base_url: string;
    config_dir: string;
    /** Seed for the runtime-profile wizard's defaults step — providers no
     * longer carry execution defaults themselves. */
    default_model: string;
    default_reasoning_effort: ReasoningEffort | "";
    api_key?: string;
    suspended?: boolean;
  };
}

export const TEMPLATES = [
  {
    id: "claude",
    label: "Claude",
    blurb: "Anthropic subscription — OAuth via the Claude Code CLI.",
    defaults: {
      name: "Claude",
      kind: "claude",
      mode: "subscription",
      base_url: "",
      config_dir: "",
      default_model: "claude-opus-5[1m]",
      default_reasoning_effort: "medium",
    },
  },
  {
    id: "codex",
    label: "Codex",
    blurb: "OpenAI Codex subscription — uses the Codex CLI with your ChatGPT account.",
    defaults: {
      name: "Codex",
      kind: "codex",
      mode: "subscription",
      base_url: "",
      config_dir: "",
      default_model: "gpt-5.5",
      default_reasoning_effort: "medium",
    },
  },
  {
    id: "agy",
    label: "Antigravity",
    blurb: "Google Antigravity subscription — uses the agy CLI.",
    defaults: {
      name: "Antigravity",
      kind: "agy",
      mode: "subscription",
      base_url: "",
      config_dir: "$HOME/.gemini/antigravity-cli",
      default_model: "Gemini 3.5 Flash (Medium)",
      default_reasoning_effort: "",
    },
  },
  {
    id: "copilot",
    label: "Copilot",
    blurb: "GitHub Copilot subscription — uses the `copilot` CLI, OAuth via `gh auth login`.",
    defaults: {
      name: "Copilot",
      kind: "copilot",
      mode: "subscription",
      base_url: "",
      config_dir: "",
      default_model: "auto",
      default_reasoning_effort: "",
    },
  },
  {
    id: "pi",
    label: "pi",
    blurb: "Minimal open-source coding agent (pi-mono). Bring any Anthropic/OpenAI/Google API key or a ChatGPT/Claude/Copilot subscription via its /login.",
    defaults: {
      name: "pi",
      kind: "pi",
      mode: "subscription",
      base_url: "",
      config_dir: "",
      default_model: "anthropic/claude-opus-4-7",
      default_reasoning_effort: "",
    },
  },
  {
    id: "qwen",
    label: "Qwen Code",
    blurb: "Alibaba's Qwen Code CLI — free Qwen OAuth tier or a DashScope/OpenAI-compatible API key.",
    defaults: {
      name: "Qwen Code",
      kind: "qwen",
      mode: "subscription",
      base_url: "",
      config_dir: "",
      default_model: "coder-model",
      default_reasoning_effort: "",
    },
  },
  {
    id: "cursor",
    label: "Cursor",
    blurb: "Cursor's cursor-agent CLI — headless agent runs with your Cursor subscription (`cursor-agent login`).",
    defaults: {
      name: "Cursor",
      kind: "cursor",
      mode: "subscription",
      base_url: "",
      config_dir: "",
      default_model: "auto",
      default_reasoning_effort: "",
    },
  },
  {
    id: "kimi",
    label: "Kimi CLI",
    blurb: "Moonshot AI's Kimi coding agent (kimi-k2) — sign in with its /login or KIMI_API_KEY.",
    defaults: {
      name: "Kimi",
      kind: "kimi",
      mode: "subscription",
      base_url: "",
      config_dir: "",
      default_model: "kimi-code/kimi-for-coding",
      default_reasoning_effort: "",
    },
  },
  {
    id: "amp",
    label: "Amp",
    blurb: "Sourcegraph's coding agent CLI. Headless (execute) mode needs paid Amp credits; sign in with `amp login`.",
    defaults: {
      name: "Amp",
      kind: "amp",
      mode: "subscription",
      base_url: "",
      config_dir: "",
      default_model: "auto",
      default_reasoning_effort: "",
    },
  },
  {
    id: "opencode",
    label: "OpenCode",
    blurb: "Open-source multi-provider coding agent. Works out of the box with free models; connect providers via `opencode auth login`.",
    defaults: {
      name: "OpenCode",
      kind: "opencode",
      mode: "subscription",
      base_url: "",
      config_dir: "",
      default_model: "opencode/big-pickle",
      default_reasoning_effort: "",
    },
  },
  {
    id: "fugu",
    label: "Sakana Fugu",
    blurb: "Sakana Fugu multi-agent system via the `codex-fugu` launcher. Install it first (sakana.ai/fugu), then add it here.",
    defaults: {
      name: "Fugu",
      kind: "fugu",
      mode: "subscription",
      base_url: "",
      config_dir: "$HOME/.codex",
      default_model: "fugu",
      default_reasoning_effort: "",
    },
  },
  {
    id: "sakana",
    label: "Sakana Fugu (API)",
    blurb: "Sakana Fugu via its native OpenAI-compatible API, driven by Better Agent's own agent loop. Needs an API key.",
    defaults: {
      name: "Sakana Fugu (API)",
      kind: "openai",
      mode: "api_key",
      base_url: "https://api.sakana.ai/v1",
      config_dir: "",
      default_model: "fugu",
      default_reasoning_effort: "",
    },
  },
  {
    id: "meta-muse",
    label: "Meta Muse Spark",
    blurb: "Meta Model API for Muse Spark 1.1, driven by Better Agent's own agent loop. Needs a Meta Model API key.",
    defaults: {
      name: "Meta Muse Spark",
      kind: "openai",
      mode: "api_key",
      base_url: "https://api.meta.ai/v1",
      config_dir: "",
      default_model: "muse-spark-1.1",
      default_reasoning_effort: "",
    },
  },
  {
    id: "ollama",
    label: "Ollama",
    blurb: "Local Anthropic-compatible models via Claude Code.",
    defaults: {
      name: "Ollama",
      kind: "claude",
      mode: "api_key",
      base_url: "http://localhost:11434",
      config_dir: "$HOME/.claude-ollama",
      default_model: "qwen3-coder",
      default_reasoning_effort: "medium",
      api_key: "ollama",
    },
  },
  {
    id: "zai",
    label: "Z.AI (Claude)",
    blurb: "Z.AI's Anthropic-compatible API via the `claude` CLI. Needs an API key.",
    defaults: {
      name: "Z.AI (Claude)",
      kind: "claude",
      mode: "api_key",
      base_url: "https://api.z.ai/api/anthropic",
      config_dir: "~/.claude-zai",
      default_model: "glm-4.6",
      default_reasoning_effort: "medium",
    },
  },
  {
    id: "zai-openai",
    label: "Z.AI (OpenAI)",
    blurb: "Z.AI's native OpenAI endpoint (Coding plan key) driven by Better Agent's own agent loop. This is where Z.AI's automatic prompt caching is reported, so it's cheaper on long contexts. Needs an API key.",
    defaults: {
      name: "Z.AI (OpenAI)",
      kind: "openai",
      mode: "api_key",
      base_url: "https://api.z.ai/api/coding/paas/v4",
      config_dir: "",
      default_model: "glm-5.2",
      default_reasoning_effort: "",
    },
  },
  {
    id: "hetzner",
    label: "Hetzner Inference",
    blurb: "Hetzner's OpenAI-compatible inference endpoint, driven by Better Agent's own agent loop. Free experimental service with no SLA. Needs an API token from experiments.hetzner.com.",
    defaults: {
      name: "Hetzner Inference",
      kind: "openai",
      mode: "api_key",
      base_url: "https://inference.hetzner.com/api/v1",
      config_dir: "",
      default_model: "Qwen/Qwen3.6-35B-A3B-FP8",
      default_reasoning_effort: "",
    },
  },
  {
    id: "custom",
    label: "Custom API",
    blurb: "Any Anthropic-compatible endpoint. Provide URL + key yourself.",
    defaults: {
      name: "Custom API",
      kind: "claude",
      mode: "api_key",
      base_url: "",
      config_dir: "",
      default_model: "",
      default_reasoning_effort: "medium",
    },
  },
  {
    id: "custom-openai",
    label: "Custom OpenAI",
    blurb: "Any OpenAI-compatible endpoint. Driven by Better Agent's own agent loop. Provide URL, key, and model.",
    defaults: {
      name: "Custom OpenAI",
      kind: "openai",
      mode: "api_key",
      base_url: "",
      config_dir: "",
      default_model: "",
      default_reasoning_effort: "",
    },
  },
] as const satisfies readonly Template[];

export type TemplateId = (typeof TEMPLATES)[number]["id"];

const TEMPLATE_KEYS: Record<TemplateId, { labelKey: string; blurbKey: string }> = {
  claude: { labelKey: "setup.templateClaudeLabel", blurbKey: "setup.templateClaudeBlurb" },
  codex: { labelKey: "setup.templateCodexLabel", blurbKey: "setup.templateCodexBlurb" },
  copilot: { labelKey: "setup.templateCopilotLabel", blurbKey: "setup.templateCopilotBlurb" },
  agy: { labelKey: "setup.templateAgyLabel", blurbKey: "setup.templateAgyBlurb" },
  fugu: { labelKey: "setup.templateFuguLabel", blurbKey: "setup.templateFuguBlurb" },
  pi: { labelKey: "setup.templatePiLabel", blurbKey: "setup.templatePiBlurb" },
  qwen: { labelKey: "setup.templateQwenLabel", blurbKey: "setup.templateQwenBlurb" },
  cursor: { labelKey: "setup.templateCursorLabel", blurbKey: "setup.templateCursorBlurb" },
  kimi: { labelKey: "setup.templateKimiLabel", blurbKey: "setup.templateKimiBlurb" },
  amp: { labelKey: "setup.templateAmpLabel", blurbKey: "setup.templateAmpBlurb" },
  opencode: { labelKey: "setup.templateOpencodeLabel", blurbKey: "setup.templateOpencodeBlurb" },
  sakana: { labelKey: "setup.templateSakanaLabel", blurbKey: "setup.templateSakanaBlurb" },
  "meta-muse": { labelKey: "setup.templateMetaMuseLabel", blurbKey: "setup.templateMetaMuseBlurb" },
  ollama: { labelKey: "setup.templateOllamaLabel", blurbKey: "setup.templateOllamaBlurb" },
  zai: { labelKey: "setup.templateZaiLabel", blurbKey: "setup.templateZaiBlurb" },
  "zai-openai": { labelKey: "setup.templateZaiOpenAILabel", blurbKey: "setup.templateZaiOpenAIBlurb" },
  hetzner: { labelKey: "setup.templateHetznerLabel", blurbKey: "setup.templateHetznerBlurb" },
  custom: { labelKey: "setup.templateCustomLabel", blurbKey: "setup.templateCustomBlurb" },
  "custom-openai": { labelKey: "setup.templateCustomOpenAILabel", blurbKey: "setup.templateCustomOpenAIBlurb" },
};

/** Card grid of provider templates — shared by the settings "new provider"
 * flow and the runtime-profile wizard's inline provider creation. */
export function TemplateGrid({ onPick }: { onPick: (id: TemplateId) => void }) {
  const { t } = useTranslation();
  return (
    <div className="provider-templates">
      {TEMPLATES.map((tpl) => {
        const keys = TEMPLATE_KEYS[tpl.id];
        return (
          <button
            key={tpl.id}
            type="button"
            className="provider-template-card"
            onClick={() => onPick(tpl.id)}
          >
            <div className="provider-template-name">{t(keys.labelKey)}</div>
            <div className="provider-template-blurb">{t(keys.blurbKey)}</div>
          </button>
        );
      })}
    </div>
  );
}

export function configDirCopyForKind(kind: string): {
  labelKey: string;
  placeholderKey: string;
  hintKey: string;
} {
  if (kind === "codex" || kind === "fugu") {
    // Fugu deploys its profile into the Codex CLI config dir (~/.codex).
    return {
      labelKey: "setup.configDirLabelCodex",
      placeholderKey: "setup.configDirPlaceholderCodex",
      hintKey: "setup.configDirHintCodex",
    };
  }
  if (kind === "agy") {
    return {
      labelKey: "setup.configDirLabelAgy",
      placeholderKey: "setup.configDirPlaceholderAgy",
      hintKey: "setup.configDirHintAgy",
    };
  }
  if (kind === "copilot") {
    return {
      labelKey: "setup.configDirLabelCopilot",
      placeholderKey: "setup.configDirPlaceholderCopilot",
      hintKey: "setup.configDirHintCopilot",
    };
  }
  return {
    labelKey: "setup.configDirLabelClaude",
    placeholderKey: "setup.configDirPlaceholderClaude",
    hintKey: "setup.configDirHintClaude",
  };
}

/** Provider account payload — auth + capabilities only. */
export interface FormPayload {
  name: string;
  nickname?: string;
  kind: string;
  mode: Provider["mode"];
  base_url: string;
  config_dir: string;
  default_permission: Permission;
  api_key: string;
  suspended: boolean;
  capabilities?: Record<string, boolean>;
}

// Per-provider-native permission vocabularies (mirror backend/permission.py).
// One axis for claude/openai/pi, two independent axes (approval + sandbox) for codex.
const PERMISSION_OPTIONS: Record<string, Record<string, string[]>> = {
  claude: { mode: ["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"] },
  codex: {
    approval: ["untrusted", "on-request", "on-failure", "never"],
    sandbox: ["read-only", "workspace-write", "danger-full-access"],
  },
  openai: { mode: ["default", "bypassPermissions"] },
  pi: { mode: ["yolo", "plan"] },
  qwen: { mode: ["auto_edit", "yolo", "plan"] },
  cursor: { mode: ["default", "force"] },
  amp: { mode: ["default", "dangerously-allow-all"] },
  opencode: { mode: ["default", "auto", "readonly"] },
};
const PERMISSION_DEFAULTS: Record<string, Record<string, string>> = {
  claude: { mode: "bypassPermissions" },
  codex: { approval: "never", sandbox: "danger-full-access" },
  openai: { mode: "bypassPermissions" },
  pi: { mode: "yolo" },
  qwen: { mode: "yolo" },
  cursor: { mode: "force" },
  amp: { mode: "dangerously-allow-all" },
  opencode: { mode: "auto" },
};
function permissionOptionsForKind(kind: string): Record<string, string[]> {
  return PERMISSION_OPTIONS[kind] ?? {};
}

// Capability keys overridable per provider (kind gives the default; these
// force it on/off). Tri-state in the editor: inherit / on / off.
const CAPABILITY_KEYS = [
  "supports_fork",
  "supports_manager_mode",
  "supports_rewind",
  "supports_steering",
  "supports_native_subagents",
  "supports_reasoning_effort",
] as const;
type CapState = "inherit" | "on" | "off";

export const KEEP = "__keep__";

export function ProviderForm({
  mode,
  initial,
  initialHasKey,
  credentialBlocked = false,
  onClose,
  onBack,
  onSubmit,
}: {
  mode: "create" | "edit";
  initial: Omit<FormPayload, "api_key" | "default_permission" | "suspended"> & {
    api_key?: string;
    capability_overrides?: Partial<Record<string, boolean>>;
    default_permission?: Permission;
    suspended?: boolean;
  };
  initialHasKey: boolean;
  credentialBlocked?: boolean;
  onClose: () => void;
  onBack: () => void;
  onSubmit: (payload: FormPayload) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(initial.name);
  const [nickname, setNickname] = useState(initial.nickname ?? "");
  const [kind] = useState(initial.kind || "claude");
  const modes = availableModesForForm(kind, mode, initial.mode);
  const [mode_, setMode] = useState<Provider["mode"]>(
    modes.includes(initial.mode) ? initial.mode : modes[0],
  );
  const [baseUrl, setBaseUrl] = useState(initial.base_url);
  const [configDir, setConfigDir] = useState(initial.config_dir);
  const configDirCopy = configDirCopyForKind(kind);
  const apiEnvCopy = apiEnvCopyForKind(kind);
  const permissionOptions = permissionOptionsForKind(kind);
  const seedPermission = (): Permission => {
    const opts = permissionOptions;
    const saved = initial.default_permission;
    const out: Permission = {};
    for (const axis of Object.keys(opts)) {
      const allowed = opts[axis];
      const v = saved?.[axis];
      out[axis] = v && allowed.includes(v) ? v : PERMISSION_DEFAULTS[kind]?.[axis] ?? allowed[0];
    }
    return out;
  };
  const [defaultPermission, setDefaultPermission] = useState<Permission>(seedPermission);
  const [apiKey, setApiKey] = useState(initial.api_key ?? "");
  const [suspended, setSuspended] = useState(initial.suspended === true);
  const [submitting, setSubmitting] = useState(false);
  // Per-capability tri-state: inherit (kind default) / on / off. Seeded
  // from the provider's raw override map so an untouched save reproduces
  // the same overrides (never silently clears them).
  const initialOverrides = initial.capability_overrides || {};
  const [capStates, setCapStates] = useState<Record<string, CapState>>(
    Object.fromEntries(
      CAPABILITY_KEYS.map((k) => [
        k,
        initialOverrides[k] === true
          ? "on"
          : initialOverrides[k] === false
            ? "off"
            : "inherit",
      ]),
    ) as Record<string, CapState>,
  );

  const submit = async () => {
    setSubmitting(true);
    try {
      await onSubmit({
        name,
        nickname,
        kind,
        mode: mode_,
        base_url: baseUrl,
        config_dir: configDir,
        default_permission: defaultPermission,
        api_key:
          mode_ === "api_key"
            ? apiKey || (initialHasKey ? KEEP : "")
            : "",
        suspended,
        capabilities: Object.fromEntries(
          CAPABILITY_KEYS.filter((k) => capStates[k] !== "inherit").map((k) => [
            k,
            capStates[k] === "on",
          ]),
        ),
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <div className="modal-header">
        <button className="modal-back" onClick={onBack} title={t('setup.backTitle')}>
          &larr;
        </button>
        <h2>{mode === "create" ? t('setup.newProviderTitle') : t('setup.editProviderTitle')}</h2>
        <button className="modal-close" onClick={onClose}>
          &times;
        </button>
      </div>

      <div className="modal-body">
        <div className="setup-field-row setup-field-row-2col">
          <div className="setup-field">
            <label>{t('setup.nameLabel')}</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t('setup.namePlaceholder')}
              spellCheck={false}
            />
          </div>
          <div className="setup-field">
            <label>{t('setup.nicknameLabel')}</label>
            <input
              type="text"
              value={nickname}
              onChange={(e) => setNickname(e.target.value)}
              placeholder={t('setup.nicknamePlaceholder')}
              spellCheck={false}
            />
          </div>
        </div>

        {modes.length > 1 && (
          <div className="setup-mode-toggle">
            {modes.includes("subscription") && (
              <button
                className={`setup-mode-btn ${
                  mode_ === "subscription" ? "active" : ""
                }`}
                onClick={() => setMode("subscription")}
                type="button"
              >
                <span className="setup-mode-icon"><Icon name="star" size={14} style={{ verticalAlign: "-2px" }} /></span>
                {t('setup.subscriptionButton')}
              </button>
            )}
            {modes.includes("api_key") && (
              <button
                className={`setup-mode-btn ${mode_ === "api_key" ? "active" : ""}`}
                onClick={() => setMode("api_key")}
                type="button"
              >
                <span className="setup-mode-icon"><Icon name="settings" size={14} style={{ verticalAlign: "-2px" }} /></span>
                {t('setup.apiKeyButton')}
              </button>
            )}
          </div>
        )}

        {mode_ === "api_key" && (
          <div className="setup-fields">
            <div className="setup-field">
              <label>{t(apiEnvCopy.keyLabelKey)}</label>
              {credentialBlocked && (
                <div className="setup-error">{t('setup.apiKeyBlockedReentryHint')}</div>
              )}
              <input
                type="password"
                aria-label={t(apiEnvCopy.keyLabelKey)}
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={
                  credentialBlocked
                    ? t('setup.apiKeyReenterPlaceholder')
                    : initialHasKey
                    ? t('setup.apiKeyPlaceholderKeep')
                    : t(apiEnvCopy.keyPlaceholderKey)
                }
                spellCheck={false}
              />
              <span className="setup-field-hint">{t("setup.apiKeySecurityHint")}</span>
            </div>
            <div className="setup-field">
              <label>{t(apiEnvCopy.urlLabelKey)}</label>
              <input
                type="text"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder={t('setup.baseUrlPlaceholder')}
                spellCheck={false}
              />
            </div>
          </div>
        )}

        {showConfigDirForKind(kind) && (
          <div className="setup-field">
            <label>{t(configDirCopy.labelKey)}</label>
            <input
              type="text"
              value={configDir}
              onChange={(e) => setConfigDir(e.target.value)}
              placeholder={t(configDirCopy.placeholderKey)}
              spellCheck={false}
            />
            <span className="setup-field-hint">
              {t(configDirCopy.hintKey)}
            </span>
          </div>
        )}

        {Object.keys(permissionOptions).length > 0 && (
          <div className="setup-field">
            <label>{t('setup.defaultPermissionLabel')}</label>
            {Object.entries(permissionOptions).map(([axis, allowed]) => (
              <Select
                key={axis}
                data-testid={`permission-axis-select-${axis}`}
                className="permission-axis-select"
                value={defaultPermission[axis] ?? allowed[0]}
                onChange={(v) =>
                  setDefaultPermission((prev) => ({ ...prev, [axis]: v }))
                }
                title={t(`permission.axis.${axis}`)}
                options={allowed.map((value) => ({
                  value,
                  label: t(`permission.value.${value}`, { defaultValue: value }),
                }))}
              />
            ))}
          </div>
        )}

        <label className="setup-field provider-suspend-toggle">
          <span>{t('setup.suspendProviderLabel')}</span>
          <input
            type="checkbox"
            checked={suspended}
            onChange={(e) => setSuspended(e.target.checked)}
          />
          <span className="setup-field-hint">{t('setup.suspendProviderHint')}</span>
        </label>

        <div className="setup-field">
          <label>{t('setup.capabilitiesLabel')}</label>
          <div className="capability-overrides">
            {CAPABILITY_KEYS.map((key) => (
              <label key={key} className="context-strategy-row">
                <span>{t(`setup.capability.${key}`)}</span>
                <Select
                  data-testid={`capability-override-select-${key}`}
                  value={capStates[key] || "inherit"}
                  onChange={(v) =>
                    setCapStates((prev) => ({ ...prev, [key]: v as CapState }))
                  }
                  options={[
                    { value: "inherit", label: t('setup.capabilityInherit') },
                    { value: "on", label: t('setup.capabilityOn') },
                    { value: "off", label: t('setup.capabilityOff') },
                  ]}
                />
              </label>
            ))}
          </div>
        </div>
      </div>

      <div className="modal-footer">
        <button className="setup-cancel-btn" onClick={onBack}>
          {t('setup.cancelButton')}
        </button>
        <button
          className="setup-save-btn"
          onClick={submit}
          disabled={submitting || (credentialBlocked && !apiKey)}
        >
          {submitting
            ? t('setup.saving')
            : mode === "create"
            ? t('setup.createProvider')
            : t('setup.saveChanges')}
        </button>
      </div>
    </>
  );
}
