import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Capacitor } from "@capacitor/core";
import type { Project, Provider, ProvidersState, ReasoningEffort, Permission } from "../types";
import {
  defaultProviderAuthority,
  providerAuthority,
  requireProvider,
} from "../providerAuthority";
import { trackPromise } from "../progress/store";
import { ShortcutSettings } from "./ShortcutSettings";
import { CrossSessionDelegateSetting } from "./CrossSessionDelegateSetting";
import { TaskStartSilenceSetting } from "./TaskStartSilenceSetting";
import { RecursionGuardsSettings } from "./RecursionGuardsSettings";
import { ContextStrategySetting } from "./ContextStrategySetting";
import { SessionTabsSettings } from "./SessionTabsSettings";
import { VoiceSettings } from "./VoiceSettings";
import { InstallationCapabilities } from "./InstallationCapabilities";
import { SessionAutoDeleteSetting } from "./SessionAutoDeleteSetting";
import { NativeImportSetting } from "./NativeImportSetting";
import { DelegateTaskPolicySetting } from "./DelegateTaskPolicySetting";
import { InternalLLMSetting } from "./InternalLLMSetting";
import { SearchInput } from "./SearchInput";
import { eventBus } from "../lib/eventBus";
import { LanguageSelector } from "./LanguageSelector";
import {
  availableModesForForm,
  apiEnvCopyForKind,
  showConfigDirForKind,
} from "./providerFormShape";
import { Select } from "./Select";
import { cacheProviders } from "../utils/providerCache";
import { providerNickname } from "../utils/providerDisplayName";
import { runnerLabelKey, runtimeKindForRunner } from "./modelPicker";
import { useProviderInstalls, type InstallRun } from "../hooks/useProviderInstalls";
import { useProviderModelCatalog } from "../hooks/useProviderModelCatalog";
import { ModelCatalogStatus } from "./ModelCatalogStatus";
import { MobileSetup } from "./MobileSetup";
import { AppearanceSetting } from "./AppearanceSetting";
import { UserDisplayNameSetting } from "./UserDisplayNameSetting";
import { AuthCredentialsSetting } from "./AuthCredentialsSetting";
import { PasswordManagerSetting } from "./PasswordManagerSetting";
import { lazyWithRetry } from "../lib/lazyWithRetry";
import {
  downloadUrl as desktopDownloadUrl,
  platformLabel as desktopPlatformLabel,
  type DesktopInstallPlatform,
  type DesktopStatus,
} from "../hooks/useDesktopInstallOffer";
import Icon from "./Icon";
import {
  ExtensionModuleSlot,
  useExtensionFrontendModules,
  type ExtensionFrontendModule,
} from "./ExtensionSlots";
import { ExtensionQuickButtons, type HookActionContext } from "./ExtensionUiHooks";
import { ServerSetting } from "./ServerSetting";
import { BasCompanionAppsSetting } from "./BasCompanionAppsSetting";
import { MobileNotificationSettings } from "./MobileNotificationSettings";
import { ExtensionAppSettingsSection } from "./ExtensionAppSettingsSection";
import { useExtensionAppSettings } from "../hooks/useExtensionAppSettings";

import { API } from "../api";
import { providerQuotaStatus } from "../utils/quotaStatus";
import { useQuotaStatus } from "../hooks/useQuotaStatus";
import { QuotaIndicator } from "./QuotaIndicator";

// Run `fn` between setBusy(true/false) bookends, routing exceptions
// into `setError` (cleared on entry). `fallback` is the message used
// when the thrown value is not an Error instance — captures the
async function runBusyAction(
  setBusy: (b: boolean) => void,
  setError: (msg: string) => void,
  fallback: string,
  fn: () => Promise<void>,
): Promise<void> {
  setBusy(true);
  setError("");
  try {
    await fn();
  } catch (e) {
    setError(e instanceof Error ? e.message : fallback);
  } finally {
    setBusy(false);
  }
}

interface Props {
  onClose: () => void;
  onRefreshApp?: () => void;
  refreshAppDisabled?: boolean;
  hookActionContext: HookActionContext;
  teamEnabled?: boolean;
  credentialBrokerEnabled?: boolean;
  onEditHarnessDescriptionFile?: (path: string) => Promise<unknown>;
}

type View =
  | { kind: "list" }
  | { kind: "edit"; providerId: string }
  | { kind: "wizard-templates" }
  | { kind: "wizard-form"; templateId: TemplateId }
  | { kind: "mobile" };

type TemplateId = (typeof TEMPLATES)[number]["id"];
type InstallableProviderKind = "claude" | "codex" | "agy" | "copilot" | "pi" | "qwen" | "amp" | "opencode";
type SettingsSection =
  | "providers"
  | "account"
  | "language"
  | "appearance"
  | "desktop"
  | "recovery"
  | "shortcuts"
  | "delegation"
  | "context"
  | "internalLlm"
  | "sessions"
  | "voice"
  | "extensions"
  | "capabilities"
  | "harnessProfiles"
  | "passwords"
  | "server"
  | "notifications"
  | `extension:${string}`
  | `extsettings:${string}`;

/** Core section ids an extension may contribute settings INTO — a manifest
 * section whose id matches one of these appends to that section instead of
 * adding a second nav entry with the same meaning. */
const CORE_SETTINGS_SECTION_IDS: ReadonlySet<string> = new Set([
  "providers", "account", "language", "appearance", "desktop", "recovery",
  "shortcuts", "delegation", "context", "internalLlm", "sessions", "voice",
  "extensions", "capabilities", "harnessProfiles", "passwords", "server",
  "notifications",
]);

const EXT_SETTINGS_PREFIX = "extsettings:";

function navIdForExtensionSection(sectionId: string): SettingsSection {
  return CORE_SETTINGS_SECTION_IDS.has(sectionId)
    ? (sectionId as SettingsSection)
    : (`${EXT_SETTINGS_PREFIX}${sectionId}` as SettingsSection);
}
type NetworkBindAddress = "127.0.0.1" | "0.0.0.0";

interface ProviderSetupCommandResult {
  ok: boolean;
  stdout: string;
  stderr: string;
  returncode: number;
}

interface ProviderSetupStatus {
  kind: InstallableProviderKind;
  label: string;
  command: string;
  install_command: string[];
  prerequisite_command: string;
  prerequisite_install_command?: string[];
  prerequisite_installable?: boolean;
  prerequisite: ProviderSetupCommandResult;
  installed: boolean;
  verify: ProviderSetupCommandResult;
  install?: ProviderSetupCommandResult | null;
}

interface Template {
  id: string;
  label: string;
  blurb: string;
  defaults: {
    name: string;
    kind: string;
    mode: Provider["mode"];
    base_url: string;
    config_dir: string;
    default_model: string;
    runner?: Provider["runner"];
    default_reasoning_effort: ReasoningEffort | "";
    api_key?: string;
    suspended?: boolean;
  };
}

const REASONING_EFFORT_OPTIONS: Record<string, ReasoningEffort[]> = {
  claude: ["low", "medium", "high", "xhigh"],
  codex: ["none", "minimal", "low", "medium", "high", "xhigh"],
  fugu: ["high", "xhigh"],
};

const HarnessSettingsEditor = lazyWithRetry(() =>
  import("./HarnessSettingsEditor").then((module) => ({
    default: module.HarnessSettingsEditor,
  })),
);
const SAKANA_FUGU_API_BASE_URL = "https://api.sakana.ai/v1";

function effortOptionsForKind(kind: string): ReasoningEffort[] {
  return REASONING_EFFORT_OPTIONS[kind] ?? [];
}

function defaultEffortForKind(kind: string): ReasoningEffort | "" {
  const options = effortOptionsForKind(kind);
  return options.includes("medium") ? "medium" : options[0] ?? "";
}

function configDirCopyForKind(kind: string): {
  labelKey: string;
  placeholderKey: string;
  hintKey: string;
} {
  if (kind === "codex") {
    return {
      labelKey: "setup.configDirLabelCodex",
      placeholderKey: "setup.configDirPlaceholderCodex",
      hintKey: "setup.configDirHintCodex",
    };
  }
  if (kind === "fugu") {
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

const TEMPLATES = [
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

const KEEP = "__keep__";

export function SettingsPage({
  onClose,
  onRefreshApp,
  refreshAppDisabled = false,
  hookActionContext,
  teamEnabled = true,
  credentialBrokerEnabled = true,
  onEditHarnessDescriptionFile,
}: Props) {
  const { t } = useTranslation();
  const [state, setState] = useState<ProvidersState | null>(null);
  const [setupStatuses, setSetupStatuses] = useState<ProviderSetupStatus[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [firstRunDone, setFirstRunDone] = useState(true);
  const [networkBindAddress, setNetworkBindAddress] = useState<NetworkBindAddress>("127.0.0.1");
  const [mobileEnabled, setMobileEnabled] = useState(false);
  const [integrationsEnabled, setIntegrationsEnabled] = useState(false);
  const [view, setView] = useState<View>({ kind: "list" });
  const [section, setSection] = useState<SettingsSection>("providers");
  const [busy, setBusy] = useState(false);
  const [credentialRetryingId, setCredentialRetryingId] = useState<string | null>(null);
  const [error, setError] = useState("");

  const refetch = async () => {
    try {
      const { promise } = trackPromise("providers:list", async () => {
        const r = await fetch(`${API}/api/providers`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return (await r.json()) as ProvidersState;
      });
      const nextState = await promise;
      setState(nextState);
      cacheProviders(nextState.providers || [], nextState.default_provider_id ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "fetch failed");
    }
  };

  const requireProviderMutation = async (response: Response) => {
    if (response.ok) return;
    const detail = await response.text();
    if (response.status === 409) await refetch();
    throw new Error(detail || `HTTP ${response.status}`);
  };

  const refetchSetupStatus = async () => {
    try {
      const { promise } = trackPromise("providerSetup:status", async () => {
        const r = await fetch(`${API}/api/provider-setup/status`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return (await r.json()) as { providers: ProviderSetupStatus[] };
      });
      setSetupStatuses((await promise).providers);
    } catch (e) {
      setError(e instanceof Error ? e.message : "setup status failed");
    }
  };

  // Streaming provider-CLI installs. Backend owns the run registry; this
  // is the live projection. Concurrent installs (different kinds) are
  // allowed — each runs as its own background task.
  const onInstallFinished = useCallback(() => {
    void refetchSetupStatus();
  }, []);
  const { runs: installRuns, startInstall } = useProviderInstalls(onInstallFinished);

  const installProvider = useCallback(
    (kind: InstallableProviderKind) => {
      setError("");
      trackPromise(`providerSetup:install:${kind}`, async () => {
        try {
          await startInstall(kind);
        } catch (e) {
          setError(e instanceof Error ? e.message : "install failed");
          throw e;
        }
      }).promise.catch(() => {
        /* error already surfaced via setError */
      });
    },
    [startInstall],
  );

  const refetchPrefs = async () => {
    try {
      const { promise } = trackPromise("userPrefs:firstRun", async () => {
        const r = await fetch(`${API}/api/user-prefs`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return (await r.json()) as {
          first_run_wizard_done?: boolean;
          network_bind_address?: NetworkBindAddress;
        };
      });
      const prefs = await promise;
      setFirstRunDone(Boolean(prefs.first_run_wizard_done));
      if (prefs.network_bind_address === "127.0.0.1" || prefs.network_bind_address === "0.0.0.0") {
        setNetworkBindAddress(prefs.network_bind_address);
      }
    } catch {
      setFirstRunDone(true);
    }
  };

  const refetchProjects = async () => {
    try {
      const { promise } = trackPromise("setup:projects", async () => {
        const r = await fetch(`${API}/api/projects`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return (await r.json()) as { projects: Project[] };
      });
      setProjects((await promise).projects || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "projects failed");
    }
  };

  const refetchInstallationProfile = async () => {
    try {
      const r = await fetch(`${API}/api/installation-profile`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const profile = (await r.json()) as {
        mobile_enabled?: boolean;
        integrations_enabled?: boolean;
      };
      setMobileEnabled(profile.mobile_enabled === true);
      setIntegrationsEnabled(profile.integrations_enabled === true);
    } catch {
      setMobileEnabled(false);
      setIntegrationsEnabled(false);
    }
  };

  useEffect(() => {
    refetch();
    refetchSetupStatus();
    refetchPrefs();
    refetchProjects();
    refetchInstallationProfile();
  }, []);

  useEffect(() => {
    const handler = () => refetch();
    window.addEventListener("provider_changed", handler);
    return () => window.removeEventListener("provider_changed", handler);
  }, []);

  useEffect(() => {
    const handler = () => refetchInstallationProfile();
    window.addEventListener("installation_capabilities_changed", handler);
    return () => window.removeEventListener("installation_capabilities_changed", handler);
  }, []);

  useEffect(() => {
    if (
      (!teamEnabled && section === "delegation")
      || (!credentialBrokerEnabled && section === "passwords")
      || (!integrationsEnabled && section === "extensions")
    ) {
      setSection("providers");
    }
  }, [credentialBrokerEnabled, integrationsEnabled, section, teamEnabled]);

  const activeId = state?.default_provider_id ?? null;
  const providers = state?.providers ?? [];
  const content = (
    <>
      {view.kind === "list" && (
        <ProvidersList
          providers={providers}
          activeId={activeId}
          busy={busy}
          error={error}
          onClose={onClose}
          onRefreshApp={onRefreshApp}
          refreshAppDisabled={refreshAppDisabled}
          hookActionContext={hookActionContext}
          onAdd={() => setView({ kind: "wizard-templates" })}
          onMobile={() => setView({ kind: "mobile" })}
          mobileEnabled={mobileEnabled}
          integrationsEnabled={integrationsEnabled}
          onEdit={(p) => setView({ kind: "edit", providerId: p.id })}
          setupStatuses={setupStatuses}
          projects={projects}
          firstRunDone={firstRunDone}
          networkBindAddress={networkBindAddress}
          teamEnabled={teamEnabled}
          credentialBrokerEnabled={credentialBrokerEnabled}
          onEditHarnessDescriptionFile={onEditHarnessDescriptionFile}
          section={section}
          onSectionChange={setSection}
          onAddProject={(path) => runBusyAction(setBusy, setError, "add project failed", async () => {
            await trackPromise("setup:project:add", async () => {
              const r = await fetch(`${API}/api/projects`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ path }),
              });
              if (!r.ok) throw new Error(await r.text());
            }).promise;
            await refetchProjects();
          })}
          onInstallProvider={installProvider}
          installRuns={installRuns}
          onVerifyProviders={() => refetchSetupStatus()}
          onNetworkBindChange={(address) => runBusyAction(setBusy, setError, "network save failed", async () => {
            await trackPromise("userPrefs:networkBind", async () => {
              const r = await fetch(`${API}/api/user-prefs`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ network_bind_address: address }),
              });
              if (!r.ok) throw new Error(await r.text());
            }).promise;
            setNetworkBindAddress(address);
          })}
          onActivate={(p) => runBusyAction(setBusy, setError, "activate failed", async () => {
            await trackPromise(`provider:activate:${p.id}`, async () => {
              const r = await fetch(`${API}/api/providers/${p.id}/set-default`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(defaultProviderAuthority(p, providers, activeId)),
              });
              await requireProviderMutation(r);
            }).promise;
            await refetch();
          })}
          onSuspend={(p, suspended) => runBusyAction(setBusy, setError, suspended ? "suspend failed" : "resume failed", async () => {
            await trackPromise(`provider:suspend:${p.id}`, async () => {
              const r = await fetch(`${API}/api/providers/${p.id}/suspended`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ suspended, ...providerAuthority(p) }),
              });
              await requireProviderMutation(r);
            }).promise;
            await refetch();
          })}
          credentialRetryingId={credentialRetryingId}
          onRetryCredential={async (p) => {
            setCredentialRetryingId(p.id);
            try {
              await runBusyAction(setBusy, setError, "credential retry failed", async () => {
                await trackPromise(`provider:credential:retry:${p.id}`, async () => {
                  const r = await fetch(`${API}/api/providers/${p.id}/credential/retry`, { method: "POST" });
                  if (!r.ok) throw new Error(await r.text());
                }).promise;
                await refetch();
              });
            } finally {
              setCredentialRetryingId(null);
            }
          }}
          onDelete={async (p) => {
            if (!confirm(t('setup.deleteConfirm'))) return;
            await runBusyAction(setBusy, setError, "delete failed", async () => {
              await trackPromise(`provider:delete:${p.id}`, async () => {
                const r = await fetch(`${API}/api/providers/${p.id}`, {
                  method: "DELETE",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify(providerAuthority(p)),
                });
                await requireProviderMutation(r);
              }).promise;
              await refetch();
            });
          }}
        />
      )}

      {view.kind === "wizard-templates" && (
        <WizardTemplates
          onClose={onClose}
          onBack={() => setView({ kind: "list" })}
          onPick={(templateId) => setView({ kind: "wizard-form", templateId })}
        />
      )}

      {view.kind === "wizard-form" && (
        <ProviderForm
          mode="create"
          initial={TEMPLATES.find((t) => t.id === view.templateId)!.defaults}
          initialHasKey={false}
          onClose={onClose}
          onBack={() => setView({ kind: "wizard-templates" })}
          onSubmit={(payload) => runBusyAction(setBusy, setError, "create failed", async () => {
            await trackPromise("provider:create", async () => {
              const r = await fetch(`${API}/api/providers`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
              });
              if (!r.ok) throw new Error(await r.text());
            }).promise;
            await refetch();
            setView({ kind: "list" });
          })}
        />
      )}

      {view.kind === "mobile" && (
        <MobileSetup open={true} onClose={() => setView({ kind: "list" })} />
      )}

      {view.kind === "edit" && (
        <EditProvider
          providers={providers}
          providerId={view.providerId}
          activeId={activeId}
          busy={busy}
          error={error}
          onClose={onClose}
          onBack={() => setView({ kind: "list" })}
          onSubmit={(payload) => runBusyAction(setBusy, setError, "save failed", async () => {
            await trackPromise(`provider:patch:${view.providerId}`, async () => {
              const r = await fetch(`${API}/api/providers/${view.providerId}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  ...payload,
                  ...providerAuthority(requireProvider(providers, view.providerId)),
                }),
              });
              await requireProviderMutation(r);
            }).promise;
            await refetch();
            setView({ kind: "list" });
          })}
          onActivate={() => runBusyAction(setBusy, setError, "activate failed", async () => {
            await trackPromise(`provider:activate:${view.providerId}`, async () => {
              const provider = requireProvider(providers, view.providerId);
              const r = await fetch(`${API}/api/providers/${view.providerId}/set-default`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(defaultProviderAuthority(provider, providers, activeId)),
              });
              await requireProviderMutation(r);
            }).promise;
            await refetch();
          })}
          onSuspend={(suspended) => runBusyAction(setBusy, setError, suspended ? "suspend failed" : "resume failed", async () => {
            await trackPromise(`provider:suspend:${view.providerId}`, async () => {
              const provider = requireProvider(providers, view.providerId);
              const r = await fetch(`${API}/api/providers/${view.providerId}/suspended`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ suspended, ...providerAuthority(provider) }),
              });
              await requireProviderMutation(r);
            }).promise;
            await refetch();
          })}
          onDelete={async () => {
            if (!confirm(t('setup.deleteConfirm'))) return;
            await runBusyAction(setBusy, setError, "delete failed", async () => {
              await trackPromise(`provider:delete:${view.providerId}`, async () => {
                const provider = requireProvider(providers, view.providerId);
                const r = await fetch(`${API}/api/providers/${view.providerId}`, {
                  method: "DELETE",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify(providerAuthority(provider)),
                });
                await requireProviderMutation(r);
              }).promise;
              await refetch();
              setView({ kind: "list" });
            });
          }}
        />
      )}
    </>
  );

  return <main className="settings-page">{content}</main>;
}

// ---------------------------------------------------------------------------
// List view
// ---------------------------------------------------------------------------

interface ProvidersListProps {
  providers: Provider[];
  activeId: string | null;
  busy: boolean;
  error: string;
  onClose: () => void;
  onRefreshApp?: () => void;
  refreshAppDisabled: boolean;
  hookActionContext: HookActionContext;
  onAdd: () => void;
  onMobile: () => void;
  mobileEnabled: boolean;
  integrationsEnabled: boolean;
  onEdit: (p: Provider) => void;
  onActivate: (p: Provider) => void;
  onSuspend: (p: Provider, suspended: boolean) => void;
  credentialRetryingId: string | null;
  onRetryCredential: (p: Provider) => void;
  onDelete: (p: Provider) => void;
  setupStatuses: ProviderSetupStatus[];
  projects: Project[];
  firstRunDone: boolean;
  networkBindAddress: NetworkBindAddress;
  teamEnabled: boolean;
  credentialBrokerEnabled: boolean;
  onEditHarnessDescriptionFile?: (path: string) => Promise<unknown>;
  section: SettingsSection;
  onSectionChange: (section: SettingsSection) => void;
  onAddProject: (path: string) => void;
  onInstallProvider: (kind: InstallableProviderKind) => void;
  installRuns: Record<string, InstallRun>;
  onVerifyProviders: () => void;
  onNetworkBindChange: (address: NetworkBindAddress) => void;
}

interface ExtensionListRecord {
  enabled?: boolean;
  manifest?: {
    id: string;
    description?: string;
    entrypoints?: {
      instructions?: { name: string; level?: string }[];
      provider_capabilities?: { name: string; level?: string }[]; // legacy field name
      skills?: { name: string }[];
      mcp?: Array<string | { name?: string }>;
    };
  };
  instructions_enabled?: { global?: boolean; projects?: Record<string, boolean> };
}
interface ExtensionConfigRow {
  id: string;
  name: string;
  description: string;
  required: boolean;
  enabled: boolean;
}

type PersonalHarnessFile = {
  name: string;
  content: string;
  level: "global";
};

export function ExtensionUiSettingsSection() {
  const { t } = useTranslation();
  const [rows, setRows] = useState<ExtensionConfigRow[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [creatingPersonalHarness, setCreatingPersonalHarness] = useState(false);
  const [personalHarnessFiles, setPersonalHarnessFiles] = useState<PersonalHarnessFile[]>([]);
  const personalHarnessInputRef = useRef<HTMLInputElement | null>(null);
  const [deletingIds, setDeletingIds] = useState<Set<string>>(() => new Set());
  const [availableUpdates, setAvailableUpdates] = useState<Record<string, { availableVersion: string }>>({});
  const [updatingIds, setUpdatingIds] = useState<Set<string>>(() => new Set());
  const refresh = useCallback(async () => {
    try {
      const listRes = await fetch(`${API}/api/extensions?include_hidden=true`, { credentials: "include" });
      const listData = await listRes.json();
      const records: ExtensionListRecord[] = Array.isArray(listData.extensions) ? listData.extensions : [];
      const installed = records.filter((r) => r.manifest?.id);
      const configs: ExtensionConfigRow[] = [];
      for (const record of installed) {
        const id = record.manifest!.id;
        try {
          const res = await fetch(`${API}/api/extensions/${encodeURIComponent(id)}/config`, {
            credentials: "include",
          });
          if (!res.ok) continue;
          const cfg = await res.json();
          const row: ExtensionConfigRow = {
            id,
            name: cfg.name || id,
            description: typeof record.manifest?.description === "string" ? record.manifest.description.trim() : "",
            required: cfg.required === true,
            enabled: record.enabled !== false,
          };
          configs.push(row);
        } catch {
          // skip extensions whose config can't be loaded
        }
      }
      configs.sort((a, b) => a.name.localeCompare(b.name));
      setRows(configs);
      setError("");
    } catch {
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshUpdates = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/extensions/updates`, { credentials: "include" });
      if (!res.ok) return;
      const data = await res.json();
      const next: Record<string, { availableVersion: string }> = {};
      for (const row of Array.isArray(data.results) ? data.results : []) {
        if (row?.update_available === true && typeof row.extension_id === "string") {
          next[row.extension_id] = {
            availableVersion: typeof row.available_version === "string" ? row.available_version : "",
          };
        }
      }
      setAvailableUpdates(next);
    } catch {
      // offline: keep the last known projection
    }
  }, []);

  useEffect(() => {
    void refresh();
    void refreshUpdates();
    const unsubscribeUpdates = eventBus.subscribe("extension_updates_changed", () => {
      void refreshUpdates();
    });
    const unsubscribeExtensions = eventBus.subscribe("extension.catalog", () => {
      void refreshUpdates();
    });
    return () => {
      unsubscribeUpdates();
      unsubscribeExtensions();
    };
  }, [refresh, refreshUpdates]);

  const patch = useCallback(
    async (path: string, body: unknown, onError?: () => void) => {
      try {
        await fetch(`${API}${path}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify(body),
        }).then((res) => {
          if (!res.ok) throw new Error("patch failed");
        });
      } catch {
        if (onError) onError();
        void refresh();
      }
    },
    [refresh],
  );

  const toggleExtension = useCallback(
    (id: string, next: boolean) => {
      setRows((prev) => prev.map((r) => (r.id === id ? { ...r, enabled: next } : r)));
      void patch(
        `/api/extensions/${encodeURIComponent(id)}/enabled`,
        { enabled: next },
        () => setRows((prev) => prev.map((r) => (r.id === id ? { ...r, enabled: !next } : r))),
      );
    },
    [patch],
  );

  const uninstallExtension = useCallback(
    async (id: string, name: string) => {
      if (!window.confirm(t("settings.extensionsUninstallConfirm", { name }))) return;
      setDeletingIds((prev) => new Set(prev).add(id));
      setError("");
      try {
        const res = await fetch(`${API}/api/extensions/${encodeURIComponent(id)}`, {
          method: "DELETE",
          credentials: "include",
        });
        if (!res.ok) {
          let detail = "";
          try {
            const payload = await res.json();
            detail = typeof payload.detail === "string" ? payload.detail : "";
          } catch {
            detail = await res.text();
          }
          throw new Error(detail || t("settings.extensionsUninstallFailed"));
        }
        setRows((prev) => prev.filter((row) => row.id !== id));
        void refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : t("settings.extensionsUninstallFailed"));
      } finally {
        setDeletingIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }
    },
    [refresh, t],
  );

  const updateExtension = useCallback(
    async (id: string) => {
      setUpdatingIds((prev) => new Set(prev).add(id));
      setError("");
      try {
        const res = await fetch(`${API}/api/extensions/${encodeURIComponent(id)}/update`, {
          method: "POST",
          credentials: "include",
        });
        if (!res.ok) {
          let detail = "";
          try {
            const payload = await res.json();
            detail = typeof payload.detail === "string" ? payload.detail : "";
          } catch {
            detail = await res.text();
          }
          throw new Error(detail || t("settings.extensionsUpdateFailed"));
        }
        await Promise.all([refresh(), refreshUpdates()]);
      } catch (e) {
        setError(e instanceof Error ? e.message : t("settings.extensionsUpdateFailed"));
      } finally {
        setUpdatingIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }
    },
    [refresh, refreshUpdates, t],
  );

  const loadPersonalHarnessFiles = useCallback(async (fileList: FileList | null) => {
    const files = Array.from(fileList ?? []);
    if (files.length === 0) return;
    setError("");
    try {
      const loaded = await Promise.all(
        files.map(async (file) => ({
          name: file.name,
          content: await file.text(),
          level: "global" as const,
        })),
      );
      setPersonalHarnessFiles((prev) => [...prev, ...loaded]);
      if (personalHarnessInputRef.current) {
        personalHarnessInputRef.current.value = "";
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : t("settings.extensionsPersonalHarnessFailed"));
    }
  }, [t]);

  const clearPersonalHarnessFiles = useCallback(() => {
    setPersonalHarnessFiles([]);
    if (personalHarnessInputRef.current) {
      personalHarnessInputRef.current.value = "";
    }
  }, []);

  const createPersonalHarness = useCallback(async () => {
    setCreatingPersonalHarness(true);
    setError("");
    try {
      const res = await fetch(`${API}/api/extensions/personal-harness`, {
        method: "POST",
        credentials: "include",
        headers: personalHarnessFiles.length > 0 ? { "Content-Type": "application/json" } : undefined,
        body:
          personalHarnessFiles.length > 0
            ? JSON.stringify({ instruction_files: personalHarnessFiles })
            : undefined,
      });
      if (!res.ok) {
        let detail = "";
        try {
          const payload = await res.json();
          detail = typeof payload.detail === "string" ? payload.detail : "";
        } catch {
          detail = await res.text();
        }
        throw new Error(detail || t("settings.extensionsPersonalHarnessFailed"));
      }
      await refresh();
      clearPersonalHarnessFiles();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("settings.extensionsPersonalHarnessFailed"));
    } finally {
      setCreatingPersonalHarness(false);
    }
  }, [clearPersonalHarnessFiles, personalHarnessFiles, refresh, t]);

  const normalizedSearch = search.trim().toLowerCase();
  const visibleRows = useMemo(() => {
    if (!normalizedSearch) return rows;
    return rows.filter((row) =>
      [row.name, row.id, row.description].some((value) => value.toLowerCase().includes(normalizedSearch)),
    );
  }, [normalizedSearch, rows]);

  if (loading) return <div className="settings-hint">…</div>;
  if (!rows.length) return <div className="settings-hint">{t("settings.extensionsNone")}</div>;

  return (
    <div className="extension-ui-settings">
      <div className="extension-ui-settings-toolbar">
        <input
          ref={personalHarnessInputRef}
          className="extension-ui-settings-personal-harness-input"
          type="file"
          multiple
          accept=".md,.markdown,.txt"
          onChange={(event) => {
            void loadPersonalHarnessFiles(event.currentTarget.files);
          }}
        />
        <button
          type="button"
          className="btn-secondary extension-ui-settings-personal-harness"
          disabled={creatingPersonalHarness}
          onClick={() => personalHarnessInputRef.current?.click()}
        >
          <Icon name="folder-plus" size={13} />
          {t("settings.extensionsPersonalHarnessAddFiles")}
        </button>
        {personalHarnessFiles.length > 0 && (
          <div className="extension-ui-settings-personal-harness-files">
            <span>
              {t("settings.extensionsPersonalHarnessFilesSelected", {
                count: personalHarnessFiles.length,
              })}
            </span>
            <button
              type="button"
              className="icon-btn"
              aria-label={t("settings.extensionsPersonalHarnessClearFiles")}
              title={t("settings.extensionsPersonalHarnessClearFiles")}
              disabled={creatingPersonalHarness}
              onClick={clearPersonalHarnessFiles}
            >
              <Icon name="x" size={13} />
            </button>
          </div>
        )}
        <button
          type="button"
          className="btn-secondary extension-ui-settings-personal-harness"
          disabled={creatingPersonalHarness}
          onClick={() => void createPersonalHarness()}
        >
          <Icon name="folder-plus" size={13} />
          {creatingPersonalHarness ? t("settings.extensionsPersonalHarnessCreating") : t("settings.extensionsPersonalHarnessCreate")}
        </button>
      </div>
      <label className="extension-ui-settings-search">
        <Icon name="search" size={14} />
        <SearchInput
          className="extension-ui-settings-search-input"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t("settings.extensionsSearchPlaceholder")}
          aria-label={t("settings.extensionsSearchPlaceholder")}
        />
      </label>
      {error && <div className="settings-error">{error}</div>}
      {!visibleRows.length && <div className="settings-hint extension-ui-settings-empty-search">{t("settings.extensionsSearchEmpty")}</div>}
      {visibleRows.map((row) => (
        <div key={row.id} className={`extension-ui-settings-row${row.enabled ? "" : " is-disabled"}`}>
          <div className="extension-ui-settings-header">
            <div className="extension-ui-settings-title">
              <div className="extension-ui-settings-name">{row.name}</div>
              <div className="extension-ui-settings-id">{row.id}</div>
              {row.description && (
                <div className="extension-ui-settings-description">{row.description}</div>
              )}
            </div>
            <div className="extension-ui-settings-header-actions">
              {availableUpdates[row.id] && (
                <>
                  <span className="extension-ui-settings-update-badge">
                    {availableUpdates[row.id].availableVersion
                      ? t("settings.extensionsUpdateAvailableVersion", {
                          version: availableUpdates[row.id].availableVersion,
                        })
                      : t("settings.extensionsUpdateAvailable")}
                  </span>
                  <button
                    type="button"
                    className="btn-secondary extension-ui-settings-update"
                    disabled={updatingIds.has(row.id)}
                    onClick={() => void updateExtension(row.id)}
                  >
                    <Icon name="refresh" size={13} />
                    {updatingIds.has(row.id)
                      ? t("settings.extensionsUpdating")
                      : t("settings.extensionsUpdate")}
                  </button>
                </>
              )}
              <label className="extension-ui-settings-toggle extension-ui-settings-main-toggle">
                <input
                  type="checkbox"
                  checked={row.enabled}
                  disabled={row.required}
                  onChange={(e) => toggleExtension(row.id, e.target.checked)}
                />
                {row.enabled ? t("settings.extensionsEnabled") : t("settings.extensionsDisabled")}
              </label>
              {!row.required && (
                <button
                  type="button"
                  className="btn-danger extension-ui-settings-uninstall"
                  disabled={deletingIds.has(row.id)}
                  onClick={() => void uninstallExtension(row.id, row.name)}
                >
                  <Icon name="trash" size={13} />
                  {deletingIds.has(row.id) ? t("settings.extensionsUninstalling") : t("settings.extensionsUninstall")}
                </button>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function ProvidersList({
  providers,
  activeId,
  busy,
  error,
  onClose,
  onRefreshApp,
  refreshAppDisabled,
  hookActionContext,
  onAdd,
  onMobile,
  mobileEnabled,
  integrationsEnabled,
  onEdit,
  onActivate,
  onSuspend,
  credentialRetryingId,
  onRetryCredential,
  onDelete,
  setupStatuses,
  projects,
  firstRunDone,
  networkBindAddress,
  teamEnabled,
  credentialBrokerEnabled,
  onEditHarnessDescriptionFile,
  section,
  onSectionChange,
  onAddProject,
  onInstallProvider,
  installRuns,
  onVerifyProviders,
  onNetworkBindChange,
}: ProvidersListProps) {
  const { t } = useTranslation();
  // Mobile only: whether the section list or the selected section's content
  // is the visible pane. No-op on desktop, where both panes render together.
  const [mobileNavOpen, setMobileNavOpen] = useState(true);
  const handleSectionSelect = useCallback(
    (id: SettingsSection) => {
      onSectionChange(id);
      setMobileNavOpen(false);
    },
    [onSectionChange],
  );
  const extensionSettingsModules = useExtensionFrontendModules("settings");
  const extensionSettingsBySection = useMemo(() => {
    const items = new Map<SettingsSection, ExtensionFrontendModule>();
    for (const item of extensionSettingsModules) {
      items.set(`extension:${item.extension_id}:${item.id}`, item);
    }
    return items;
  }, [extensionSettingsModules]);
  const extensionSettingsSection = extensionSettingsBySection.get(section);
  const isNative = Capacitor.isNativePlatform();
  const { sections: extensionAppSettings, refresh: refreshExtensionAppSettings } =
    useExtensionAppSettings();
  type SettingsGroup = "general" | "harness";
  const coreSections: { id: SettingsSection; label: string; group: SettingsGroup }[] = [
    { id: "providers", label: t("setup.providersTitle"), group: "general" },
    { id: "account", label: t("settings.accountTitle"), group: "general" },
    { id: "language", label: t("language.label"), group: "general" },
    { id: "appearance", label: t("settings.appearanceTitle"), group: "general" },
    { id: "desktop", label: t("settings.desktopTitle"), group: "general" },
    { id: "recovery", label: t("settings.recoveryTitle"), group: "general" },
    { id: "shortcuts", label: t("settings.shortcutsTitle"), group: "general" },
    ...(teamEnabled ? [{ id: "delegation" as const, label: t("settings.delegationTitle"), group: "general" as const }] : []),
    { id: "context", label: t("settings.contextTitle"), group: "general" },
    { id: "internalLlm", label: t("settings.internalLlmTitle"), group: "general" },
    { id: "sessions", label: t("settings.sessionsTitle"), group: "general" },
    { id: "voice", label: t("settings.voiceTitle"), group: "general" },
    ...(credentialBrokerEnabled ? [{ id: "passwords" as const, label: t("settings.passwordManager"), group: "general" as const }] : []),
    ...(isNative ? [{ id: "server" as const, label: t("settings.serverTitle"), group: "general" as const }] : []),
    ...(isNative ? [{ id: "notifications" as const, label: t("settings.mobileNotificationsTitle"), group: "general" as const }] : []),
    ...(integrationsEnabled ? [{ id: "extensions" as const, label: t("settings.extensionsTitle"), group: "harness" as const }] : []),
    { id: "capabilities", label: t("settings.capabilitiesTitle"), group: "harness" },
    { id: "harnessProfiles", label: t("settings.harnessProfilesSection"), group: "harness" },
    ...extensionSettingsModules.map((item) => ({
      id: `extension:${item.extension_id}:${item.id}` as const,
      label: item.label,
      group: "general" as const,
    })),
  ];
  // An extension section whose id matches a core section merges into it; the
  // rest each get their own nav entry.
  const extensionAppSettingsSection = useMemo(() => {
    const id = section.startsWith(EXT_SETTINGS_PREFIX)
      ? section.slice(EXT_SETTINGS_PREFIX.length)
      : section;
    return (extensionAppSettings ?? []).find((item) => item.id === id) ?? null;
  }, [extensionAppSettings, section]);
  const presentSectionIds = new Set(coreSections.map((item) => item.id));
  const sections = [
    ...coreSections,
    ...(extensionAppSettings ?? [])
      .map((item) => ({
        id: navIdForExtensionSection(item.id),
        label: item.label,
        group: "general" as const,
      }))
      .filter((item) => !presentSectionIds.has(item.id)),
  ];
  useEffect(() => {
    if (section.startsWith("extension:") && !extensionSettingsSection) {
      onSectionChange("providers");
    }
  }, [extensionSettingsSection, onSectionChange, section]);
  useEffect(() => {
    if (
      section.startsWith(EXT_SETTINGS_PREFIX)
      && extensionAppSettings
      && !extensionAppSettingsSection
    ) {
      onSectionChange("providers");
    }
  }, [extensionAppSettings, extensionAppSettingsSection, onSectionChange, section]);
  const body = (
    <>
      {section === "providers" && (
        <ProvidersSettingsSection
          providers={providers}
          activeId={activeId}
          busy={busy}
          error={error}
          onAdd={onAdd}
          onEdit={onEdit}
          onActivate={onActivate}
          onSuspend={onSuspend}
          credentialRetryingId={credentialRetryingId}
          onRetryCredential={onRetryCredential}
          onDelete={onDelete}
          onRefreshApp={onRefreshApp}
          refreshAppDisabled={refreshAppDisabled}
          setupStatuses={setupStatuses}
          projects={projects}
          firstRunDone={firstRunDone}
          networkBindAddress={networkBindAddress}
          credentialBrokerEnabled={credentialBrokerEnabled}
          onAddProject={onAddProject}
          onInstallProvider={onInstallProvider}
          installRuns={installRuns}
          onVerifyProviders={onVerifyProviders}
          onNetworkBindChange={onNetworkBindChange}
        />
      )}
      {section === "language" && (
        <div className="language-setting">
          <label>{t('language.label')}</label>
          <LanguageSelector />
        </div>
      )}
      {section === "account" && (
        <>
          <UserDisplayNameSetting />
          <div className="setup-divider" />
          <AuthCredentialsSetting />
        </>
      )}
      {section === "appearance" && <AppearanceSetting />}
      {section === "desktop" && (
        <>
          <DesktopAppSettingsSection />
          <div className="setup-divider" />
          <TaskStartSilenceSetting />
        </>
      )}
      {section === "recovery" && <BasCompanionAppsSetting />}
      {section === "shortcuts" && <ShortcutSettings />}
      {section === "delegation" && teamEnabled && (
        <>
          <CrossSessionDelegateSetting />
          <div className="setup-divider" />
          <DelegateTaskPolicySetting />
        </>
      )}
      {section === "context" && <ContextStrategySetting />}
      {section === "internalLlm" && <InternalLLMSetting />}
      {section === "sessions" && (
        <>
          <SessionTabsSettings />
          <div className="setup-divider" />
          <SessionAutoDeleteSetting />
          <div className="setup-divider" />
          <RecursionGuardsSettings />
          <div className="setup-divider" />
          <NativeImportSetting />
        </>
      )}
      {section === "voice" && <VoiceSettings />}
      {section === "extensions" && <ExtensionUiSettingsSection />}
      {section === "capabilities" && (
        <InstallationCapabilities onRestartRequested={onRefreshApp} />
      )}
      {section === "harnessProfiles" && (
        <Suspense
          fallback={
            <div className="harness-settings-editor">
              {t("common.loading", "Loading…")}
            </div>
          }
        >
          <HarnessSettingsEditor onEditDescriptionFile={onEditHarnessDescriptionFile} />
        </Suspense>
      )}
      {section === "passwords" && credentialBrokerEnabled && <PasswordManagerSetting />}
      {section === "server" && isNative && <ServerSetting />}
      {section === "notifications" && isNative && <MobileNotificationSettings />}
      {extensionSettingsSection && <ExtensionModuleSlot module={extensionSettingsSection} />}
      {extensionAppSettingsSection && (
        <ExtensionAppSettingsSection
          section={extensionAppSettingsSection}
          onSaved={refreshExtensionAppSettings}
        />
      )}
    </>
  );

  return (
    <>
      <div className="settings-page-header">
        <div className="settings-page-title">
          <h2>{t("settings.title")}</h2>
          <span>{sections.find((item) => item.id === section)?.label}</span>
        </div>
        <div className="settings-page-actions">
          {integrationsEnabled && (
            <ExtensionQuickButtons context={hookActionContext} variant="topbar" placement="settings" />
          )}
          {onRefreshApp && (
            <button
              type="button"
              className="btn-secondary settings-page-refresh-action"
              onClick={onRefreshApp}
              disabled={refreshAppDisabled}
            >
              {refreshAppDisabled ? "..." : <Icon name="refresh" size={14} style={{ verticalAlign: "-2px" }} />} {t("app.refreshButtonTitle")}
            </button>
          )}
          {mobileEnabled && (
            <button type="button" className="btn-secondary settings-page-mobile-action" onClick={onMobile}>
              {t("mobileSetup.title")}
            </button>
          )}
          <button className="setup-cancel-btn settings-page-close-action" onClick={onClose}>
            {t("machines.back")}
          </button>
        </div>
      </div>
      <div className={`settings-page-layout${mobileNavOpen ? "" : " settings-page-layout--mobile-detail"}`}>
        <nav className="settings-page-nav" aria-label={t("settings.title")}>
          {(["general", "harness"] as const).map((group) => {
            const groupSections = sections.filter((item) => item.group === group);
            if (groupSections.length === 0) return null;
            return (
              <div className="settings-page-nav-group" key={group}>
                <div className="settings-page-nav-group-header">
                  {group === "general" ? t("settings.generalTab") : t("settings.harnessTab")}
                </div>
                {groupSections.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    data-testid={`settings-nav-${item.id}`}
                    className={item.id === section ? "active" : ""}
                    aria-current={item.id === section ? "page" : undefined}
                    onClick={() => handleSectionSelect(item.id)}
                  >
                    <span className="settings-page-nav-button-label">{item.label}</span>
                    <Icon name="chevron-right" size={14} className="settings-page-nav-button-chevron" />
                  </button>
                ))}
              </div>
            );
          })}
        </nav>
        <div className="settings-page-content">
          <button
            type="button"
            className="settings-page-content-back"
            onClick={() => setMobileNavOpen(true)}
          >
            <Icon name="chevron-left" size={14} />
            {t("settings.backToList")}
          </button>
          {body}
          {section === "providers" && (
            <div className="settings-page-provider-actions">
              <button className="setup-save-btn" onClick={onAdd} disabled={busy}>
                {t('setup.addProvider')}
              </button>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

function DesktopAppSettingsSection() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<DesktopStatus | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch(`${API}/api/desktop/status`, { credentials: "include" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const payload = (await res.json()) as DesktopStatus;
        if (!cancelled) {
          setStatus(payload);
          setError("");
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : t("settings.desktopStatusFailed"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [t]);

  const platforms: DesktopInstallPlatform[] = ["macos", "windows"];
  return (
    <div className="desktop-app-setting">
      <div className="desktop-app-setting-header">
        <div>
          <h3>{t("settings.desktopTitle")}</h3>
          <p>{t("settings.desktopSubtitle")}</p>
        </div>
        {status?.version && <span>{status.version}</span>}
      </div>
      {error && <div className="settings-error">{error}</div>}
      <div className="desktop-app-downloads">
        {platforms.map((platform) => {
          const available = Boolean(status?.[platform]);
          return (
            <a
              key={platform}
              className={`desktop-app-download ${available ? "" : "disabled"}`}
              href={available ? desktopDownloadUrl(platform) : undefined}
              aria-disabled={!available}
              onClick={(e) => {
                if (!available) e.preventDefault();
              }}
            >
              <Icon name="archive" size={18} />
              <span>{t("settings.desktopDownloadPlatform", { platform: desktopPlatformLabel(platform) })}</span>
              <small>
                {available ? t("settings.desktopAvailable") : t("settings.desktopUnavailable")}
              </small>
            </a>
          );
        })}
      </div>
    </div>
  );
}

function ProvidersSettingsSection({
  providers,
  activeId,
  busy,
  error,
  onAdd,
  onEdit,
  onActivate,
  onSuspend,
  credentialRetryingId,
  onRetryCredential,
  onDelete,
  setupStatuses,
  projects,
  firstRunDone,
  networkBindAddress,
  credentialBrokerEnabled,
  onAddProject,
  onInstallProvider,
  installRuns,
  onVerifyProviders,
  onNetworkBindChange,
  onRefreshApp,
  refreshAppDisabled,
}: Omit<
  ProvidersListProps,
  | "onClose"
  | "onMobile"
  | "mobileEnabled"
  | "integrationsEnabled"
  | "teamEnabled"
  | "section"
  | "onSectionChange"
  | "hookActionContext"
>) {
  const { t } = useTranslation();
  const quotaStatus = useQuotaStatus(API, providers);
  // Desktop-only OAuth login: the provider CLI opens the OS browser and
  // binds a localhost callback, so the user's browser must share the
  // machine with the backend. Loopback access is the accurate signal.
  const loginEnabled =
    typeof window !== "undefined" &&
    ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);
  const [loginPendingId, setLoginPendingId] = useState<string | null>(null);
  const [loginError, setLoginError] = useState<{ id: string; message: string } | null>(null);
  const runLoginAction = useCallback(
    async (p: Provider, action: "login" | "logout") => {
      setLoginPendingId(p.id);
      setLoginError(null);
      try {
        const r = await fetch(`${API}/api/providers/${p.id}/${action}`, { method: "POST" });
        if (!r.ok) {
          let message = `${action} failed`;
          try {
            const body = await r.json();
            if (body?.detail) message = String(body.detail);
          } catch {
            /* keep default */
          }
          setLoginError({ id: p.id, message });
        }
      } finally {
        setLoginPendingId(null);
      }
    },
    [],
  );
  const cancelLoginAction = useCallback(async (p: Provider) => {
    try {
      await fetch(`${API}/api/providers/${p.id}/login/cancel`, { method: "POST" });
    } catch {
      /* best-effort cancel */
    }
  }, []);
  return (
    <>
      {!firstRunDone && (
        <FirstRunWizard
          statuses={setupStatuses}
          providers={providers}
          projects={projects}
          networkBindAddress={networkBindAddress}
          onNetworkBindChange={onNetworkBindChange}
          onRefreshApp={onRefreshApp}
          refreshAppDisabled={refreshAppDisabled}
          busy={busy}
          credentialBrokerEnabled={credentialBrokerEnabled}
          onAddProject={onAddProject}
          onInstallProvider={onInstallProvider}
          installRuns={installRuns}
          onVerifyProviders={onVerifyProviders}
          onAdd={onAdd}
        />
      )}
      {firstRunDone && (
        <ProviderCliTools
          statuses={setupStatuses}
          busy={busy}
          onInstallProvider={onInstallProvider}
          installRuns={installRuns}
          onVerifyProviders={onVerifyProviders}
        />
      )}
      {providers.length === 0 && (
        <div className="setup-mode-desc">{t('setup.noProviders')}</div>
      )}
      <div className="provider-list">
        {providers.map((p) => {
          const isActive = p.id === activeId;
          const isSuspended = p.suspended === true;
          const isCredentialRetrying = p.id === credentialRetryingId;
          const credentialStatus = p.credential_status || (p.has_api_key ? "available" : "unknown");
          const loginState = p.login_state;
          const loginStatus = loginState?.status ?? "idle";
          const authenticated = loginState?.authenticated === true;
          const loginActionRunning =
            loginStatus === "login_running" ||
            loginStatus === "logout_running" ||
            loginPendingId === p.id;
          const loginSupported =
            loginEnabled && p.mode === "subscription" && (p.kind === "claude" || p.kind === "codex");
          const loginErrorMessage = loginError?.id === p.id ? loginError.message : null;
          return (
            <div key={p.id} data-testid={`provider-row-${p.kind}`} className={`provider-row ${isActive ? "active" : ""} ${isSuspended ? "suspended" : ""} credential-${credentialStatus}`}>
              <div className="provider-row-main" onClick={() => onEdit(p)}>
                <div className="provider-row-name">
                  {p.name}
                  {providerNickname(p) && (
                    <span className="provider-nickname">{providerNickname(p)}</span>
                  )}
                  {isActive && (
                    <span className="provider-active-pill">{t('setup.default')}</span>
                  )}
                  {isSuspended && (
                    <span className="provider-suspended-pill">{t('setup.suspended')}</span>
                  )}
                  {p.runner && (
                    <span
                      className={`provider-runner-pill runner-${p.runner}`}
                      title={t('setup.runnerHint')}
                    >
                      {t(runnerLabelKey(p.kind, p.runner), { defaultValue: p.runner })}
                    </span>
                  )}
                </div>
                <div className="provider-row-meta">
                  {p.mode === "subscription"
                    ? t('setup.subscriptionMode')
                    : `API key${
                        credentialStatus === "available"
                          ? ""
                          : ` — ${t(`setup.apiKeyStatus.${credentialStatus}`)}`
                      }${p.base_url ? ` · ${p.base_url}` : ""}`}
                </div>
                <QuotaIndicator status={providerQuotaStatus(quotaStatus, p)} />
              </div>
              <div className="provider-row-actions">
                {p.mode === "api_key" && credentialStatus === "blocked" && (
                  <>
                    <button
                      type="button"
                      className="btn-warning provider-credential-retry"
                      disabled={busy}
                      onClick={() => onRetryCredential(p)}
                    >
                      {isCredentialRetrying && <span className="retrying-spinner" aria-hidden="true" />}
                      {isCredentialRetrying
                        ? t('setup.apiKeyWaitingAccess')
                        : t('backendUnavailable.retry')}
                    </button>
                    <button
                      type="button"
                      className="btn-secondary"
                      disabled={busy}
                      onClick={() => onEdit(p)}
                    >
                      {t('setup.apiKeyReenter')}
                    </button>
                  </>
                )}
                {loginSupported && (
                  <>
                    {authenticated && loginStatus !== "login_running" ? (
                      <button
                        type="button"
                        className="btn-secondary provider-login-action"
                        disabled={busy || loginActionRunning}
                        onClick={() => runLoginAction(p, "logout")}
                      >
                        {loginStatus === "logout_running" || loginPendingId === p.id
                          ? t('setup.signingOut')
                          : t('setup.logOut')}
                      </button>
                    ) : (
                      <button
                        type="button"
                        className={`provider-login-action ${
                          loginStatus === "login_failed" || loginErrorMessage ? "btn-warning" : "btn-secondary"
                        }`}
                        disabled={busy || loginActionRunning}
                        onClick={() => runLoginAction(p, "login")}
                        title={loginErrorMessage || (loginStatus === "login_failed" ? loginState?.message : undefined)}
                      >
                        {loginActionRunning && (
                          <span className="retrying-spinner" aria-hidden="true" />
                        )}
                        {loginActionRunning
                          ? t('setup.signingIn')
                          : loginStatus === "login_failed"
                            ? t('setup.retryLogin')
                            : t('setup.logIn')}
                      </button>
                    )}
                    {loginActionRunning && (
                      <button
                        type="button"
                        className="btn-secondary provider-login-cancel"
                        onClick={() => cancelLoginAction(p)}
                        title={t('setup.cancelLogin')}
                      >
                        {t('setup.cancelLogin')}
                      </button>
                    )}
                  </>
                )}
                {!isActive && !isSuspended && (
                  <button
                    type="button"
                    className="btn-secondary"
                    disabled={busy}
                    onClick={() => onActivate(p)}
                  >
                    {t('setup.setDefaultButton')}
                  </button>
                )}
                <button
                  type="button"
                  className={isSuspended ? "btn-secondary" : "btn-warning"}
                  disabled={busy}
                  onClick={() => onSuspend(p, !isSuspended)}
                >
                  {isSuspended ? t('setup.resumeProvider') : t('setup.suspendProvider')}
                </button>
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={busy}
                  onClick={() => onEdit(p)}
                >
                  {t('setup.editButton')}
                </button>
                {!isActive && (
                  <button
                    type="button"
                    className="btn-danger"
                    disabled={busy}
                    onClick={() => onDelete(p)}
                  >
                    {t('setup.deleteButton')}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
      {error && <div className="setup-error">{error}</div>}
    </>
  );
}

function ProviderCliTools({
  statuses,
  busy,
  onInstallProvider,
  installRuns,
  onVerifyProviders,
}: {
  statuses: ProviderSetupStatus[];
  busy: boolean;
  onInstallProvider: (kind: InstallableProviderKind) => void;
  installRuns: Record<string, InstallRun>;
  onVerifyProviders: () => void;
}) {
  const { t } = useTranslation();
  if (statuses.length === 0) return null;
  return (
    <section className="provider-cli-tools">
      <div className="provider-cli-tools-header">
        <div>
          <h3>{t("setup.providerCliToolsTitle")}</h3>
          <p>{t("setup.providerCliToolsSubtitle")}</p>
        </div>
        <button type="button" className="btn-secondary" disabled={busy} onClick={onVerifyProviders}>
          {t("setup.verifyButton")}
        </button>
      </div>
      <ProviderCliToolGrid
        statuses={statuses}
        busy={busy}
        onInstallProvider={onInstallProvider}
        installRuns={installRuns}
      />
    </section>
  );
}

const TERMINAL_OPEN_STATES = new Set(["running", "succeeded", "failed"]);

function InstallTerminal({ run }: { run: InstallRun }) {
  const { t } = useTranslation();
  const bodyRef = useRef<HTMLDivElement>(null);
  const lines = run.lines;
  useEffect(() => {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length]);
  const stateLabel =
    run.state === "running"
      ? t("setup.installing")
      : run.state === "succeeded"
        ? t("setup.installSucceeded")
        : t("setup.installFailed");
  return (
    <div className={`provider-install-terminal ${run.state}`}>
      <div className="provider-install-terminal-header">
        <span className={`provider-install-state ${run.state}`}>{stateLabel}</span>
      </div>
      <div className="provider-install-terminal-body" ref={bodyRef}>
        {lines.length === 0 ? (
          <span className="provider-install-terminal-empty">{t("setup.installing")}</span>
        ) : (
          lines.map((line, i) => (
            <div key={i} className={`terminal-line ${line.s}`}>
              {line.t || " "}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

function ProviderCliToolGrid({
  statuses,
  busy,
  onInstallProvider,
  installRuns,
}: {
  statuses: ProviderSetupStatus[];
  busy: boolean;
  onInstallProvider: (kind: InstallableProviderKind) => void;
  installRuns: Record<string, InstallRun>;
}) {
  const { t } = useTranslation();
  return (
    <div className="first-run-provider-grid">
      {statuses.map((item) => {
        const run = installRuns[item.kind];
        const running = run?.state === "running";
        const showTerminal = run && TERMINAL_OPEN_STATES.has(run.state);
        const prerequisiteBlocksInstall = !item.prerequisite.ok && !item.prerequisite_installable;
        return (
          <div key={item.kind} className={`first-run-provider ${item.installed ? "ready" : ""}`}>
            <div className="first-run-provider-main">
              <strong>{item.label}</strong>
              <span>{item.installed ? t("setup.cliInstalled") : t("setup.cliMissing", { command: item.command })}</span>
            </div>
            <code>{item.install_command.join(" ")}</code>
            {!item.prerequisite.ok && (
              <span className="setup-field-hint">{t("setup.prerequisiteMissing", { command: item.prerequisite_command })}</span>
            )}
            {showTerminal && <InstallTerminal run={run} />}
            <button
              type="button"
              className={item.installed ? "btn-secondary" : "setup-save-btn"}
              disabled={running || busy || prerequisiteBlocksInstall}
              onClick={() => onInstallProvider(item.kind)}
            >
              {running
                ? t("setup.installing")
                : item.installed
                  ? t("setup.updateButton")
                  : t("setup.installButton")}
            </button>
          </div>
        );
      })}
    </div>
  );
}

function FirstRunWizard({
  statuses,
  providers,
  projects,
  networkBindAddress,
  busy,
  credentialBrokerEnabled,
  onAddProject,
  onInstallProvider,
  installRuns,
  onVerifyProviders,
  onNetworkBindChange,
  onRefreshApp,
  refreshAppDisabled,
  onAdd,
}: {
  statuses: ProviderSetupStatus[];
  providers: Provider[];
  projects: Project[];
  networkBindAddress: NetworkBindAddress;
  busy: boolean;
  credentialBrokerEnabled: boolean;
  onAddProject: (path: string) => void;
  onInstallProvider: (kind: InstallableProviderKind) => void;
  installRuns: Record<string, InstallRun>;
  onVerifyProviders: () => void;
  onNetworkBindChange: (address: NetworkBindAddress) => void;
  onRefreshApp?: () => void;
  refreshAppDisabled: boolean;
  onAdd: () => void;
}) {
  const { t } = useTranslation();
  const [projectPath, setProjectPath] = useState("");
  const hasProvider = providers.length > 0;
  const hasProject = projects.length > 0;

  return (
    <section className="first-run-wizard">
      <div className="first-run-wizard-header">
        <div>
          <h3>{t("setup.firstRunTitle")}</h3>
          <p>{t("setup.firstRunSubtitle")}</p>
        </div>
        <button type="button" className="btn-secondary" disabled={busy} onClick={onVerifyProviders}>
          {t("setup.verifyButton")}
        </button>
      </div>
      <ProviderCliToolGrid
        statuses={statuses}
        busy={busy}
        onInstallProvider={onInstallProvider}
        installRuns={installRuns}
      />
      <div className="first-run-step">
        <div className="first-run-step-copy">
          <strong>{t("setup.projectsStepTitle")}</strong>
          <span>{hasProject ? t("setup.projectsConfigured", { count: projects.length }) : t("setup.projectsMissing")}</span>
        </div>
        <div className="first-run-inline-form">
          <input
            type="text"
            value={projectPath}
            onChange={(e) => setProjectPath(e.target.value)}
            placeholder={t("setup.projectPathPlaceholder")}
            spellCheck={false}
          />
          <button
            type="button"
            className="btn-secondary"
            disabled={busy || !projectPath.trim()}
            onClick={() => {
              onAddProject(projectPath.trim());
              setProjectPath("");
            }}
          >
            {t("setup.addProjectButton")}
          </button>
        </div>
      </div>
      <div className="first-run-step">
        <NativeImportSetting />
      </div>
      <div className="first-run-step">
        <div className="first-run-step-copy">
          <strong>{t("setup.networkStepTitle")}</strong>
          <span>{t("setup.networkStepDescription")}</span>
          <span className="setup-field-hint">{t("setup.networkStepSecurity")}</span>
        </div>
        <div className="first-run-network-options" role="radiogroup" aria-label={t("setup.networkStepTitle")}>
          <label className={`first-run-network-option ${networkBindAddress === "127.0.0.1" ? "active" : ""}`}>
            <input
              type="radio"
              name="network-bind-address"
              aria-label={t("setup.networkLocalTitle")}
              checked={networkBindAddress === "127.0.0.1"}
              disabled={busy}
              onChange={() => onNetworkBindChange("127.0.0.1")}
            />
            <span>
              <strong>{t("setup.networkLocalTitle")}</strong>
              <small>{t("setup.networkLocalDescription")}</small>
              <code>127.0.0.1</code>
            </span>
          </label>
          <label className={`first-run-network-option ${networkBindAddress === "0.0.0.0" ? "active" : ""}`}>
            <input
              type="radio"
              name="network-bind-address"
              aria-label={t("setup.networkLanTitle")}
              checked={networkBindAddress === "0.0.0.0"}
              disabled={busy}
              onChange={() => onNetworkBindChange("0.0.0.0")}
            />
            <span>
              <strong>{t("setup.networkLanTitle")}</strong>
              <small>{t("setup.networkLanDescription")}</small>
              <code>0.0.0.0</code>
            </span>
          </label>
          {onRefreshApp && (
            <button
              type="button"
              className="btn-secondary"
              disabled={refreshAppDisabled}
              onClick={onRefreshApp}
            >
              {refreshAppDisabled ? "..." : <Icon name="refresh" size={14} style={{ verticalAlign: "-2px" }} />} {t("setup.applyNetworkRestart")}
            </button>
          )}
        </div>
      </div>
      {credentialBrokerEnabled && (
        <div className="first-run-step">
          <div className="first-run-step-copy">
            <strong>{t("setup.passwordsStepTitle")}</strong>
            <span>{t("setup.passwordsStepDescription")}</span>
            <span className="setup-field-hint">{t("setup.passwordsStepSecurity")}</span>
          </div>
        </div>
      )}
      <div className="first-run-next">
        <span>
          {hasProvider
            ? t("setup.providerDefined")
            : t("setup.providerDefinitionMissing")}
        </span>
        <div>
          <button type="button" className="btn-secondary" disabled={busy} onClick={onAdd}>
            {t("setup.addProvider")}
          </button>
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Wizard: pick template
// ---------------------------------------------------------------------------

function WizardTemplates({
  onClose,
  onBack,
  onPick,
}: {
  onClose: () => void;
  onBack: () => void;
  onPick: (id: TemplateId) => void;
}) {
  const { t } = useTranslation();
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
  return (
    <>
      <div className="modal-header">
        <button className="modal-back" onClick={onBack} title={t('setup.backTitle')}>
          &larr;
        </button>
        <h2>{t('setup.newProviderTitle')}</h2>
        <button className="modal-close" onClick={onClose}>
          &times;
        </button>
      </div>
      <div className="modal-body">
        <p className="setup-mode-desc">{t('setup.pickTemplate')}</p>
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
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Provider form (used by both wizard create and edit)
// ---------------------------------------------------------------------------

interface FormPayload {
  name: string;
  nickname?: string;
  kind: string;
  mode: Provider["mode"];
  base_url: string;
  config_dir: string;
  default_model: string;
  runner: Provider["runner"];
  default_reasoning_effort: ReasoningEffort | "";
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

function runnerOptionsForKind(kind: string, saved?: Provider["runner_options"]): Provider["runner_options"] {
  if (saved?.length) return saved;
  // codex's better_agent_runner choice speaks OpenAI's Codex ResponsesAPI
  // directly over the ChatGPT-subscription OAuth credential (subscription
  // mode only — see the mode-forcing effect in ProviderForm below).
  if (kind === "fugu" || kind === "codex") return ["native", "better_agent_runner"];
  return kind === "openai" ? ["better_agent_runner"] : ["native"];
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

function ProviderForm({
  mode,
  providerId,
  initial,
  initialHasKey,
  credentialBlocked = false,
  onClose,
  onBack,
  onSubmit,
}: {
  mode: "create" | "edit";
  /** Set on edit only — used to fetch this provider's model list for
   * the default_model dropdown. Undefined during the create wizard
   * (provider doesn't exist yet → free-text input). */
  providerId?: string;
  initial: Omit<FormPayload, "api_key" | "default_permission" | "runner" | "suspended"> & {
    api_key?: string;
    capability_overrides?: Partial<Record<string, boolean>>;
    default_permission?: Permission;
    runner?: Provider["runner"];
    runner_options?: Provider["runner_options"];
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
  const runnerOptions = runnerOptionsForKind(kind, initial.runner_options);
  const initialRunner = initial.runner ?? runnerOptions[0];
  const [runner, setRunner] = useState<Provider["runner"]>(
    runnerOptions.includes(initialRunner) ? initialRunner : runnerOptions[0],
  );
  const runtimeKind = runtimeKindForRunner(kind, runner);
  const modes = availableModesForForm(runtimeKind, mode, initial.mode);
  const [mode_, setMode] = useState<Provider["mode"]>(
    modes.includes(initial.mode) ? initial.mode : modes[0],
  );
  const [baseUrl, setBaseUrl] = useState(initial.base_url);
  const [configDir, setConfigDir] = useState(initial.config_dir);
  const configDirCopy = configDirCopyForKind(kind);
  const apiEnvCopy = apiEnvCopyForKind(runtimeKind);
  const [defaultModel, setDefaultModel] = useState(initial.default_model);
  const effortOptions = effortOptionsForKind(kind);
  const initialEffort =
    initial.default_reasoning_effort && effortOptions.includes(initial.default_reasoning_effort)
      ? initial.default_reasoning_effort
      : defaultEffortForKind(kind);
  const [defaultReasoningEffort, setDefaultReasoningEffort] =
    useState<ReasoningEffort | "">(initialEffort);
  const permissionOptions = permissionOptionsForKind(runtimeKind);
  const seedPermission = (): Permission => {
    const opts = permissionOptions;
    const saved = initial.default_permission;
    const out: Permission = {};
    for (const axis of Object.keys(opts)) {
      const allowed = opts[axis];
      const v = saved?.[axis];
      out[axis] = v && allowed.includes(v) ? v : PERMISSION_DEFAULTS[runtimeKind]?.[axis] ?? allowed[0];
    }
    return out;
  };
  const [defaultPermission, setDefaultPermission] = useState<Permission>(seedPermission);
  const [apiKey, setApiKey] = useState(initial.api_key ?? "");
  const [suspended, setSuspended] = useState(initial.suspended === true);
  const [submitting, setSubmitting] = useState(false);
  const [customModelMode, setCustomModelMode] = useState(false);
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

  useEffect(() => {
    if (!modes.includes(mode_)) {
      setMode(modes[0]);
    }
    if (kind === "fugu" && runner === "better_agent_runner") {
      if (mode_ !== "api_key") setMode("api_key");
      if (!baseUrl) setBaseUrl(SAKANA_FUGU_API_BASE_URL);
      if (!defaultModel) setDefaultModel("fugu");
    }
    // codex's better_agent_runner choice is the ChatGPT-subscription
    // ResponsesAPI wire protocol — there is no API-key variant of it.
    if (kind === "codex" && runner === "better_agent_runner" && mode_ !== "subscription") {
      setMode("subscription");
    }
  }, [baseUrl, defaultModel, kind, mode_, modes, runner]);

  const updateRunner = (next: Provider["runner"]) => {
    setRunner(next);
    if (kind === "fugu" && next === "better_agent_runner") {
      setMode("api_key");
      if (!baseUrl) setBaseUrl(SAKANA_FUGU_API_BASE_URL);
      if (!defaultModel) setDefaultModel("fugu");
    }
    if (kind === "codex" && next === "better_agent_runner") {
      setMode("subscription");
    }
  };

  const {
    catalog,
    networkState,
    refresh,
    refreshing,
    refreshError,
  } = useProviderModelCatalog(
    mode === "edit" ? providerId || "" : "",
  );
  const catalogDefaultInvalid = (
    mode === "edit"
    && catalog?.authoritative === true
    && (
      catalog.status === "pending"
      || catalog.status === "unsupported"
      || catalog.status === "unavailable"
      || !catalog.models.includes(defaultModel)
    )
  );
  const modelOptions = mode === "edit" ? catalog?.models ?? null : null;

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
        default_model: defaultModel,
        runner,
        default_reasoning_effort: defaultReasoningEffort,
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

        {runnerOptions.length > 1 && (
          <div className="setup-field">
            <label>{t("setup.runnerLabel")}</label>
            <select
              value={runner}
              onChange={(e) => updateRunner(e.target.value as Provider["runner"])}
            >
              {runnerOptions.map((option) => (
                <option key={option} value={option}>
                  {t(runnerLabelKey(kind, option))}
                </option>
              ))}
            </select>
            <span className="setup-field-hint">{t("setup.runnerHint")}</span>
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

        {showConfigDirForKind(runtimeKind) && (
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

        <div className="setup-field">
          <label>{t('setup.defaultModelLabel')}</label>
          {mode === "edit" && modelOptions !== null && !customModelMode ? (
            <div style={{ display: "flex", gap: 4 }}>
              <Select
                value={
                  defaultModel && modelOptions.includes(defaultModel)
                    ? defaultModel
                    : ""
                }
                onChange={(v) => setDefaultModel(v)}
                options={[
                  ...(!modelOptions.includes(defaultModel)
                    ? [{
                        value: "",
                        label: defaultModel
                          ? t('setup.defaultModelNotInList', { model: defaultModel })
                          : t('setup.defaultModelSelectPlaceholder'),
                        disabled: true,
                      }]
                    : []),
                  ...modelOptions.map((m) => ({ value: m, label: m })),
                ]}
              />
              <button
                type="button"
                className="btn-icon"
                title="Type a custom model name"
                onClick={() => setCustomModelMode(true)}
              >
                +
              </button>
            </div>
          ) : (
            <div style={{ display: "flex", gap: 4 }}>
              <input
                type="text"
                value={defaultModel}
                onChange={(e) => setDefaultModel(e.target.value)}
                placeholder={t("setup.defaultModelCustomPlaceholder")}
                spellCheck={false}
              />
              {mode === "edit" && modelOptions !== null && (
                <button
                  type="button"
                  className="btn-icon"
                  title={t("setup.defaultModelPickFromList")}
                  onClick={() => setCustomModelMode(false)}
                >
                  <Icon name="check" size={18} />
                </button>
              )}
            </div>
          )}
          {mode === "edit" && modelOptions === null && (
            <span className="setup-field-hint">{t("model.catalogPending")}</span>
          )}
          {mode === "edit" ? (
            <ModelCatalogStatus
              catalog={catalog}
              networkState={networkState}
              onRefresh={refresh}
              refreshing={refreshing}
              refreshError={refreshError}
            />
          ) : null}
        </div>

        {effortOptions.length > 0 && (
          <div className="setup-field">
            <label>{t('setup.defaultReasoningEffortLabel')}</label>
            <Select
              value={defaultReasoningEffort}
              onChange={(v) => setDefaultReasoningEffort(v as ReasoningEffort)}
              options={effortOptions.map((effort) => ({
                value: effort,
                label: t(`reasoningEffort.${effort}`),
              }))}
            />
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
          disabled={
            submitting
            || (credentialBlocked && !apiKey)
            || catalogDefaultInvalid
          }
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

// ---------------------------------------------------------------------------
// Edit view (wraps ProviderForm + adds Activate/Delete)
// ---------------------------------------------------------------------------

function EditProvider({
  providers,
  providerId,
  activeId,
  busy,
  error,
  onClose,
  onBack,
  onSubmit,
  onActivate,
  onSuspend,
  onDelete,
}: {
  providers: Provider[];
  providerId: string;
  activeId: string | null;
  busy: boolean;
  error: string;
  onClose: () => void;
  onBack: () => void;
  onSubmit: (payload: FormPayload) => Promise<void>;
  onActivate: () => Promise<void>;
  onSuspend: (suspended: boolean) => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const provider = useMemo(
    () => providers.find((p) => p.id === providerId),
    [providers, providerId]
  );

  if (!provider) {
    return (
      <>
        <div className="modal-header">
          <button className="modal-back" onClick={onBack} title={t('setup.backTitle')}>
            &larr;
          </button>
          <h2>{t('setup.providerNotFound')}</h2>
          <button className="modal-close" onClick={onClose}>
            &times;
          </button>
        </div>
      </>
    );
  }

  const isActive = provider.id === activeId;

  return (
    <>
      <ProviderForm
        mode="edit"
        providerId={provider.id}
        initial={provider}
        initialHasKey={
          provider.credential_status !== "missing" && provider.credential_status !== "blocked"
        }
        credentialBlocked={provider.credential_status === "blocked"}
        onClose={onClose}
        onBack={onBack}
        onSubmit={onSubmit}
      />
      <div className="modal-body provider-edit-extra">
        {error && <div className="setup-error">{error}</div>}
        <div className="provider-edit-actions">
          {!isActive && !provider.suspended && (
            <button
              type="button"
              className="btn-secondary"
              disabled={busy}
              onClick={onActivate}
            >
              {t('setup.setDefaultButton')}
            </button>
          )}
          <button
            type="button"
            className={provider.suspended ? "btn-secondary" : "btn-warning"}
            disabled={busy}
            onClick={() => onSuspend(!provider.suspended)}
          >
            {provider.suspended ? t('setup.resumeProvider') : t('setup.suspendProvider')}
          </button>
          {!isActive && (
            <button
              type="button"
              className="btn-danger"
              disabled={busy}
              onClick={onDelete}
            >
              {t('setup.deleteProvider')}
            </button>
          )}
          {isActive && (
            <span className="setup-field-hint">
              {t('setup.defaultCannotDelete')}
            </span>
          )}
        </div>
      </div>
    </>
  );
}
