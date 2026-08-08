import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { Provider, ReasoningEffort, Permission } from "../types";
import type { FormFieldWire } from "../adapter/wire";
import { fieldHintKey, fieldLabelKey, fieldPlaceholderKey, modesFromSchema, schemaHasField } from "./providerFormFields";
import { Select } from "./Select";
import Icon from "./Icon";

// ---------------------------------------------------------------------------
// Provider ACCOUNT templates + creation/edit form (auth-shaped). Execution
// selection (runner, default model/effort) lives on runtime profiles — see
// RuntimeProfileWizard / RuntimeProfilesEditor.
//
// Descriptor-driven per ADR 0007 (Package D): the installable-kind catalog
// (id/label/defaults/allowed-mode/field-presence data) comes from the
// backend's `GET /api/v2/surface/providers/installable` — see
// `frontend/src/hooks/useInstallableProviders.ts`, the ONE source of
// `Template[]` now (the static `TEMPLATES` array + `providerFormShape.ts`'s
// per-kind mode-restriction/field-visibility tables that used to live here
// are deleted). `default_permission`/per-capability tri-state overrides
// have no backend `form_schema` equivalent (documented gap,
// `backend/adapters/provider_adapter.py`'s `_form_schema_for` docstring) —
// those sections below stay kind-based, by design, not a leftover.
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
  /** Backend-derived field list (`InstallableDescriptor.form_schema`) —
   * which base fields `ProviderForm` below should render/gate for this
   * kind. See `useInstallableProviders.ts`'s `formSchemaForProviderKind`
   * for how an EDIT-mode form (no template id, only a `kind`) resolves one
   * of these. */
  formSchema: FormFieldWire[];
}

/** Card grid of installable provider kinds — shared by the settings "new
 * provider" flow and the runtime-profile wizard's inline provider
 * creation. `templates` comes from `useInstallableProviders()` (backend
 * `GET /providers/installable`, ADR 0007 Package D) — no static list here
 * anymore. Per-kind blurb copy (the descriptor carries none) is resolved
 * via `_TEMPLATE_COPY_KEYS`, falling back to the descriptor's raw
 * `display.label` (no blurb) for any kind not in that presentation-only
 * table — never a crash, never fabricated business data. */
const _TEMPLATE_COPY_KEYS: Record<string, { labelKey: string; blurbKey: string }> = {
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

export function TemplateGrid({
  templates,
  loading,
  error,
  onRetry,
  onPick,
}: {
  templates: Template[];
  /** A9: while the backend catalog fetch is in flight and no templates have
   * arrived yet, show a loading placeholder instead of a blank grid. */
  loading?: boolean;
  /** A9: fetch failed and no templates are available — show the error with
   * a retry affordance instead of silently rendering an empty grid. */
  error?: string | null;
  onRetry?: () => void;
  onPick: (id: string) => void;
}) {
  const { t } = useTranslation();

  if (templates.length === 0 && loading) {
    return <div className="settings-hint">{t("common.loading", "Loading…")}</div>;
  }

  if (templates.length === 0 && error) {
    return (
      <div className="provider-catalog-error" role="alert" data-testid="provider-catalog-error">
        <span>{error}</span>
        {onRetry && (
          <button type="button" className="btn-secondary" onClick={onRetry}>
            {t("common.retry", "Retry")}
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="provider-templates">
      {templates.map((tpl) => {
        const keys = _TEMPLATE_COPY_KEYS[tpl.id];
        return (
          <button
            key={tpl.id}
            type="button"
            className="provider-template-card"
            onClick={() => onPick(tpl.id)}
          >
            <div className="provider-template-name">{keys ? t(keys.labelKey) : tpl.label}</div>
            <div className="provider-template-blurb">{keys ? t(keys.blurbKey) : ""}</div>
          </button>
        );
      })}
    </div>
  );
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
  formSchema,
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
  /** `InstallableDescriptor.form_schema` for this provider's kind (ADR
   * 0007 Package D) — drives which of the base fields below render/gate.
   * For CREATE, the picked template's own schema; for EDIT,
   * `formSchemaForProviderKind(templates, provider.kind)`. */
  formSchema: FormFieldWire[];
  onClose: () => void;
  onBack: () => void;
  onSubmit: (payload: FormPayload) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(initial.name);
  const [nickname, setNickname] = useState(initial.nickname ?? "");
  const [kind] = useState(initial.kind || "claude");
  const modes = modesFromSchema(formSchema, mode, initial.mode);
  const [mode_, setMode] = useState<Provider["mode"]>(
    modes.includes(initial.mode) ? initial.mode : modes[0],
  );
  const [baseUrl, setBaseUrl] = useState(initial.base_url);
  const [configDir, setConfigDir] = useState(initial.config_dir);
  const showConfigDir = schemaHasField(formSchema, "config_dir");
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
              {/* label_key/placeholder_key come from `form_schema`'s
               * "api_key" field — per-kind now (closure 4: restores the
               * deleted `apiEnvCopyForKind`'s OpenAI-flavored
               * "setup.apiKeyLabelOpenai"/"setup.apiKeyPlaceholderEmptyOpenai"
               * copy for kind="openai", generic ANTHROPIC_* copy for every
               * other kind — the backend descriptor is the one source now). */}
              <label>{t(fieldLabelKey(formSchema, "api_key", "setup.apiKeyLabel"))}</label>
              {credentialBlocked && (
                <div className="setup-error">{t('setup.apiKeyBlockedReentryHint')}</div>
              )}
              <input
                type="password"
                aria-label={t(fieldLabelKey(formSchema, "api_key", "setup.apiKeyLabel"))}
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={
                  credentialBlocked
                    ? t('setup.apiKeyReenterPlaceholder')
                    : initialHasKey
                    ? t('setup.apiKeyPlaceholderKeep')
                    : t(fieldPlaceholderKey(formSchema, "api_key", "setup.apiKeyPlaceholderEmpty"))
                }
                spellCheck={false}
              />
              <span className="setup-field-hint">{t("setup.apiKeySecurityHint")}</span>
            </div>
            <div className="setup-field">
              <label>{t(fieldLabelKey(formSchema, "base_url", "setup.baseUrlLabel"))}</label>
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

        {showConfigDir && (
          <div className="setup-field">
            {/* label_key/placeholder_key/hint_key come from `form_schema`'s
             * "config_dir" field — per-kind now (closure 4: restores the
             * deleted `configDirCopyForKind`'s codex/agy/copilot-flavored
             * copy; generic Claude-flavored copy for every other kind). */}
            <label>{t(fieldLabelKey(formSchema, "config_dir", "setup.configDirLabelClaude"))}</label>
            <input
              type="text"
              value={configDir}
              onChange={(e) => setConfigDir(e.target.value)}
              placeholder={t(fieldPlaceholderKey(formSchema, "config_dir", "setup.configDirPlaceholderClaude"))}
              spellCheck={false}
            />
            <span className="setup-field-hint">
              {t(fieldHintKey(formSchema, "config_dir", "setup.configDirHintClaude") ?? "setup.configDirHintClaude")}
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
