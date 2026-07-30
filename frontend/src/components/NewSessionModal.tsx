import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import type { KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import type {
  FileAttachment,
  HarnessProfile,
  NodeSnapshot,
  OrchestrationMode,
  PastedImage,
  Project,
  Provider,
  ProviderRunner,
  ReasoningEffort,
  Permission,
} from "../types";
import { trackedFetch, useOpProgress } from "../progress/store";
import { PUBLIC_EXTENSION_IDS } from "../extensionIds";
import { useMachines } from "../hooks/useMachines";
import { useLocalNodeId } from "../hooks/useLocalNodeId";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import { usePersistedDraft } from "../hooks/usePersistedDraft";
import { useProviderModelCatalog } from "../hooks/useProviderModelCatalog";
import { ConfirmModal } from "./ConfirmModal";
import { ModelCatalogStatus } from "./ModelCatalogStatus";

import { API, fetchSessionOrganization, createSessionFolder } from "../api";
import {
  optionLabelWithQuota,
  quotaRemainingText,
  quotaResetText,
  summarizeProvider,
  type QuotaSummary,
} from "../utils/quotaStatus";
import { providerDisplayName } from "../utils/providerDisplayName";
import { useQuotaStatus } from "../hooks/useQuotaStatus";
import Icon from "./Icon";
import { ComposerImagePreviews } from "./ComposerImagePreviews";
import { VoiceActivation } from "./VoiceActivation";
import {
  dictationDelta,
  speakVoiceText,
  type VoiceCommandAction,
} from "../lib/voiceActivation";
import { NewSessionCreateButton } from "./NewSessionCreateButton";
import { HarnessProfileSelector } from "./HarnessProfileSelector";
import { SessionFolderPopover } from "./SessionFolderPopover";
import type { PopoverAnchor } from "./SessionTagPopover";
import { buildFolderPathMap } from "../sessionFolders";
import type { SessionFolder } from "../types";
import { fileToAttachment } from "../utils/fileAttach";
import { fileToPastedImage, imageFilesFromClipboard } from "../utils/imageAttach";
import {
  cacheProviders,
  readProviderCache,
} from "../utils/providerCache";
import {
  effortsForRuntime,
  effortsForRunner,
  runnerForProvider,
  runnerLabelKey,
  type ModelRuntimeProfile,
} from "./modelPicker";

export const NEW_SESSION_PROMPT_TESTID = "new-session-prompt-textarea";

interface RuntimeProfile {
  providerId: string;
  model: string;
  reasoningEffort: ReasoningEffort | "";
  runner: ProviderRunner;
  /** Per-session permission override. {} = inherit provider default. */
  permission: Permission;
}

interface SessionConfig {
  orchestrationMode: OrchestrationMode;
  main: RuntimeProfile;
  worker: RuntimeProfile;
  cwd: string;
  fileEditEnabled: boolean;
  fileEditPath?: string;
  /** Multi-machine: the topology node id that will execute this
   * session's workers. Defaults to "primary" (the local backend).
   * The picker is hidden in single-machine deploys (≤ 1 machine
   * known to the backend) so users never see it unless it matters. */
  nodeId: string;
  initialPrompt: string;
  initialImages: PastedImage[];
  initialFiles: FileAttachment[];
  harnessProfileId: string;
  /** Optional folder to file the new session into. `null` means "no
   * folder" (Unfiled) — a valid, persistable choice. Persisted across
   * opens as the last selection; re-validated against the chosen
   * project's folders on load (a folder from another project is ignored). */
  folderId: string | null;
}

type NewSessionExtensionOptionValue = boolean;

export interface NewSessionExtensionOption {
  id: string;
  extensionId: string;
  label: string;
  defaultValue: NewSessionExtensionOptionValue;
  children?: NewSessionExtensionOption[];
  applyToSessionConfig?: (
    value: NewSessionExtensionOptionValue,
    values: Record<string, NewSessionExtensionOptionValue>,
  ) => Partial<SessionConfig>;
}

/** Optional initial prompt + images (e.g. from "Investigate" right-click). */
export interface InvestigationContext {
  prompt: string;
  images: PastedImage[];
  files?: FileAttachment[];
}

export type NewSessionCreationAction = "create" | "send" | "send-and-open";

interface Props {
  open: boolean;
  onClose: () => void;
  onCreate: (
    config: SessionConfig,
    investigation: InvestigationContext | undefined,
    action: NewSessionCreationAction,
  ) => void | Promise<void>;
  defaultCwd: string;
  /** Existing projects (paths + names). Drives the project picker so
   * users don't have to type a path. Required so the modal can render
   * the picker without an extra fetch (App already loads projects). */
  projects: Project[];
  /** Optional project path to pre-select (overrides `defaultCwd` when
   * provided). Wired by the Ask flow's "Create new" — the Ask agent's
   * `proposed_project_path` lands here. Treated as a SHORTCUT, not a
   * constraint: the user can still change the project in the picker. */
  initialProjectPath?: string;
  /** Owning machine `node_id` for `initialProjectPath`. Resolved
   * server-side from `project_store` so a multi-machine deploy with
   * two projects sharing the same `path` on different nodes pre-fills
   * the right machine (the client-side `projects.find(path)` would pick
   * arbitrarily). Ignored when `initialProjectPath` is omitted. */
  initialNodeId?: string;
  /** Pre-filled investigation context (screenshot + prompt). When present,
   *  shows an editable prompt textarea at the top of the modal. */
  investigation?: InvestigationContext;
  teamEnabled?: boolean;
  machineNodesEnabled?: boolean;
  allowOfflineCreate?: boolean;
  extensionOptions?: NewSessionExtensionOption[];
}

const STORAGE_KEY = "better-agent-new-session-defaults";
// Unsent initial-prompt text survives closing the modal so a half-written
// prompt is never lost; cleared on successful create or on explicit discard.
const PROMPT_DRAFT_KEY = "better-agent-new-session-prompt-draft";
const EMPTY_EXTENSION_OPTIONS: NewSessionExtensionOption[] = [];

interface NewSessionDefaults extends Partial<SessionConfig> {
  creationAction?: NewSessionCreationAction;
}

function isCreationAction(value: unknown): value is NewSessionCreationAction {
  return value === "create" || value === "send" || value === "send-and-open";
}

function loadDefaults(): NewSessionDefaults {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const defaults = JSON.parse(raw) as NewSessionDefaults;
    if (!isCreationAction(defaults.creationAction)) delete defaults.creationAction;
    return defaults;
  } catch {
    return {};
  }
}

function saveDefaults(config: SessionConfig, creationAction: NewSessionCreationAction) {
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      orchestrationMode: config.orchestrationMode,
      main: config.main,
      worker: config.worker,
      folderId: config.folderId,
      harnessProfileId: config.harnessProfileId,
      creationAction,
    }),
  );
}

function flattenExtensionOptions(options: NewSessionExtensionOption[]): NewSessionExtensionOption[] {
  return options.flatMap((option) => [
    option,
    ...flattenExtensionOptions(option.children ?? []),
  ]);
}

function extensionOptionKey(option: NewSessionExtensionOption): string {
  return `${option.extensionId}:${option.id}`;
}

function extensionOptionDefaults(
  options: NewSessionExtensionOption[],
): Record<string, NewSessionExtensionOptionValue> {
  const values: Record<string, NewSessionExtensionOptionValue> = {};
  for (const option of flattenExtensionOptions(options)) {
    const key = extensionOptionKey(option);
    values[key] = option.defaultValue;
  }
  return values;
}

function applyExtensionOptionsToSessionConfig(
  config: SessionConfig,
  options: NewSessionExtensionOption[],
  values: Record<string, NewSessionExtensionOptionValue>,
): SessionConfig {
  return options.reduce((next, option) => {
    const value = values[extensionOptionKey(option)] ?? option.defaultValue;
    const patch = option.applyToSessionConfig?.(value, values);
    const patched = patch ? { ...next, ...patch } : next;
    if (!value) return patched;
    return applyExtensionOptionsToSessionConfig(patched, option.children ?? [], values);
  }, config);
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function resolveReasoningEffort(
  saved: RuntimeProfile | undefined,
  provider: Provider,
  runner: ProviderRunner,
  role: "main" | "worker",
): ReasoningEffort | "" {
  const options = effortsForRunner(provider, runner);
  if (options.length === 0) return "";
  const savedRunner = saved?.runner || runnerForProvider(provider);
  const savedEffort = saved?.providerId === provider.id && savedRunner === runner
    ? saved.reasoningEffort
    : "";
  const lastEffort = provider.last_reasoning_effort ?? "";
  const defaultEffort = provider.default_reasoning_effort || "";
  const candidates =
    role === "main"
      ? [lastEffort, savedEffort, defaultEffort]
      : [savedEffort, lastEffort, defaultEffort];
  return candidates.find((effort): effort is ReasoningEffort =>
    !!effort && options.includes(effort as ReasoningEffort)
  ) ?? options[0];
}

function resolvePermission(
  saved: RuntimeProfile | undefined,
  provider: Provider,
): Permission {
  // Carry over a previously chosen override when the provider still matches;
  // otherwise inherit the provider default ({}).
  const savedPerm = saved?.providerId === provider.id ? saved.permission : {};
  return savedPerm && Object.keys(savedPerm).length > 0 ? { ...savedPerm } : {};
}

export function resolveRuntimeProfile(
  saved: RuntimeProfile | undefined,
  providers: Provider[],
  defaultProviderId: string | null,
  role: "main" | "worker",
): RuntimeProfile {
  const availableProviders = providers.filter((item) => !item.suspended);
  const provider =
    availableProviders.find((item) => item.id === saved?.providerId)
    ?? availableProviders.find((item) => item.id === defaultProviderId)
    ?? availableProviders[0];
  if (!provider) return { providerId: "", model: "", reasoningEffort: "", runner: "native", permission: {} };

  const savedRunner = saved?.providerId === provider.id ? saved.runner : undefined;
  const runner = savedRunner && provider.runner_options.includes(savedRunner)
    ? savedRunner
    : runnerForProvider(provider);

  const savedModel = saved?.providerId === provider.id ? saved.model : "";
  const lastModel = provider.last_model ?? "";
  // Main usage is what the backend records as `last_model`, so for the
  // main role it outranks the locally-saved default. The worker role's
  // only memory is the saved default — keep it first so a main pick on
  // the same provider can't silently override the worker's model.
  const candidates =
    role === "main"
      ? [lastModel, savedModel, provider.default_model]
      : [savedModel, lastModel, provider.default_model];
  const model = candidates.find(Boolean) || "";
  return {
    providerId: provider.id,
    model,
    reasoningEffort: resolveReasoningEffort(saved, provider, runner, role),
    runner,
    permission: resolvePermission(saved, provider),
  };
}

/** Inline usage-left warning shown in a runtime profile picker when the
 * selected provider's worst-window quota is at warn or critical. Surfaces
 * the same reading the option labels append as "X% left", but prominently
 * so a near-exhausted provider is not missed before creating a session. */
function UsageLeftWarning({
  summary,
  providerLabel,
}: {
  summary: QuotaSummary;
  providerLabel: string;
}) {
  const { t } = useTranslation();
  const remaining = quotaRemainingText(summary, t);
  const reset = quotaResetText(summary, t);
  return (
    <div
      className={`ns-modal-usage-warning usage-${summary.level}`}
      role="status"
      aria-live="polite"
    >
      <span className="ns-modal-usage-warning-dot" aria-hidden="true" />
      <span className="ns-modal-usage-warning-text">
        {t("newSession.usageLowWarning", {
          provider: providerLabel,
          remaining,
          defaultValue: "{{provider}} usage is running low — {{remaining}}",
        })}
      </span>
      {reset && <span className="ns-modal-usage-warning-meta">{reset}</span>}
      {summary.stale && (
        <span className="ns-modal-usage-warning-meta">
          {t("quota.stale", { defaultValue: "stale" })}
        </span>
      )}
    </div>
  );
}

function RuntimeProfilePicker({
  label,
  role,
  providers,
  value,
  onChange,
}: {
  label: string;
  role: "main" | "worker";
  providers: Provider[];
  value: RuntimeProfile;
  onChange: (v: RuntimeProfile) => void;
}) {
  const { t } = useTranslation();
  const quotaStatus = useQuotaStatus(API, providers);
  const selectedProvider = providers.find((p) => p.id === value.providerId);
  const {
    catalog,
    networkState,
    refresh,
    refreshing,
    refreshError,
  } = useProviderModelCatalog(value.providerId);
  const models = catalog?.models ?? [];
  const runtimeProfiles = (catalog?.runtime_profiles ?? []) as ModelRuntimeProfile[];
  const selectedQuota = summarizeProvider(quotaStatus, selectedProvider);

  useEffect(() => {
    if (!catalog || catalog.provider_id !== value.providerId) return;
    if (
      catalog.authoritative
      && (
        catalog.status === "pending"
        || catalog.status === "unsupported"
        || catalog.status === "unavailable"
        || (catalog.status === "error" && !catalog.models.length)
      )
    ) {
      if (value.model) onChange({ ...value, model: "" });
      return;
    }
    if (!catalog.models.length || catalog.models.includes(value.model)) return;
    onChange({ ...value, model: catalog.models[0] });
  }, [catalog, onChange, value]);

  return (
    <div className="ns-modal-section">
      <div className="ns-modal-section-title">{label}</div>
      <div className="ns-modal-row">
        <label>{t("newSession.provider")}</label>
        <select
          data-testid="new-session-provider-select"
          value={value.providerId}
          onChange={(e) => {
            const p = providers.find((pr) => pr.id === e.target.value && !pr.suspended);
            if (!p) return;
            onChange({
              providerId: e.target.value,
              model: p?.last_model || p?.default_model || "",
              reasoningEffort: p ? resolveReasoningEffort(undefined, p, runnerForProvider(p), role) : "",
              runner: runnerForProvider(p),
              permission: p ? resolvePermission(undefined, p) : {},
            });
          }}
        >
          {providers.map((p) => {
            const q = summarizeProvider(quotaStatus, p);
            return (
              <option key={p.id} value={p.id} disabled={p.suspended}>
                {optionLabelWithQuota(providerDisplayName(p), q, t)}
                {p.suspended ? ` — ${t("setup.suspended", "Suspended")}` : ""}
              </option>
            );
          })}
        </select>
      </div>
      {selectedQuota &&
      (selectedQuota.level === "warn" || selectedQuota.level === "critical") &&
      selectedProvider ? (
        <UsageLeftWarning
          summary={selectedQuota}
          providerLabel={providerDisplayName(selectedProvider)}
        />
      ) : null}
      {selectedProvider && selectedProvider.runner_options.length > 1 ? (
        <div className="ns-modal-row ns-runtime-axis">
          <label>{t("newSession.runner")}</label>
          <select
            value={value.runner}
            onChange={(e) => {
              const runner = e.target.value as ProviderRunner;
              const options = effortsForRuntime(selectedProvider, runner, value.model, runtimeProfiles);
              const reasoningEffort = options.includes(value.reasoningEffort as ReasoningEffort)
                ? value.reasoningEffort
                : options.includes(selectedProvider.default_reasoning_effort as ReasoningEffort)
                  ? selectedProvider.default_reasoning_effort
                  : options[0] || "";
              onChange({ ...value, runner, reasoningEffort });
            }}
          >
            {selectedProvider.runner_options.map((runner) => (
              <option key={runner} value={runner}>{t(runnerLabelKey(selectedProvider.kind, runner))}</option>
            ))}
          </select>
        </div>
      ) : null}
      <div className="ns-modal-row">
        <label>{t("newSession.model")}</label>
        <select
          data-testid="new-session-model-select"
          value={value.model}
          disabled={
            !models.length
            || catalog?.status === "pending"
            || catalog?.status === "unsupported"
            || catalog?.status === "unavailable"
          }
          onChange={(e) => {
            const model = e.target.value;
            if (!selectedProvider) {
              onChange({ ...value, model });
              return;
            }
            const options = effortsForRuntime(selectedProvider, value.runner, model, runtimeProfiles);
            const reasoningEffort = options.includes(value.reasoningEffort as ReasoningEffort)
              ? value.reasoningEffort
              : options.includes(selectedProvider.default_reasoning_effort as ReasoningEffort)
                ? selectedProvider.default_reasoning_effort
                : options[0] || "";
            onChange({ ...value, model, reasoningEffort });
          }}
        >
          {models.map((m) => (
            <option key={m} value={m}>
              {optionLabelWithQuota(m, selectedQuota, t)}
            </option>
          ))}
          {!models.length && (
            <option value={value.model} disabled>{value.model || "—"}</option>
          )}
        </select>
      </div>
      <ModelCatalogStatus
        catalog={catalog}
        networkState={networkState}
        onRefresh={refresh}
        refreshing={refreshing}
        refreshError={refreshError}
      />
      {selectedProvider && effortsForRuntime(selectedProvider, value.runner, value.model, runtimeProfiles).length ? (
        <div className="ns-modal-row">
          <label>{t("newSession.reasoningEffort")}</label>
          <select
            data-testid="new-session-effort-select"
            value={value.reasoningEffort}
            onChange={(e) => onChange({ ...value, reasoningEffort: e.target.value as ReasoningEffort })}
          >
            {effortsForRuntime(selectedProvider, value.runner, value.model, runtimeProfiles).map((effort) => (
              <option key={effort} value={effort}>
                {t(`reasoningEffort.${effort}`)}
              </option>
            ))}
          </select>
        </div>
      ) : null}
      {selectedProvider?.permission_options &&
      Object.keys(selectedProvider.permission_options).length > 0
        ? Object.entries(selectedProvider.permission_options).map(([axis, allowed]) => {
            const def = selectedProvider.default_permission?.[axis];
            const current = value.permission[axis] ?? "";
            return (
              <div className="ns-modal-row" key={axis}>
                <label>
                  {t("newSession.permission")} ({t(`permission.axis.${axis}`, { defaultValue: axis })})
                </label>
                <select
                  value={current}
                  onChange={(e) => {
                    const v = e.target.value;
                    const next: Permission = { ...value.permission };
                    if (v === "") delete next[axis];
                    else next[axis] = v;
                    onChange({ ...value, permission: next });
                  }}
                >
                  <option value="">
                    {t("permission.inherit", { defaultValue: "Inherit default" })}
                    {def ? ` (${def})` : ""}
                  </option>
                  {allowed.map((v) => (
                    <option key={v} value={v}>
                      {t(`permission.value.${v}`, { defaultValue: v })}
                    </option>
                  ))}
                </select>
              </div>
            );
          })
        : null}
    </div>
  );
}

function MachineNodePicker({
  machines,
  localNodeId,
  value,
  onChange,
}: {
  machines: NodeSnapshot[];
  /** id of "this backend's machine" from the machine-nodes extension. Used
   * to render the "(host)" tag — REPLACES the legacy
   * "primary" label so the UI shows the actual hostname/topology id. */
  localNodeId: string;
  value: string;
  onChange: (id: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="ns-modal-section">
      <div className="ns-modal-section-title">{t("newSession.machine")}</div>
      <div className="ns-modal-row">
        <label>{t("newSession.machineLabel")}</label>
        <select value={value} onChange={(e) => onChange(e.target.value)}>
          {machines.map((m) => {
            const isLocal = m.id === localNodeId;
            // Offline state only meaningful for non-local nodes; the
            // local backend (us) is always "connected" by construction.
            const offline = !isLocal && m.state !== "connected";
            const versionMismatch = !isLocal && m.version_status === "mismatch";
            const dirty = isLocal ? m.primary_dirty : m.app_dirty;
            return (
              <option key={m.id} value={m.id} disabled={versionMismatch}>
                {m.id}
                {isLocal ? ` (${t("newSession.machinePrimary")})` : ""}
                {offline ? ` — ${t("newSession.machineOffline")}` : ""}
                {versionMismatch ? ` — ${t("newSession.machineVersionMismatch")}` : ""}
                {dirty ? ` — ${t("newSession.machineDirty")}` : ""}
              </option>
            );
          })}
        </select>
      </div>
    </div>
  );
}

export function NewSessionModal({
  open,
  onClose,
  onCreate,
  defaultCwd,
  projects,
  initialProjectPath,
  initialNodeId,
  investigation,
  teamEnabled = true,
  machineNodesEnabled = true,
  allowOfflineCreate = false,
  extensionOptions = EMPTY_EXTENSION_OPTIONS,
}: Props) {
  const { t } = useTranslation();
  const [creating, setCreating] = useState(false);
  const creatingRef = useRef(false);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [editedPrompt, setEditedPrompt] = useState("");
  // Investigation prompts are supplied by the caller and edited in
  // `editedPrompt`, so only the plain new-session prompt is drafted.
  const [initialPrompt, setInitialPrompt, clearPromptDraft] = usePersistedDraft(
    investigation ? null : PROMPT_DRAFT_KEY,
  );
  const [discardPromptOpen, setDiscardPromptOpen] = useState(false);
  const [initialImages, setInitialImages] = useState<PastedImage[]>([]);
  const [initialFiles, setInitialFiles] = useState<FileAttachment[]>([]);
  const [harnessProfileId, setHarnessProfileId] = useState("");
  // File Edit's availability derives purely from whether the
  // "ofek-dev.file-edit" extension is enabled on the currently selected
  // harness profile (Default when none is picked) — refetched whenever the
  // profile selection changes.
  const [fileEditExtensionEnabled, setFileEditExtensionEnabled] = useState(true);
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  // Prompt text as last rendered + the dictated portion already merged into it.
  const promptRef = useRef("");
  const dictatedRef = useRef("");
  // cwd state — picker writes here, handleCreate reads from here.
  // Initialized on open from initialProjectPath (Ask shortcut) || defaultCwd
  // || first-project fallback so the picker is never visually-vs-state
  // desynced (empty value would let the browser pick the first option
  // while state stays "").
  const [cwd, setCwd] = useState<string>(
    initialProjectPath || defaultCwd || projects[0]?.path || "",
  );
  // Folder picker. Folders are scoped to the project (cwd). The selected
  // id is loaded from the saved defaults (last selection) and re-validated
  // when the project's folders arrive — an id from a different project is
  // dropped to null (Unfiled) rather than shown stale.
  const [folders, setFolders] = useState<SessionFolder[]>([]);
  const [folderId, setFolderId] = useState<string | null>(
    () => loadDefaults().folderId ?? null,
  );
  const [folderPopover, setFolderPopover] = useState<PopoverAnchor | null>(null);
  const [creationAction, setCreationAction] = useState<NewSessionCreationAction>(
    () => loadDefaults().creationAction ?? "send-and-open",
  );

  const [orchestrationMode, setOrchestrationMode] = useState<OrchestrationMode>(
    teamEnabled ? "team" : "native",
  );
  const [main, setMain] = useState<RuntimeProfile>({ providerId: "", model: "", reasoningEffort: "", runner: "native", permission: {} });
  const [worker, setWorker] = useState<RuntimeProfile>({ providerId: "", model: "", reasoningEffort: "", runner: "native", permission: {} });
  // Fetches the provider catalog. `trackedFetch` already retries transient
  // failures internally (fetchWithRetry — 3 attempts, exponential backoff)
  // and records a persistent failure under the "providers:list" op via
  // `useOpProgress` below, so a manual retry (see `ns-providers-error`
  // below) just re-runs this same call.
  const loadProviders = useCallback(() => {
    const defaults = loadDefaults();
    trackedFetch("providers:list", `${API}/api/providers`)
      .then((r) => r.json())
      .then((d) => {
        const list: Provider[] = d.providers || [];
        const activeId: string | null = d.default_provider_id;
        cacheProviders(list, activeId);
        setProviders(list);
        setMain(resolveRuntimeProfile(defaults.main, list, activeId, "main"));
        setWorker(resolveRuntimeProfile(defaults.worker, list, activeId, "worker"));
      })
      .catch(() => {});
  }, []);
  const providersOp = useOpProgress("providers:list");
  const sessionExtensionOptions = useMemo<NewSessionExtensionOption[]>(
    () => [...extensionOptions],
    [extensionOptions],
  );
  const [extensionOptionValues, setExtensionOptionValues] = useState<
    Record<string, NewSessionExtensionOptionValue>
  >({});
  const [fileEditEnabled, setFileEditEnabled] = useState(false);
  // Machine choice is per-session (like cwd / model — backend
  // persists it on the session record). Intentionally NOT in
  // localStorage defaults per CLAUDE.md state-ownership rule.
  const [nodeId, setNodeId] = useState<string>("primary");
  // Tracks whether the user has explicitly touched the machine picker
  // during this modal session. When true, picking a project NO LONGER
  // auto-syncs `nodeId` — the explicit choice wins. Reset on every open.
  // Prevents the "user picks node-B, then picks project on node-A which
  // silently overrides their choice back to node-A" regression.
  const nodeIdTouchedRef = useRef(false);
  const { machines } = useMachines();
  const localNodeId = useLocalNodeId();
  // Default-pick rule:
  //   0 machines (single-machine deploy, no topology) → silent "primary"
  //   1 machine                                       → auto-pick the one
  //   >1 machines                                     → picker visible
  const showPicker = machineNodesEnabled && machines.length > 1;

  // Reset state from localStorage defaults + fetch providers when modal opens
  useEffect(() => {
    if (!open) return;
    const defaults = loadDefaults();
    setEditedPrompt(investigation?.prompt ?? "");
    dictatedRef.current = "";
    setInitialImages(investigation?.images ?? []);
    setInitialFiles(investigation?.files ?? []);
    setHarnessProfileId(defaults.harnessProfileId ?? "");
    // Prefer the Ask flow's proposed project, else fall back to defaultCwd
    // (the project currently selected in the sidebar), else first project.
    // Re-run on every open so reopening with a new shortcut doesn't show
    // stale state. Mirrors the useState init — keeps the picker value
    // and the `cwd` state in lockstep on first paint. If `projects` is
    // still loading (async fetch), the second effect below backfills
    // when it arrives.
    const initialCwd = initialProjectPath || defaultCwd || projects[0]?.path || "";
    setCwd(initialCwd);
    nodeIdTouchedRef.current = false;
    setOrchestrationMode(
      teamEnabled
        ? defaults.orchestrationMode || "team"
        : "native",
    );
    setExtensionOptionValues(extensionOptionDefaults(sessionExtensionOptions));
    setFolderId(defaults.folderId ?? null);
    // Default pick: (1) the Ask-resolved `initialNodeId` if given —
    // backend already resolved it from project_store; trust it over
    // client-side lookup which can't disambiguate cross-node path
    // collisions. (2) else the node owning the initial cwd. (3) else
    // the local node / sole machine. Critical: without this, picking a
    // non-primary-machine project creates the session on "primary" with
    // a cwd that doesn't exist there.
    const ownerNode =
      initialNodeId
      || projects.find((p) => p.path === initialCwd)?.node_id;
    setNodeId(
      ownerNode
      || (machineNodesEnabled && machines.length === 1 ? machines[0].id : localNodeId || "primary"),
    );
    const cached = readProviderCache();
    if (cached) {
      setProviders(cached.providers);
      setMain(resolveRuntimeProfile(defaults.main, cached.providers, cached.defaultProviderId, "main"));
      setWorker(resolveRuntimeProfile(defaults.worker, cached.providers, cached.defaultProviderId, "worker"));
    }
    loadProviders();
  }, [open, sessionExtensionOptions, loadProviders]);

  // Backfill `cwd` when `projects` arrives AFTER the modal opened. The
  // App-level projects list is fetched async on mount; if the user
  // opens this modal before that fetch resolves, the open-effect runs
  // with `projects=[]` and seeds `cwd=""`. When the fetch lands, this
  // effect picks the first project so Create stops being stuck disabled.
  // Idempotent — only fires when `cwd` is still empty (so it never
  // clobbers a user pick or an Ask-flow `initialProjectPath`).
  useEffect(() => {
    if (!open) return;
    if (cwd) return;
    if (projects.length === 0) return;
    const first = projects[0].path;
    setCwd(first);
    if (!nodeIdTouchedRef.current) {
      const owner = projects.find((p) => p.path === first)?.node_id;
      if (owner) setNodeId(owner);
    }
  }, [open, projects, cwd]);

  // Load the chosen project's folders whenever the project (cwd) changes
  // or the modal reopens. Folders are project-scoped, so switching
  // projects reloads the list.
  useEffect(() => {
    if (!open) return;
    const pid = cwd || defaultCwd;
    if (!pid) {
      setFolders([]);
      return;
    }
    let cancelled = false;
    fetchSessionOrganization(pid)
      .then((snap) => {
        if (cancelled) return;
        setFolders(snap.folders ?? []);
      })
      .catch(() => {
        if (!cancelled) setFolders([]);
      });
    return () => {
      cancelled = true;
    };
  }, [open, cwd, defaultCwd]);

  // Drop a folder selection that doesn't belong to the current project
  // (e.g. a remembered id from another project, or a since-deleted
  // folder). State-only reset — the saved default is preserved so
  // returning to the original project still recalls it.
  useEffect(() => {
    if (folderId && !folders.some((f) => f.id === folderId)) {
      setFolderId(null);
    }
  }, [folders, folderId]);

  // File Edit is gated by whether the "ofek-dev.file-edit" extension is
  // enabled on the currently selected harness profile (resolved GET —
  // `default` when no profile is explicitly selected).
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    trackedFetch(
      "harnessProfiles:fileEditCheck",
      `${API}/api/harness-profiles/${encodeURIComponent(harnessProfileId || "default")}`,
    )
      .then((r) => r.json())
      .then((profile: HarnessProfile) => {
        if (cancelled) return;
        const disabledExtensions = profile?.fields?.disabled_builtin_extensions?.resolved || [];
        setFileEditExtensionEnabled(
          Boolean(profile?.fields?.extension_instances?.[PUBLIC_EXTENSION_IDS.fileEdit]) &&
            !disabledExtensions.includes(PUBLIC_EXTENSION_IDS.fileEdit),
        );
      })
      .catch(() => {
        // Can't resolve the profile (e.g. offline) — don't lock the user
        // out of an otherwise-available feature; the backend still
        // enforces the real gate at session-creation time.
        if (!cancelled) setFileEditExtensionEnabled(true);
      });
    return () => {
      cancelled = true;
    };
  }, [open, harnessProfileId]);

  useEffect(() => {
    if (!fileEditExtensionEnabled) setFileEditEnabled(false);
  }, [fileEditExtensionEnabled]);

  // Capability gating: only manager-capable providers can drive the
  // persistent "manager" session in manager mode. If the user has no
  // such provider configured, the "manager" button is disabled and
  // the modal forces "native". The main-role provider picker also
  // filters to capable providers when in manager mode so the user
  // can't pick a manager-incapable provider as the manager.
  const activeProviders = providers.filter((p) => !p.suspended);
  const managerCapableProviders = activeProviders.filter(
    (p) => p.supports_manager_mode,
  );
  const managerModeAvailable = teamEnabled && managerCapableProviders.length > 0;
  const availableOrchestrationModes = useMemo<OrchestrationMode[]>(
    () => [
      ...(managerModeAvailable ? (["team"] as OrchestrationMode[]) : []),
      "native",
    ],
    [managerModeAvailable],
  );
  const effectiveOrchestrationMode = availableOrchestrationModes.includes(orchestrationMode)
    ? orchestrationMode
    : availableOrchestrationModes[0];
  const showOrchestrationPicker = availableOrchestrationModes.length > 1;
  useEffect(() => {
    if (orchestrationMode !== effectiveOrchestrationMode) {
      setOrchestrationMode(effectiveOrchestrationMode);
    }
  }, [orchestrationMode, effectiveOrchestrationMode]);
  // When in manager mode but `main` points at a non-manager-capable
  // provider (e.g. user switched provider AFTER picking manager mode),
  // reset `main` to the first manager-capable provider.
  useEffect(() => {
    if (effectiveOrchestrationMode !== "team") return;
    if (!main.providerId) return;
    const cur = activeProviders.find((p) => p.id === main.providerId);
    if (cur && cur.supports_manager_mode) return;
    const fb = managerCapableProviders[0];
    if (fb) {
      setMain({
        providerId: fb.id,
        model: fb.default_model,
        reasoningEffort: resolveReasoningEffort(undefined, fb, runnerForProvider(fb), "main"),
        runner: runnerForProvider(fb),
        permission: resolvePermission(main, fb),
      });
    }
  }, [effectiveOrchestrationMode, main.providerId, activeProviders, managerCapableProviders]);

  const addAttachments = useCallback((files: File[]) => {
    files.forEach((file) => {
      if (file.type.startsWith("image/")) {
        fileToPastedImage(file).then((image) => {
          setInitialImages((prev) => [...prev, image]);
        });
        return;
      }
      fileToAttachment(file).then((attachment) => {
        setInitialFiles((prev) => [...prev, attachment]);
      });
    });
  }, []);

  const promptText = investigation ? editedPrompt : initialPrompt;
  promptRef.current = promptText;
  const promptImages = initialImages;
  const promptFiles = initialFiles;
  const folderPathMap = useMemo(() => buildFolderPathMap(folders), [folders]);
  const selectedFolderLabel = folderId
    ? (folderPathMap.get(folderId) ?? t("session.unfiled"))
    : t("session.unfiled");
  const missingProviderConfig =
    !main.providerId || (effectiveOrchestrationMode === "team" && !worker.providerId);
  const createDisabled = !(cwd || defaultCwd) || (!allowOfflineCreate && missingProviderConfig);

  const handleCreate = async (action: NewSessionCreationAction, promptOverride?: string) => {
    if (creatingRef.current || createDisabled) return;
    creatingRef.current = true;
    setCreating(true);
    try {
      const effectiveCwd = cwd || defaultCwd;
      const baseConfig: SessionConfig = {
        orchestrationMode: effectiveOrchestrationMode,
        main,
        worker,
        cwd: effectiveCwd,
        fileEditEnabled,
        fileEditPath: undefined,
        nodeId,
        initialPrompt: promptOverride ?? initialPrompt,
        initialImages,
        initialFiles,
        harnessProfileId,
        folderId,
      };
      const config = applyExtensionOptionsToSessionConfig(
        baseConfig,
        sessionExtensionOptions,
        extensionOptionValues,
      );
      saveDefaults(config, action);
      setCreationAction(action);
      const ctx = investigation
        ? { ...investigation, prompt: promptOverride ?? editedPrompt, images: initialImages, files: initialFiles }
        : undefined;
      await onCreate(config, ctx, action);
      clearPromptDraft();
    } catch (error) {
      window.alert(error instanceof Error ? error.message : String(error));
    } finally {
      creatingRef.current = false;
      setCreating(false);
    }
  };

  const handlePromptKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter") return;
    if (event.shiftKey || event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
    event.preventDefault();
    if (createDisabled) return;
    void handleCreate(creationAction);
  };

  // The modal closes only through an explicit cancel (Cancel button, header
  // ×, back button) or a successful create — never on a stray click outside
  // it. Cancelling with unsent text asks whether to keep the draft.
  const requestCancel = () => {
    if (creating) return;
    if (!investigation && initialPrompt.trim()) {
      setDiscardPromptOpen(true);
      return;
    }
    onClose();
  };

  useBackButtonDismiss(open, requestCancel);

  const handlePromptPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const files = imageFilesFromClipboard(e.clipboardData);
    if (files.length === 0) return;
    e.preventDefault();
    addAttachments(files);
  };

  const focusPromptTextarea = () => {
    document
      .querySelector<HTMLTextAreaElement>(`[data-testid="${NEW_SESSION_PROMPT_TESTID}"]`)
      ?.focus();
  };

  const appendDictation = (text: string): string => {
    const clean = text.trim();
    if (!clean) return promptRef.current;
    const next = promptRef.current ? `${promptRef.current} ${clean}` : clean;
    promptRef.current = next;
    dictatedRef.current = dictatedRef.current ? `${dictatedRef.current} ${clean}` : clean;
    if (investigation) setEditedPrompt(next);
    else setInitialPrompt(next);
    return next;
  };

  // Voice actions land in this modal's prompt instead of the chat composer draft.
  const handleVoiceAction = (action: VoiceCommandAction) => {
    if (action.type === "speak") {
      speakVoiceText(action.text);
      return;
    }
    // Already creating a session here — no nested new-session command.
    if (action.type === "new-session") return;
    if (action.type === "open-prompt") {
      focusPromptTextarea();
      return;
    }
    if (action.type === "append-draft") {
      appendDictation(action.text);
      focusPromptTextarea();
      return;
    }
    const next = appendDictation(dictationDelta(action.text, dictatedRef.current));
    if (!next.trim() || creating || !(cwd || defaultCwd)) return;
    void handleCreate("send-and-open", next);
  };

  const renderExtensionOption = (
    option: NewSessionExtensionOption,
    nested = false,
  ) => {
    const key = extensionOptionKey(option);
    const checked = extensionOptionValues[key] ?? option.defaultValue;
    return (
      <div key={key}>
        <label className={`browser-harness-toggle${nested ? " browser-harness-sub-toggle" : ""}`}>
          <input
            type="checkbox"
            checked={checked}
            onChange={(e) => {
              const nextValue = e.target.checked;
              setExtensionOptionValues((prev) => ({
                ...prev,
                [key]: nextValue,
              }));
            }}
          />
          {option.label}
        </label>
        {checked && option.children?.map((child) => renderExtensionOption(child, true))}
      </div>
    );
  };

  if (!open) return null;

  return (
    <>
    <div className="modal-overlay ns-session-overlay">
      <div className="modal-content ns-session-modal">
        <div className="modal-header">
          <h2>{t("newSession.title")}</h2>
          <button className="modal-close" onClick={requestCancel} disabled={creating}>
            <Icon name="x" size={16} />
          </button>
        </div>
        <div className="modal-body">
          <div className="ns-modal-section">
            <div className="ns-modal-section-title">{t("newSession.initialPrompt", "Initial Prompt")}</div>
            <ComposerImagePreviews
              images={promptImages}
              className="ns-initial-attachments"
              onRemove={(index) => {
                setInitialImages((previous) => previous.filter((_, imageIndex) => imageIndex !== index));
              }}
            />
            {promptFiles.length > 0 && (
              <div className="file-previews ns-initial-attachments">
                {promptFiles.map((file, i) => (
                  <div key={`ns-file-${i}`} className="file-preview-item">
                    <span className="file-preview-name">{file.name}</span>
                    <span className="file-preview-size">{formatFileSize(file.size)}</span>
                    <button
                      type="button"
                      className="file-remove-btn"
                      onClick={() => setInitialFiles((prev) => prev.filter((_, index) => index !== i))}
                      title={t("input.removeImageTitle")}
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            )}
            <textarea
              className="ns-investigation-textarea"
              data-testid={NEW_SESSION_PROMPT_TESTID}
              value={promptText}
              onChange={(e) => investigation ? setEditedPrompt(e.target.value) : setInitialPrompt(e.target.value)}
              onKeyDown={handlePromptKeyDown}
              onPaste={handlePromptPaste}
              rows={4}
            />
            <input
              ref={attachmentInputRef}
              type="file"
              data-testid="new-session-attachment-input"
              multiple
              style={{ display: "none" }}
              onChange={(e) => {
                addAttachments(Array.from(e.target.files || []));
                e.target.value = "";
              }}
            />
            <div className="ns-prompt-actions">
              <button
                type="button"
                className="btn-secondary ns-attach-btn"
                onClick={() => attachmentInputRef.current?.click()}
              >
                <Icon name="paperclip" size={14} /> {t("input.attachTitle")}
              </button>
              <VoiceActivation
                onAction={handleVoiceAction}
                promptTestId={NEW_SESSION_PROMPT_TESTID}
              />
            </div>
          </div>
          {providersOp.error && (
            <div className="ns-providers-error" data-testid="new-session-providers-error">
              <span>{t("newSession.providersLoadError")}</span>
              <button
                type="button"
                className="btn-secondary"
                disabled={providersOp.inflight}
                onClick={loadProviders}
              >
                {providersOp.inflight ? t("newSession.providersRetrying") : t("newSession.providersRetry")}
              </button>
            </div>
          )}
          {effectiveOrchestrationMode === "native" && (
            <RuntimeProfilePicker
              label={t("newSession.sessionRuntimeProfile")}
              role="main"
              providers={activeProviders}
              value={main}
              onChange={setMain}
            />
          )}

          {effectiveOrchestrationMode === "team" && (
            <>
              <RuntimeProfilePicker
                label={t("newSession.managerRuntimeProfile")}
                role="main"
                providers={managerCapableProviders}
                value={main}
                onChange={setMain}
              />
              <RuntimeProfilePicker
                label={t("newSession.workerRuntimeProfile")}
                role="worker"
                providers={activeProviders}
                value={worker}
                onChange={setWorker}
              />
            </>
          )}

          <div className="ns-modal-section">
            <div className="ns-modal-section-title">{t("newSession.project")}</div>
            <div className="ns-modal-row">
              <label>{t("newSession.projectLabel")}</label>
              <select
                value={cwd}
                onChange={(e) => {
                  const next = e.target.value;
                  setCwd(next);
                  // Sync the machine picker to the project's node so a
                  // remote-machine project never silently falls back to
                  // "primary" (where `next` doesn't exist on disk). Only
                  // overrides when the picked path matches a known
                  // project AND the user hasn't explicitly touched the
                  // machine picker this modal session — keeps the
                  // synthetic-custom path (e.g. one the Ask agent
                  // invented) on whatever node the user already chose,
                  // and respects an explicit machine override.
                  if (nodeIdTouchedRef.current) return;
                  const owner = projects.find((p) => p.path === next)?.node_id;
                  if (owner) setNodeId(owner);
                }}
              >
                {/* Disabled placeholder when cwd is empty (fresh install
                    with no projects yet, or sidebar had no project
                    selected). Forces the user to pick before Create
                    enables, avoids the browser-default-first-option
                    visual/state desync. */}
                {!cwd && (
                  <option value="" disabled>
                    {t("newSession.pickProject")}
                  </option>
                )}
                {/* Synthetic "(custom)" row when the current cwd doesn't
                    match any known project (e.g. the Ask agent proposed
                    a path the user hasn't added as a project yet).
                    Keeps the user's choice visible AND selectable. */}
                {cwd && !projects.some((p) => p.path === cwd) && (
                  <option value={cwd}>{cwd}</option>
                )}
                {projects.map((p) => (
                  <option key={`${p.node_id || "primary"}:${p.path}`} value={p.path}>
                    {p.name} — {p.path}
                  </option>
                ))}
              </select>
            </div>
            {(cwd || defaultCwd) && (
              <div className="ns-modal-row">
                <label>{t("newSession.folder", "Folder")}</label>
                <button
                  type="button"
                  className="ns-folder-trigger"
                  onClick={(e) => setFolderPopover(e.currentTarget.getBoundingClientRect())}
                >
                  <Icon name="folder" size={14} />
                  <span className="ns-folder-trigger-label">{selectedFolderLabel}</span>
                  <Icon name="chevron-down" size={12} />
                </button>
              </div>
            )}
          </div>
          <div className="ns-modal-section">
            {showOrchestrationPicker && (
              <>
                <div className="ns-modal-section-title">{t("newSession.orchestration")}</div>
                <div className="ns-modal-orch-buttons">
                  {availableOrchestrationModes.map((mode) => {
                    const label = mode === "team"
                      ? t("orchestration.managerWorkers")
                      : t("orchestration.nativeDirect");
                    return (
                      <button
                        key={mode}
                        className={`ns-modal-orch-btn ${effectiveOrchestrationMode === mode ? "active" : ""}`}
                        onClick={() => setOrchestrationMode(mode)}
                      >
                        {label}
                      </button>
                    );
                  })}
                </div>
              </>
            )}
            {sessionExtensionOptions.map((option) => renderExtensionOption(option))}
          </div>

          <div className="ns-modal-section">
            <label className="browser-harness-toggle" title={!fileEditExtensionEnabled ? t("harnessProfile.fileEditDisabledHint") : undefined}>
              <input
                type="checkbox"
                checked={fileEditEnabled}
                disabled={!fileEditExtensionEnabled}
                onChange={(e) => setFileEditEnabled(e.target.checked)}
              />
              {t("newSession.fileEdit")}
            </label>
            {!fileEditExtensionEnabled ? (
              <span className="ns-modal-hint">{t("harnessProfile.fileEditDisabledHint")}</span>
            ) : null}
          </div>

          <div className="ns-modal-section">
            <HarnessProfileSelector
              value={harnessProfileId}
              className="ns-modal-row harness-profile-selector"
              disabled={creating}
              onChange={setHarnessProfileId}
            />
          </div>

          {showPicker && (
            <MachineNodePicker
              machines={machines}
              localNodeId={localNodeId}
              value={nodeId}
              onChange={(id) => {
                // Flag the explicit user touch so the project picker's
                // auto-sync stops overriding this choice. See
                // `nodeIdTouchedRef` and the project picker's onChange.
                nodeIdTouchedRef.current = true;
                setNodeId(id);
              }}
            />
          )}

        </div>
        <div className="modal-footer ns-create-actions">
          <button className="btn-secondary" onClick={requestCancel} disabled={creating}>
            {t("newSession.cancel")}
          </button>
          <NewSessionCreateButton
            selectedAction={creationAction}
            labels={{
              create: t("newSession.create"),
              send: t("newSession.createAndSend"),
              "send-and-open": t("newSession.createAndSendAndOpen"),
            }}
            loadingLabel={t("newSession.creating")}
            disabled={createDisabled}
            creating={creating}
            onAction={handleCreate}
          />
        </div>
      </div>
    </div>
    {folderPopover && (cwd || defaultCwd) && (
      <SessionFolderPopover
        anchor={folderPopover}
        folders={folders}
        assignedFolderId={folderId}
        onSelect={(id) => setFolderId(id)}
        onCreateFolder={async (name) => {
          const pid = cwd || defaultCwd;
          if (!pid) return;
          try {
            const created = await createSessionFolder(pid, name);
            setFolders((prev) => [...prev, created]);
            setFolderId(created.id);
          } catch {
            // leave the picker as-is; the folder just wasn't created
          }
        }}
        onClose={() => setFolderPopover(null)}
      />
    )}
    <ConfirmModal
      open={discardPromptOpen}
      title={t("newSession.keepDraftTitle")}
      message={t("newSession.keepDraftMessage")}
      confirmLabel={t("newSession.keepDraftDiscard")}
      cancelLabel={t("newSession.keepDraftKeep")}
      onConfirm={() => {
        clearPromptDraft();
        setDiscardPromptOpen(false);
        onClose();
      }}
      onCancel={() => {
        setDiscardPromptOpen(false);
        onClose();
      }}
      onDismiss={() => setDiscardPromptOpen(false)}
    />
    </>
  );
}

export type { SessionConfig, RuntimeProfile };
