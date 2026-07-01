import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { Project, Provider, ProvidersState, ReasoningEffort, Permission } from "../types";
import { trackPromise } from "../progress/store";
import { ShortcutSettings } from "./ShortcutSettings";
import { CrossSessionDelegateSetting } from "./CrossSessionDelegateSetting";
import { AutoRestartOnIdleSetting } from "./AutoRestartOnIdleSetting";
import { ContextStrategySetting } from "./ContextStrategySetting";
import { SessionTabsSettings } from "./SessionTabsSettings";
import { VoiceSettings } from "./VoiceSettings";
import { SessionAutoDeleteSetting } from "./SessionAutoDeleteSetting";
import { NativeImportSetting } from "./NativeImportSetting";
import { DelegateTaskPolicySetting } from "./DelegateTaskPolicySetting";
import { InternalLLMSetting } from "./InternalLLMSetting";
import { SearchInput } from "./SearchInput";
import { LanguageSelector } from "./LanguageSelector";
import {
  availableModesForForm,
  apiEnvCopyForKind,
  showConfigDirForKind,
} from "./providerFormShape";
import { Select } from "./Select";
import { cacheProviders } from "../utils/providerCache";
import { useProviderInstalls, type InstallRun } from "../hooks/useProviderInstalls";
import { MobileSetup } from "./MobileSetup";
import { AppearanceSetting } from "./AppearanceSetting";
import { PasswordManagerSetting } from "./PasswordManagerSetting";
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

import { API } from "../api";

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
  teamEnabled?: boolean;
  credentialBrokerEnabled?: boolean;
  providerConfigSyncEnabled?: boolean;
  onOpenProviderConfigSync?: () => void;
}

type View =
  | { kind: "list" }
  | { kind: "edit"; providerId: string }
  | { kind: "wizard-templates" }
  | { kind: "wizard-form"; templateId: TemplateId }
  | { kind: "mobile" };

type TemplateId = "claude" | "codex" | "copilot" | "agy" | "fugu" | "sakana" | "ollama" | "zai" | "zai-openai" | "custom" | "custom-openai";
type InstallableProviderKind = "claude" | "codex" | "gemini" | "agy" | "copilot";
type SettingsSection =
  | "providers"
  | "language"
  | "appearance"
  | "desktop"
  | "shortcuts"
  | "delegation"
  | "context"
  | "internalLlm"
  | "sessions"
  | "voice"
  | "extensions"
  | "passwords"
  | `extension:${string}`;
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
  prerequisite: ProviderSetupCommandResult;
  installed: boolean;
  verify: ProviderSetupCommandResult;
  install?: ProviderSetupCommandResult | null;
}

interface ProviderConfigRepositoryStatus {
  enabled: boolean;
  auto_apply: boolean;
  remote_url: string;
  checkout_path: string;
  checkout_exists: boolean;
  last_synced_at?: string;
  last_error?: string;
  apply?: { updated: number; considered: number };
}

interface Template {
  id: TemplateId;
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
  if (kind === "gemini") {
    return {
      labelKey: "setup.configDirLabelGemini",
      placeholderKey: "setup.configDirPlaceholderGemini",
      hintKey: "setup.configDirHintGemini",
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

const TEMPLATES: Template[] = [
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
      default_model: "claude-opus-4-8[1m]",
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
      config_dir: "$HOME/.claude-zai",
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
];

const KEEP = "__keep__";
const PROVIDER_CONFIG_SYNC_API = `${API}/api/extensions/ofek-dev.provider-config-sync/backend`;

export function SettingsPage({
  onClose,
  onRefreshApp,
  refreshAppDisabled = false,
  teamEnabled = true,
  credentialBrokerEnabled = true,
  providerConfigSyncEnabled = true,
  onOpenProviderConfigSync,
}: Props) {
  const { t } = useTranslation();
  const [state, setState] = useState<ProvidersState | null>(null);
  const [setupStatuses, setSetupStatuses] = useState<ProviderSetupStatus[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [repoStatus, setRepoStatus] = useState<ProviderConfigRepositoryStatus | null>(null);
  const [firstRunDone, setFirstRunDone] = useState(true);
  const [networkBindAddress, setNetworkBindAddress] = useState<NetworkBindAddress>("127.0.0.1");
  const [view, setView] = useState<View>({ kind: "list" });
  const [section, setSection] = useState<SettingsSection>("providers");
  const [busy, setBusy] = useState(false);
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

  const refetchRepository = async () => {
    if (!providerConfigSyncEnabled) {
      setRepoStatus(null);
      return;
    }
    try {
      const { promise } = trackPromise("providerConfigRepo:status", async () => {
        const r = await fetch(`${PROVIDER_CONFIG_SYNC_API}/repository`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return (await r.json()) as ProviderConfigRepositoryStatus;
      });
      setRepoStatus(await promise);
    } catch (e) {
      setError(e instanceof Error ? e.message : "repository status failed");
    }
  };

  useEffect(() => {
    refetch();
    refetchSetupStatus();
    refetchPrefs();
    refetchProjects();
    refetchRepository();
  }, [providerConfigSyncEnabled]);

  useEffect(() => {
    const handler = () => refetch();
    window.addEventListener("provider_changed", handler);
    return () => window.removeEventListener("provider_changed", handler);
  }, []);

  useEffect(() => {
    if ((!teamEnabled && section === "delegation") || (!credentialBrokerEnabled && section === "passwords")) {
      setSection("providers");
    }
  }, [credentialBrokerEnabled, section, teamEnabled]);

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
          onAdd={() => setView({ kind: "wizard-templates" })}
          onMobile={() => setView({ kind: "mobile" })}
          onEdit={(p) => setView({ kind: "edit", providerId: p.id })}
          onOpenProviderConfigSync={onOpenProviderConfigSync}
          setupStatuses={setupStatuses}
          projects={projects}
          repoStatus={repoStatus}
          firstRunDone={firstRunDone}
          networkBindAddress={networkBindAddress}
          teamEnabled={teamEnabled}
          credentialBrokerEnabled={credentialBrokerEnabled}
          providerConfigSyncEnabled={providerConfigSyncEnabled}
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
          onInitConfigRepo={(remoteUrl) => runBusyAction(setBusy, setError, "repository init failed", async () => {
            await trackPromise("providerConfigRepo:init", async () => {
              const r = await fetch(`${PROVIDER_CONFIG_SYNC_API}/repository/init`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ remote_url: remoteUrl, auto_apply: true }),
              });
              if (!r.ok) throw new Error(await r.text());
            }).promise;
            await refetchRepository();
          })}
          onLoadConfigRepo={(remoteUrl) => runBusyAction(setBusy, setError, "repository load failed", async () => {
            await trackPromise("providerConfigRepo:load", async () => {
              const r = await fetch(`${PROVIDER_CONFIG_SYNC_API}/repository/load`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ remote_url: remoteUrl, auto_apply: true }),
              });
              if (!r.ok) throw new Error(await r.text());
            }).promise;
            await refetchRepository();
          })}
          onSyncConfigRepo={() => runBusyAction(setBusy, setError, "repository sync failed", async () => {
            await trackPromise("providerConfigRepo:sync", async () => {
              const r = await fetch(`${PROVIDER_CONFIG_SYNC_API}/repository/sync`, { method: "POST" });
              if (!r.ok) throw new Error(await r.text());
            }).promise;
            await refetchRepository();
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
              const r = await fetch(`${API}/api/providers/${p.id}/set-default`, { method: "POST" });
              if (!r.ok) throw new Error(await r.text());
            }).promise;
            await refetch();
          })}
          onSuspend={(p, suspended) => runBusyAction(setBusy, setError, suspended ? "suspend failed" : "resume failed", async () => {
            await trackPromise(`provider:suspend:${p.id}`, async () => {
              const r = await fetch(`${API}/api/providers/${p.id}/suspended`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ suspended }),
              });
              if (!r.ok) throw new Error(await r.text());
            }).promise;
            await refetch();
          })}
          onDelete={async (p) => {
            if (!confirm(t('setup.deleteConfirm'))) return;
            await runBusyAction(setBusy, setError, "delete failed", async () => {
              await trackPromise(`provider:delete:${p.id}`, async () => {
                const r = await fetch(`${API}/api/providers/${p.id}`, { method: "DELETE" });
                if (!r.ok) {
                  const t = await r.text();
                  throw new Error(t || "delete failed");
                }
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
                body: JSON.stringify(payload),
              });
              if (!r.ok) throw new Error(await r.text());
            }).promise;
            await refetch();
            setView({ kind: "list" });
          })}
          onActivate={() => runBusyAction(setBusy, setError, "activate failed", async () => {
            await trackPromise(`provider:activate:${view.providerId}`, async () => {
              const r = await fetch(`${API}/api/providers/${view.providerId}/set-default`, { method: "POST" });
              if (!r.ok) throw new Error(await r.text());
            }).promise;
            await refetch();
          })}
          onSuspend={(suspended) => runBusyAction(setBusy, setError, suspended ? "suspend failed" : "resume failed", async () => {
            await trackPromise(`provider:suspend:${view.providerId}`, async () => {
              const r = await fetch(`${API}/api/providers/${view.providerId}/suspended`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ suspended }),
              });
              if (!r.ok) throw new Error(await r.text());
            }).promise;
            await refetch();
          })}
          onDelete={async () => {
            if (!confirm(t('setup.deleteConfirm'))) return;
            await runBusyAction(setBusy, setError, "delete failed", async () => {
              await trackPromise(`provider:delete:${view.providerId}`, async () => {
                const r = await fetch(`${API}/api/providers/${view.providerId}`, { method: "DELETE" });
                if (!r.ok) {
                  const t = await r.text();
                  throw new Error(t || "delete failed");
                }
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
  onAdd: () => void;
  onMobile: () => void;
  onEdit: (p: Provider) => void;
  onActivate: (p: Provider) => void;
  onSuspend: (p: Provider, suspended: boolean) => void;
  onDelete: (p: Provider) => void;
  onOpenProviderConfigSync?: () => void;
  setupStatuses: ProviderSetupStatus[];
  projects: Project[];
  repoStatus: ProviderConfigRepositoryStatus | null;
  firstRunDone: boolean;
  networkBindAddress: NetworkBindAddress;
  teamEnabled: boolean;
  credentialBrokerEnabled: boolean;
  providerConfigSyncEnabled: boolean;
  section: SettingsSection;
  onSectionChange: (section: SettingsSection) => void;
  onAddProject: (path: string) => void;
  onInitConfigRepo: (remoteUrl: string) => void;
  onLoadConfigRepo: (remoteUrl: string) => void;
  onSyncConfigRepo: () => void;
  onInstallProvider: (kind: InstallableProviderKind) => void;
  installRuns: Record<string, InstallRun>;
  onVerifyProviders: () => void;
  onNetworkBindChange: (address: NetworkBindAddress) => void;
}

interface ExtensionListRecord {
  enabled?: boolean;
  manifest?: {
    id: string;
    entrypoints?: {
      instructions?: { name: string; level?: string }[];
      provider_capabilities?: { name: string; level?: string }[]; // legacy field name
      skills?: { name: string }[];
      mcp?: Array<string | { name?: string }>;
    };
  };
  instructions_enabled?: { global?: boolean; projects?: Record<string, boolean> };
}
interface SettingSpec {
  key: string;
  label: string;
  type: "string" | "number" | "boolean" | "secret";
  default?: unknown;
  enum?: unknown[];
  help?: string;
}
type ExtensionPermissionMode = "required" | "optional" | "scoped";

interface ExtensionPermissionView {
  name: string;
  mode: ExtensionPermissionMode;
  granted?: boolean;
  scope?: string[];
}

interface ExtensionRemoteService {
  name: string;
  base_url: string;
  purpose: string;
}

interface ExtensionHarnessAddition {
  kind: "instructions" | "skill" | "mcp" | string;
  name: string;
  detail?: string;
}

interface ExtensionPermissionsConfig {
  permissions?: {
    declared?: Record<string, unknown>;
    optional?: unknown[];
    grants?: Record<string, unknown>;
  };
}

interface ExtensionConfigRow {
  id: string;
  name: string;
  required: boolean;
  enabled: boolean;
  harnessDelivery: "native" | "runtime";
  hasQuickButton: boolean;
  hasPage: boolean;
  quickButtonEnabled: boolean;
  pageEnabled: boolean;
  harnessAdditions: ExtensionHarnessAddition[];
  internalLlmTasks: string[];
  userInstructions: string;
  mcp: Array<{ name: string; label: string; enabled: boolean }>;
  remoteServices: ExtensionRemoteService[];
  settingsSchema: SettingSpec[];
  settingsValues: Record<string, unknown>;
  secretPresent: Record<string, boolean>;
  instructionSections: { name: string; level: string }[];
  globalInstructionsEnabled: boolean;
  projectInstructionsEnabled: Record<string, boolean>;
  permissions: ExtensionPermissionView[];
}

const KNOWN_EXTENSION_PERMISSIONS = [
  "session_state",
  "spawn_runs",
  "internal_loopback",
  "filesystem",
  "network",
  "secrets",
  "provider_config",
  "backend_routes",
  "storage",
  "mutates_session_fields",
] as const;

const KNOWN_EXTENSION_PERMISSION_SET = new Set<string>(KNOWN_EXTENSION_PERMISSIONS);

function permissionTranslationKey(permission: string, field: "label" | "risk"): string {
  const key = KNOWN_EXTENSION_PERMISSION_SET.has(permission) ? permission : "unknown";
  return `settings.extensionsPermission.${key}.${field}`;
}

function buildPermissionViews(cfg: ExtensionPermissionsConfig): ExtensionPermissionView[] {
  const declared = cfg.permissions?.declared ?? {};
  const optional = new Set<string>(
    Array.isArray(cfg.permissions?.optional)
      ? cfg.permissions.optional.filter((name): name is string => typeof name === "string")
      : [],
  );
  const grants = cfg.permissions?.grants ?? {};
  const permissions: ExtensionPermissionView[] = [];
  for (const [name, value] of Object.entries(declared)) {
    if (value === false) continue;
    if (optional.has(name) || value === "optional") {
      permissions.push({ name, mode: "optional", granted: grants[name] === true });
      continue;
    }
    if (Array.isArray(value)) {
      permissions.push({
        name,
        mode: "scoped",
        scope: value.filter((part): part is string => typeof part === "string" && Boolean(part)),
      });
      continue;
    }
    permissions.push({ name, mode: "required" });
  }
  return permissions.sort((a, b) => a.name.localeCompare(b.name));
}

type ExtensionManifestEntryPoints = NonNullable<NonNullable<ExtensionListRecord["manifest"]>["entrypoints"]>;

function buildManifestHarnessAdditions(entrypoints: ExtensionManifestEntryPoints): ExtensionHarnessAddition[] {
  const additions: ExtensionHarnessAddition[] = [];
  for (const item of entrypoints.instructions ?? []) {
    if (item.name) additions.push({ kind: "instructions", name: item.name, detail: item.level === "project" ? "project" : "global" });
  }
  for (const item of entrypoints.provider_capabilities ?? []) {
    if (item.name) additions.push({ kind: "instructions", name: item.name, detail: item.level === "project" ? "project" : "global" });
  }
  for (const item of entrypoints.skills ?? []) {
    if (item.name) additions.push({ kind: "skill", name: item.name });
  }
  for (const item of entrypoints.mcp ?? []) {
    if (typeof item === "string") {
      additions.push({ kind: "mcp", name: item });
    } else if (item?.name) {
      additions.push({ kind: "mcp", name: item.name });
    }
  }
  return additions;
}

function ExtensionConfigGroup({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section className="extension-ui-settings-group">
      <div className="extension-ui-settings-group-header">
        <div className="extension-ui-settings-group-title">{title}</div>
        <div className="extension-ui-settings-group-description">{description}</div>
      </div>
      <div className="extension-ui-settings-group-body">{children}</div>
    </section>
  );
}

function ExtensionPermissionRow({
  permission,
  onToggle,
}: {
  permission: ExtensionPermissionView;
  onToggle: (permission: string, next: boolean) => void;
}) {
  const { t } = useTranslation();
  const modeLabel =
    permission.mode === "optional"
      ? permission.granted
        ? t("settings.extensionsPermissionMode.optionalOn")
        : t("settings.extensionsPermissionMode.optionalOff")
      : permission.mode === "scoped"
        ? t("settings.extensionsPermissionMode.scoped")
        : t("settings.extensionsPermissionMode.required");

  return (
    <div className="extension-ui-settings-permission">
      <div className="extension-ui-settings-permission-main">
        <div className="extension-ui-settings-permission-copy">
          <div className="extension-ui-settings-permission-title">
            {t(permissionTranslationKey(permission.name, "label"))}
          </div>
          <div className="extension-ui-settings-permission-risk">
            {t(permissionTranslationKey(permission.name, "risk"))}
          </div>
          {permission.scope && permission.scope.length > 0 && (
            <div className="extension-ui-settings-permission-scope">
              {t("settings.extensionsPermission.scope", { scope: permission.scope.join(", ") })}
            </div>
          )}
        </div>
        {permission.mode === "optional" ? (
          <label className="extension-ui-settings-permission-toggle">
            <input
              type="checkbox"
              checked={permission.granted === true}
              onChange={(e) => onToggle(permission.name, e.target.checked)}
            />
            {modeLabel}
          </label>
        ) : (
          <span className="extension-ui-settings-permission-mode">{modeLabel}</span>
        )}
      </div>
      <div className="extension-ui-settings-permission-key">{permission.name}</div>
    </div>
  );
}

/** Per-extension config: UI-surface toggles (quick button / page), per-MCP-
 *  server enable/disable, and declared settings. Secrets are write-only. */
export function ExtensionUiSettingsSection() {
  const { t } = useTranslation();
  const [rows, setRows] = useState<ExtensionConfigRow[]>([]);
  const [search, setSearch] = useState("");
  const [primaryProjects, setPrimaryProjects] = useState<{ path: string; name?: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [deletingIds, setDeletingIds] = useState<Set<string>>(() => new Set());

  const refresh = useCallback(async () => {
    try {
      const [listRes, projectsRes] = await Promise.all([
        fetch(`${API}/api/extensions?include_hidden=true`, { credentials: "include" }),
        fetch(`${API}/api/projects`, { credentials: "include" }),
      ]);
      const listData = await listRes.json();
      const records: ExtensionListRecord[] = Array.isArray(listData.extensions) ? listData.extensions : [];
      const projectsData = await projectsRes.json();
      const projectsList: { path: string; name?: string; node_id?: string }[] =
        Array.isArray(projectsData.projects) ? projectsData.projects : [];
      setPrimaryProjects(
        projectsList
          .filter((p) => (p.node_id || "primary") === "primary" && p.path)
          .map((p) => ({ path: p.path, name: p.name })),
      );
      const installed = records.filter((r) => r.manifest?.id);
      const configs: ExtensionConfigRow[] = [];
      for (const record of installed) {
        const id = record.manifest!.id;
        // Instruction sections: new "instructions" field, falling back to the
        // legacy "provider_capabilities" field (treated as global-level).
        const ep = record.manifest?.entrypoints ?? {};
        const instructionSections = [
          ...(ep.instructions ?? []),
          ...(ep.provider_capabilities ?? []).map((s) => ({
            name: s.name,
            level: s.level === "project" ? "project" : "global",
          })),
        ]
          .filter((s) => s.name)
          .map((s) => ({
            name: s.name,
            level: s.level === "project" ? "project" : "global",
          }));
        const instructionsEnabled = record.instructions_enabled ?? {};
        const manifestHarnessAdditions = buildManifestHarnessAdditions(ep);
        try {
          const res = await fetch(`${API}/api/extensions/${encodeURIComponent(id)}/config`, {
            credentials: "include",
          });
          if (!res.ok) continue;
          const cfg = await res.json();
          const row: ExtensionConfigRow = {
            id,
            name: cfg.name || id,
            required: cfg.required === true,
            enabled: record.enabled !== false,
            harnessDelivery: cfg.harness_delivery === "runtime" ? "runtime" : "native",
            hasQuickButton: Boolean(cfg.has_quick_button),
            hasPage: Boolean(cfg.has_page),
            quickButtonEnabled: cfg.ui?.quick_button_enabled !== false,
            pageEnabled: cfg.ui?.page_enabled !== false,
            harnessAdditions: Array.isArray(cfg.harness_additions) ? cfg.harness_additions : manifestHarnessAdditions,
            internalLlmTasks: Array.isArray(cfg.internal_llm_tasks)
              ? cfg.internal_llm_tasks.filter((task: unknown): task is string => typeof task === "string" && task.length > 0)
              : [],
            userInstructions: typeof cfg.user_instructions === "string" ? cfg.user_instructions : "",
            mcp: Array.isArray(cfg.mcp) ? cfg.mcp : [],
            remoteServices: Array.isArray(cfg.remote_services) ? cfg.remote_services : [],
            settingsSchema: Array.isArray(cfg.settings?.schema) ? cfg.settings.schema : [],
            settingsValues: cfg.settings?.values || {},
            secretPresent: cfg.settings?.secret_present || {},
            instructionSections,
            globalInstructionsEnabled: instructionsEnabled.global !== false,
            projectInstructionsEnabled: instructionsEnabled.projects ?? {},
            permissions: buildPermissionViews(cfg),
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

  useEffect(() => {
    void refresh();
  }, [refresh]);

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

  const toggleSurface = useCallback(
    (id: string, surface: "quick_button_enabled" | "page_enabled", next: boolean) => {
      setRows((prev) =>
        prev.map((r) =>
          r.id === id
            ? { ...r, [surface === "quick_button_enabled" ? "quickButtonEnabled" : "pageEnabled"]: next }
            : r,
        ),
      );
      void patch(`/api/extensions/${encodeURIComponent(id)}/ui-settings`, { [surface]: next });
    },
    [patch],
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

  const toggleMcp = useCallback(
    (id: string, server: string, next: boolean) => {
      setRows((prev) =>
        prev.map((r) =>
          r.id === id
            ? { ...r, mcp: r.mcp.map((s) => (s.name === server ? { ...s, enabled: next } : s)) }
            : r,
        ),
      );
      void patch(`/api/extensions/${encodeURIComponent(id)}/mcp/${encodeURIComponent(server)}/enabled`, {
        enabled: next,
      });
    },
    [patch],
  );

  const setHarnessDelivery = useCallback(
    (id: string, mode: "native" | "runtime") => {
      setRows((prev) => prev.map((r) => (r.id === id ? { ...r, harnessDelivery: mode } : r)));
      void patch(`/api/extensions/${encodeURIComponent(id)}/harness-delivery`, { mode });
    },
    [patch],
  );

  const toggleInstructions = useCallback(
    (id: string, level: "global" | "project", next: boolean, projectPath?: string) => {
      setRows((prev) =>
        prev.map((r) => {
          if (r.id !== id) return r;
          if (level === "global") return { ...r, globalInstructionsEnabled: next };
          const projects = { ...r.projectInstructionsEnabled };
          if (next && projectPath) projects[projectPath] = true;
          else if (projectPath) delete projects[projectPath];
          return { ...r, projectInstructionsEnabled: projects };
        }),
      );
      void patch(`/api/extensions/${encodeURIComponent(id)}/instructions/enabled`, {
        level,
        enabled: next,
        project_path: projectPath ?? "",
      });
    },
    [patch],
  );

  const togglePermission = useCallback(
    (id: string, permission: string, next: boolean) => {
      setRows((prev) =>
        prev.map((r) =>
          r.id === id
            ? {
                ...r,
                permissions: r.permissions.map((p) => (p.name === permission ? { ...p, granted: next } : p)),
              }
            : r,
        ),
      );
      void patch(`/api/extensions/${encodeURIComponent(id)}/permissions/${encodeURIComponent(permission)}/granted`, {
        granted: next,
      });
    },
    [patch],
  );

  const setSetting = useCallback(
    (id: string, key: string, value: unknown, isSecret: boolean) => {
      setRows((prev) =>
        prev.map((r) => {
          if (r.id !== id) return r;
          if (isSecret) {
            return { ...r, secretPresent: { ...r.secretPresent, [key]: Boolean(value) } };
          }
          return { ...r, settingsValues: { ...r.settingsValues, [key]: value } };
        }),
      );
      void patch(`/api/extensions/${encodeURIComponent(id)}/settings`, { key, value });
    },
    [patch],
  );

  const setUserInstructions = useCallback(
    (id: string, instructions: string) => {
      let previous = "";
      setRows((prev) =>
        prev.map((r) => {
          if (r.id !== id) return r;
          previous = r.userInstructions;
          return { ...r, userInstructions: instructions };
        }),
      );
      void patch(
        `/api/extensions/${encodeURIComponent(id)}/user-instructions`,
        { instructions },
        () => setRows((prev) => prev.map((r) => (r.id === id ? { ...r, userInstructions: previous } : r))),
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

  const normalizedSearch = search.trim().toLowerCase();
  const visibleRows = useMemo(() => {
    if (!normalizedSearch) return rows;
    return rows.filter((row) =>
      [
        row.name,
        row.id,
        ...row.mcp.flatMap((server) => [server.name, server.label]),
        ...row.harnessAdditions.flatMap((item) => [item.name, item.detail ?? ""]),
        ...row.remoteServices.flatMap((service) => [service.name, service.base_url, service.purpose]),
        ...row.settingsSchema.flatMap((spec) => [spec.key, spec.label, spec.help ?? ""]),
      ].some((value) => value.toLowerCase().includes(normalizedSearch)),
    );
  }, [normalizedSearch, rows]);

  if (loading) return <div className="settings-hint">…</div>;
  if (!rows.length) return <div className="settings-hint">{t("settings.extensionsNone")}</div>;

  return (
    <div className="extension-ui-settings">
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
            </div>
            <div className="extension-ui-settings-header-actions">
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
          <div className="extension-ui-settings-groups">
            <ExtensionConfigGroup
              title={t("settings.extensionsHarnessDelivery")}
              description={t("settings.extensionsHarnessDeliveryHelp")}
            >
              <label className="extension-ui-settings-select">
                <Select
                  value={row.harnessDelivery}
                  onChange={(v) => setHarnessDelivery(row.id, v === "runtime" ? "runtime" : "native")}
                  options={[
                    { value: "native", label: t("settings.extensionsHarnessDeliveryNative") },
                    { value: "runtime", label: t("settings.extensionsHarnessDeliveryRuntime") },
                  ]}
                />
              </label>
            </ExtensionConfigGroup>
            {row.harnessAdditions.length > 0 && (
              <ExtensionConfigGroup
                title={t("settings.extensionsHarnessAdditions")}
                description={t("settings.extensionsHarnessAdditionsHelp")}
              >
                <div className="extension-ui-settings-harness-additions">
                  {row.harnessAdditions.map((item) => (
                    <div key={`${item.kind}:${item.name}:${item.detail || ""}`} className="extension-ui-settings-harness-addition">
                      <span className="extension-ui-settings-harness-kind">
                        {t(`settings.extensionsHarnessKind.${item.kind}`, { defaultValue: item.kind })}
                      </span>
                      <span className="extension-ui-settings-harness-name">{item.name}</span>
                      {item.detail && <span className="extension-ui-settings-harness-detail">{item.detail}</span>}
                    </div>
                  ))}
                </div>
              </ExtensionConfigGroup>
            )}
            <ExtensionConfigGroup
              title={t("settings.extensionsUserInstructions")}
              description={t("settings.extensionsUserInstructionsHelp")}
            >
              <ExtensionUserInstructionsField
                value={row.userInstructions}
                placeholder={t("settings.extensionsUserInstructionsPlaceholder")}
                onSave={(text) => setUserInstructions(row.id, text)}
              />
            </ExtensionConfigGroup>
            {row.internalLlmTasks.length > 0 && (
              <ExtensionConfigGroup
                title={t("settings.internalLlmTitle")}
                description={t("settings.internalLlmHint")}
              >
                <InternalLLMSetting tasks={row.internalLlmTasks} showHint={false} />
              </ExtensionConfigGroup>
            )}
            {(row.hasQuickButton || row.hasPage) && (
              <ExtensionConfigGroup
                title={t("settings.extensionsUiSurfaces")}
                description={t("settings.extensionsUiSurfacesHelp")}
              >
                {row.hasQuickButton && (
                  <label className="extension-ui-settings-toggle">
                    <input
                      type="checkbox"
                      checked={row.quickButtonEnabled}
                      onChange={(e) => toggleSurface(row.id, "quick_button_enabled", e.target.checked)}
                    />
                    {t("settings.extensionsQuickButton")}
                  </label>
                )}
                {row.hasPage && (
                  <label className="extension-ui-settings-toggle">
                    <input
                      type="checkbox"
                      checked={row.pageEnabled}
                      onChange={(e) => toggleSurface(row.id, "page_enabled", e.target.checked)}
                    />
                    {t("settings.extensionsPage")}
                  </label>
                )}
              </ExtensionConfigGroup>
            )}
            {row.mcp.length > 0 && (
              <ExtensionConfigGroup
                title={t("settings.extensionsMcpServers")}
                description={t("settings.extensionsMcpServersHelp")}
              >
                {row.mcp.map((server) => (
                  <label key={server.name} className="extension-ui-settings-toggle">
                    <input
                      type="checkbox"
                      checked={server.enabled}
                      onChange={(e) => toggleMcp(row.id, server.name, e.target.checked)}
                    />
                    {server.label}
                  </label>
                ))}
              </ExtensionConfigGroup>
            )}
            {row.instructionSections.length > 0 && (
              <ExtensionConfigGroup
                title={t("settings.extensionsInstructions")}
                description={t("settings.extensionsInstructionsHelp")}
              >
                {row.instructionSections.some((s) => s.level === "global") && (
                  <label className="extension-ui-settings-toggle">
                    <input
                      type="checkbox"
                      checked={row.globalInstructionsEnabled}
                      onChange={(e) => toggleInstructions(row.id, "global", e.target.checked)}
                    />
                    {t("settings.extensionsInstructionsGlobal")}
                  </label>
                )}
                {row.instructionSections.some((s) => s.level === "project") && primaryProjects.length > 0 && (
                  <div className="extension-ui-settings-instruction-projects">
                    <span className="extension-ui-settings-instruction-group">
                      {t("settings.extensionsInstructionsProjects")}
                    </span>
                    {primaryProjects.map((p) => (
                      <label key={p.path} className="extension-ui-settings-toggle" title={p.path}>
                        <input
                          type="checkbox"
                          checked={Boolean(row.projectInstructionsEnabled[p.path])}
                          onChange={(e) => toggleInstructions(row.id, "project", e.target.checked, p.path)}
                        />
                        {p.name || p.path}
                      </label>
                    ))}
                  </div>
                )}
              </ExtensionConfigGroup>
            )}
            {row.permissions.length > 0 && (
              <ExtensionConfigGroup
                title={t("settings.extensionsPermissions")}
                description={t("settings.extensionsPermissionsHelp")}
              >
                {row.permissions.map((permission) => (
                  <ExtensionPermissionRow
                    key={permission.name}
                    permission={permission}
                    onToggle={(permissionName, next) => togglePermission(row.id, permissionName, next)}
                  />
                ))}
              </ExtensionConfigGroup>
            )}
            {row.remoteServices.length > 0 && (
              <ExtensionConfigGroup
                title={t("settings.extensionsRemoteServices")}
                description={t("settings.extensionsRemoteServicesHelp")}
              >
                <div className="extension-ui-settings-remote-services">
                  {row.remoteServices.map((service) => (
                    <div key={service.name} className="extension-ui-settings-remote-service">
                      <div className="extension-ui-settings-remote-service-main">
                        <span className="extension-ui-settings-remote-service-name">{service.name}</span>
                        <span className="extension-ui-settings-remote-service-url">{service.base_url}</span>
                      </div>
                      <div className="extension-ui-settings-remote-service-purpose">{service.purpose}</div>
                    </div>
                  ))}
                </div>
              </ExtensionConfigGroup>
            )}
            {row.settingsSchema.length > 0 && (
              <ExtensionConfigGroup
                title={t("settings.extensionsSettings")}
                description={t("settings.extensionsSettingsHelp")}
              >
                <div className="extension-ui-settings-fields">
                  {row.settingsSchema.map((spec) => (
                    <ExtensionSettingField
                      key={spec.key}
                      spec={spec}
                      value={row.settingsValues[spec.key]}
                      secretPresent={Boolean(row.secretPresent[spec.key])}
                      onChange={(value) => setSetting(row.id, spec.key, value, spec.type === "secret")}
                      onClearSecret={() => setSetting(row.id, spec.key, "", true)}
                    />
                  ))}
                </div>
              </ExtensionConfigGroup>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

function ExtensionUserInstructionsField({
  value,
  placeholder,
  onSave,
}: {
  value: string;
  placeholder: string;
  onSave: (text: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  // Re-sync when the persisted value changes from elsewhere (e.g. refresh).
  useEffect(() => {
    setDraft(value);
  }, [value]);
  const commit = useCallback(() => {
    const next = draft.trim();
    if (next === value.trim()) return;
    onSave(next);
  }, [draft, value, onSave]);
  return (
    <textarea
      className="extension-ui-settings-instructions-input"
      value={draft}
      placeholder={placeholder}
      rows={3}
      maxLength={4000}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
    />
  );
}

function ExtensionSettingField({
  spec,
  value,
  secretPresent,
  onChange,
  onClearSecret,
}: {
  spec: SettingSpec;
  value: unknown;
  secretPresent: boolean;
  onChange: (value: unknown) => void;
  onClearSecret: () => void;
}) {
  if (spec.type === "boolean") {
    return (
      <label className="ext-setting-field">
        <span className="ext-setting-field-label">{spec.label}</span>
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
        />
      </label>
    );
  }
  if (spec.type === "secret") {
    return (
      <div className="ext-setting-field">
        <span className="ext-setting-field-label">{spec.label}</span>
        <input
          type="password"
          placeholder={secretPresent ? "•••••• (saved)" : ""}
          onBlur={(e) => {
            if (e.target.value) onChange(e.target.value);
          }}
        />
        {secretPresent && <button type="button" className="ext-setting-clear" onClick={onClearSecret}><Icon name="x" size={18} /></button>}
      </div>
    );
  }
  if (spec.type === "string" && Array.isArray(spec.enum)) {
    return (
      <label className="ext-setting-field">
        <span className="ext-setting-field-label">{spec.label}</span>
        <Select
          value={(value as string) ?? (spec.default as string) ?? ""}
          onChange={(v) => onChange(v)}
          options={spec.enum.map((opt) => ({ value: String(opt), label: String(opt) }))}
        />
      </label>
    );
  }
  return (
    <label className="ext-setting-field">
      <span className="ext-setting-field-label">{spec.label}</span>
      <input
        type={spec.type === "number" ? "number" : "text"}
        defaultValue={(value as string) ?? (spec.default as string) ?? ""}
        onBlur={(e) =>
          onChange(spec.type === "number" ? Number(e.target.value) : e.target.value)
        }
      />
    </label>
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
  onAdd,
  onMobile,
  onEdit,
  onActivate,
  onSuspend,
  onDelete,
  onOpenProviderConfigSync,
  setupStatuses,
  projects,
  repoStatus,
  firstRunDone,
  networkBindAddress,
  teamEnabled,
  credentialBrokerEnabled,
  providerConfigSyncEnabled,
  section,
  onSectionChange,
  onAddProject,
  onInitConfigRepo,
  onLoadConfigRepo,
  onSyncConfigRepo,
  onInstallProvider,
  installRuns,
  onVerifyProviders,
  onNetworkBindChange,
}: ProvidersListProps) {
  const { t } = useTranslation();
  const extensionSettingsModules = useExtensionFrontendModules("settings");
  const extensionSettingsBySection = useMemo(() => {
    const items = new Map<SettingsSection, ExtensionFrontendModule>();
    for (const item of extensionSettingsModules) {
      items.set(`extension:${item.extension_id}:${item.id}`, item);
    }
    return items;
  }, [extensionSettingsModules]);
  const extensionSettingsSection = extensionSettingsBySection.get(section);
  const sections: { id: SettingsSection; label: string }[] = [
    { id: "providers", label: t("setup.providersTitle") },
    { id: "language", label: t("language.label") },
    { id: "appearance", label: t("settings.appearanceTitle") },
    { id: "desktop", label: t("settings.desktopTitle") },
    { id: "shortcuts", label: t("settings.shortcutsTitle") },
    ...(teamEnabled ? [{ id: "delegation" as const, label: t("settings.delegationTitle") }] : []),
    { id: "context", label: t("settings.contextTitle") },
    { id: "internalLlm", label: t("settings.internalLlmTitle") },
    { id: "sessions", label: t("settings.sessionsTitle") },
    { id: "voice", label: t("settings.voiceTitle") },
    { id: "extensions", label: t("settings.extensionsTitle") },
    ...(credentialBrokerEnabled ? [{ id: "passwords" as const, label: t("settings.passwordManager") }] : []),
    ...extensionSettingsModules.map((item) => ({
      id: `extension:${item.extension_id}:${item.id}` as const,
      label: item.label,
    })),
  ];
  useEffect(() => {
    if (section.startsWith("extension:") && !extensionSettingsSection) {
      onSectionChange("providers");
    }
  }, [extensionSettingsSection, onSectionChange, section]);
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
          onDelete={onDelete}
          onRefreshApp={onRefreshApp}
          refreshAppDisabled={refreshAppDisabled}
          setupStatuses={setupStatuses}
          projects={projects}
          repoStatus={repoStatus}
          firstRunDone={firstRunDone}
          networkBindAddress={networkBindAddress}
          credentialBrokerEnabled={credentialBrokerEnabled}
          providerConfigSyncEnabled={providerConfigSyncEnabled}
          onAddProject={onAddProject}
          onInitConfigRepo={onInitConfigRepo}
          onLoadConfigRepo={onLoadConfigRepo}
          onSyncConfigRepo={onSyncConfigRepo}
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
      {section === "appearance" && <AppearanceSetting />}
      {section === "desktop" && (
        <>
          <DesktopAppSettingsSection />
          <div className="setup-divider" />
          <AutoRestartOnIdleSetting />
        </>
      )}
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
          <NativeImportSetting />
        </>
      )}
      {section === "voice" && <VoiceSettings />}
      {section === "extensions" && <ExtensionUiSettingsSection />}
      {section === "passwords" && credentialBrokerEnabled && <PasswordManagerSetting />}
      {extensionSettingsSection && <ExtensionModuleSlot module={extensionSettingsSection} />}
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
          <button type="button" className="btn-secondary settings-page-mobile-action" onClick={onMobile}>
            {t("mobileSetup.title")}
          </button>
          <button className="setup-cancel-btn settings-page-close-action" onClick={onClose}>
            {t("machines.back")}
          </button>
        </div>
      </div>
      <div className="settings-page-layout">
        <nav className="settings-page-nav" aria-label={t("settings.title")}>
          {sections.map((item) => (
            <button
              key={item.id}
              type="button"
              className={item.id === section ? "active" : ""}
              aria-current={item.id === section ? "page" : undefined}
              onClick={() => onSectionChange(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>
        <div className="settings-page-content">
          {body}
          {section === "providers" && (
            <div className="settings-page-provider-actions">
              {onOpenProviderConfigSync && (
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={onOpenProviderConfigSync}
                >
                  Provider Config Sync
                </button>
              )}
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
  onDelete,
  setupStatuses,
  projects,
  repoStatus,
  firstRunDone,
  networkBindAddress,
  credentialBrokerEnabled,
  providerConfigSyncEnabled,
  onAddProject,
  onInitConfigRepo,
  onLoadConfigRepo,
  onSyncConfigRepo,
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
  | "teamEnabled"
  | "section"
  | "onSectionChange"
>) {
  const { t } = useTranslation();
  return (
    <>
      {!firstRunDone && (
        <FirstRunWizard
          statuses={setupStatuses}
          providers={providers}
          projects={projects}
          repoStatus={repoStatus}
          networkBindAddress={networkBindAddress}
          onNetworkBindChange={onNetworkBindChange}
          onRefreshApp={onRefreshApp}
          refreshAppDisabled={refreshAppDisabled}
          busy={busy}
          credentialBrokerEnabled={credentialBrokerEnabled}
          providerConfigSyncEnabled={providerConfigSyncEnabled}
          onAddProject={onAddProject}
          onInitConfigRepo={onInitConfigRepo}
          onLoadConfigRepo={onLoadConfigRepo}
          onSyncConfigRepo={onSyncConfigRepo}
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
          return (
            <div key={p.id} className={`provider-row ${isActive ? "active" : ""} ${isSuspended ? "suspended" : ""}`}>
              <div className="provider-row-main" onClick={() => onEdit(p)}>
                <div className="provider-row-name">
                  {p.name}
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
                      {t(`setup.runner.${p.runner}`, { defaultValue: p.runner })}
                    </span>
                  )}
                </div>
                <div className="provider-row-meta">
                  {p.mode === "subscription"
                    ? t('setup.subscriptionMode')
                    : `API key${
                        p.has_api_key ? "" : ` — ${t('setup.apiKeyMissing')}`
                      }${p.base_url ? ` · ${p.base_url}` : ""}`}
                </div>
              </div>
              <div className="provider-row-actions">
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
              disabled={running || busy || !item.prerequisite.ok}
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
  repoStatus,
  networkBindAddress,
  busy,
  credentialBrokerEnabled,
  providerConfigSyncEnabled,
  onAddProject,
  onInitConfigRepo,
  onLoadConfigRepo,
  onSyncConfigRepo,
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
  repoStatus: ProviderConfigRepositoryStatus | null;
  networkBindAddress: NetworkBindAddress;
  busy: boolean;
  credentialBrokerEnabled: boolean;
  providerConfigSyncEnabled: boolean;
  onAddProject: (path: string) => void;
  onInitConfigRepo: (remoteUrl: string) => void;
  onLoadConfigRepo: (remoteUrl: string) => void;
  onSyncConfigRepo: () => void;
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
  const [remoteUrl, setRemoteUrl] = useState(repoStatus?.remote_url || "");
  useEffect(() => {
    if (repoStatus?.remote_url) setRemoteUrl(repoStatus.remote_url);
  }, [repoStatus?.remote_url]);
  const hasProvider = providers.length > 0;
  const hasProject = projects.length > 0;
  const hasRepo = Boolean(repoStatus?.enabled && repoStatus.remote_url);

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
      {providerConfigSyncEnabled && (
        <div className="first-run-step">
          <div className="first-run-step-copy">
            <strong>{t("setup.configRepoStepTitle")}</strong>
            <span>
              {hasRepo
                ? t("setup.configRepoEnabled")
                : t("setup.configRepoMissing")}
            </span>
            {repoStatus?.last_error && <span className="setup-error">{repoStatus.last_error}</span>}
            {repoStatus?.apply && (
              <span>{t("setup.configRepoApplied", { count: repoStatus.apply.updated })}</span>
            )}
          </div>
          <div className="first-run-inline-form">
            <input
              type="text"
              value={remoteUrl}
              onChange={(e) => setRemoteUrl(e.target.value)}
              placeholder={t("setup.configRepoRemotePlaceholder")}
              spellCheck={false}
            />
            <button
              type="button"
              className="btn-secondary"
              disabled={busy || !remoteUrl.trim()}
              onClick={() => onLoadConfigRepo(remoteUrl.trim())}
            >
              {t("setup.loadConfigRepo")}
            </button>
            <button
              type="button"
              className="btn-secondary"
              disabled={busy || !remoteUrl.trim()}
              onClick={() => onInitConfigRepo(remoteUrl.trim())}
            >
              {t("setup.pushConfigRepo")}
            </button>
            <button
              type="button"
              className="btn-secondary"
              disabled={busy || !hasRepo}
              onClick={onSyncConfigRepo}
            >
              {t("setup.syncConfigRepo")}
            </button>
          </div>
        </div>
      )}
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
    sakana: { labelKey: "setup.templateSakanaLabel", blurbKey: "setup.templateSakanaBlurb" },
    ollama: { labelKey: "setup.templateOllamaLabel", blurbKey: "setup.templateOllamaBlurb" },
    zai: { labelKey: "setup.templateZaiLabel", blurbKey: "setup.templateZaiBlurb" },
    "zai-openai": { labelKey: "setup.templateZaiOpenAILabel", blurbKey: "setup.templateZaiOpenAIBlurb" },
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
// One axis for claude/gemini/openai, two independent axes (approval + sandbox) for codex.
const PERMISSION_OPTIONS: Record<string, Record<string, string[]>> = {
  claude: { mode: ["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"] },
  codex: {
    approval: ["untrusted", "on-request", "on-failure", "never"],
    sandbox: ["read-only", "workspace-write", "danger-full-access"],
  },
  gemini: { mode: ["auto_edit", "yolo", "plan"] },
  openai: { mode: ["default", "bypassPermissions"] },
};
const PERMISSION_DEFAULTS: Record<string, Record<string, string>> = {
  claude: { mode: "bypassPermissions" },
  codex: { approval: "never", sandbox: "danger-full-access" },
  gemini: { mode: "yolo" },
  openai: { mode: "bypassPermissions" },
};
function permissionOptionsForKind(kind: string): Record<string, string[]> {
  return PERMISSION_OPTIONS[kind] ?? {};
}

function runnerOptionsForKind(kind: string, saved?: Provider["runner_options"]): Provider["runner_options"] {
  if (saved?.length) return saved;
  if (kind === "fugu") return ["native", "better_agent_runner"];
  return kind === "openai" ? ["better_agent_runner"] : ["native"];
}

function runtimeKindForRunner(kind: string, runner: Provider["runner"]): string {
  return runner === "better_agent_runner" ? "openai" : kind;
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
  onClose: () => void;
  onBack: () => void;
  onSubmit: (payload: FormPayload) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(initial.name);
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
  const [modelOptions, setModelOptions] = useState<string[] | null>(null);
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
  }, [baseUrl, defaultModel, kind, mode_, modes, runner]);

  const updateRunner = (next: Provider["runner"]) => {
    setRunner(next);
    if (kind === "fugu" && next === "better_agent_runner") {
      setMode("api_key");
      if (!baseUrl) setBaseUrl(SAKANA_FUGU_API_BASE_URL);
      if (!defaultModel) setDefaultModel("fugu");
    }
  };

  // Edit mode: fetch this provider's model list so the default_model
  // dropdown is populated. Refetch on remount; cheap (cached server-side).
  useEffect(() => {
    if (mode !== "edit" || !providerId) return;
    let cancelled = false;
    const { promise } = trackPromise(`providers:fetchModels:${providerId}`, async () => {
      const r = await fetch(`${API}/api/providers/${providerId}/models`);
      return r.ok ? ((await r.json()) as { models: string[] }) : { models: [] };
    });
    promise
      .then((d) => {
        if (!cancelled) setModelOptions(d.models || []);
      })
      .catch(() => {
        if (!cancelled) setModelOptions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [mode, providerId]);

  const submit = async () => {
    setSubmitting(true);
    try {
      await onSubmit({
        name,
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
                  {t(`setup.runner.${option}`)}
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
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={
                  initialHasKey
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
                placeholder="sonnet, glm-4.6, claude-opus-4-8[1m], …"
                spellCheck={false}
              />
              {mode === "edit" && modelOptions !== null && (
                <button
                  type="button"
                  className="btn-icon"
                  title="Pick from list"
                  onClick={() => setCustomModelMode(false)}
                >
                  <Icon name="check" size={18} />
                </button>
              )}
            </div>
          )}
          {mode === "edit" && modelOptions === null && (
            <span className="setup-field-hint">Loading model list…</span>
          )}
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
          disabled={submitting}
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
        initialHasKey={provider.has_api_key}
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
