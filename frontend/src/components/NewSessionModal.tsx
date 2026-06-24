import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { useTranslation } from "react-i18next";
import type {
  CapabilityContext,
  FileAttachment,
  NodeSnapshot,
  OrchestrationMode,
  PastedImage,
  Project,
  Provider,
  ReasoningEffort,
} from "../types";
import {
  ProviderCapabilityPicker,
  type ProviderConfigSyncApiClient,
  type ProviderConfigSyncCapabilityPickerOutput,
  type ProviderConfigSyncCapabilityPickerSource,
} from "@better-agent/provider-config-sync-ui";
import { trackedFetch, useOpProgress } from "../progress/store";
import { useMachines } from "../hooks/useMachines";
import { useLocalNodeId } from "../hooks/useLocalNodeId";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";

import { API, fetchSessionOrganization, createSessionFolder } from "../api";
import { ProgressButton } from "../progress/ProgressButton";
import Icon from "./Icon";
import { SessionFolderPopover } from "./SessionFolderPopover";
import type { PopoverAnchor } from "./SessionTagPopover";
import { buildFolderPathMap } from "../sessionFolders";
import type { SessionFolder } from "../types";
import { fileToAttachment } from "../utils/fileAttach";
import { fileToPastedImage, imageFilesFromClipboard } from "../utils/imageAttach";
import {
  cacheProviderModels,
  cacheProviders,
  readProviderCache,
} from "../utils/providerCache";

interface RoleConfig {
  providerId: string;
  model: string;
  reasoningEffort: ReasoningEffort | "";
}

interface SessionConfig {
  orchestrationMode: OrchestrationMode;
  main: RoleConfig;
  worker: RoleConfig;
  cwd: string;
  browserHarnessEnabled: boolean;
  browserHarnessHeadless: boolean;
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
  capabilityContexts: CapabilityContext[];
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

interface Props {
  open: boolean;
  onClose: () => void;
  onCreate: (config: SessionConfig, investigation?: InvestigationContext) => void;
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
  capabilityPickerClient: Pick<ProviderConfigSyncApiClient, "listCapabilityPickerSources">;
  teamEnabled?: boolean;
  machineNodesEnabled?: boolean;
  browserHarnessEnabled?: boolean;
  extensionOptions?: NewSessionExtensionOption[];
}

const STORAGE_KEY = "better-agent-new-session-defaults";
const EMPTY_EXTENSION_OPTIONS: NewSessionExtensionOption[] = [];

function loadDefaults(): Partial<SessionConfig> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function saveDefaults(config: SessionConfig) {
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      orchestrationMode: config.orchestrationMode,
      main: config.main,
      worker: config.worker,
      browserHarnessEnabled: config.browserHarnessEnabled,
      browserHarnessHeadless: config.browserHarnessHeadless,
      folderId: config.folderId,
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
  saved: Partial<SessionConfig>,
): Record<string, NewSessionExtensionOptionValue> {
  const values: Record<string, NewSessionExtensionOptionValue> = {};
  for (const option of flattenExtensionOptions(options)) {
    const key = extensionOptionKey(option);
    if (option.id === "browser_harness_enabled") {
      values[key] = saved.browserHarnessEnabled ?? option.defaultValue;
      continue;
    }
    if (option.id === "browser_harness_headless") {
      values[key] = saved.browserHarnessHeadless ?? option.defaultValue;
      continue;
    }
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

function capabilityContextFromPickerSource(
  source: ProviderConfigSyncCapabilityPickerSource,
  output?: ProviderConfigSyncCapabilityPickerOutput,
): CapabilityContext {
  const outputs = output ? [output] : source.outputs;
  return {
    source_id: source.source_id,
    capability_id: source.capability.capability_id,
    name: source.capability.name,
    category: source.capability.category,
    outputs: outputs
      .filter((item) => item.content && !item.render_error)
      .map((item) => ({
        provider_kind: item.provider_kind,
        provider_name: item.provider_name,
        content_kind: item.content_kind,
        content: item.content,
      })),
  };
}

function resolveReasoningEffort(
  saved: RoleConfig | undefined,
  provider: Provider,
  role: "main" | "worker",
): ReasoningEffort | "" {
  const options = provider.reasoning_effort_options ?? [];
  if (options.length === 0) return "";
  const savedEffort = saved?.providerId === provider.id ? saved.reasoningEffort : "";
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

export function resolveRoleConfig(
  saved: RoleConfig | undefined,
  providers: Provider[],
  defaultProviderId: string | null,
  modelsByProvider: Record<string, string[]>,
  role: "main" | "worker",
): RoleConfig {
  const provider =
    providers.find((item) => item.id === saved?.providerId)
    ?? providers.find((item) => item.id === defaultProviderId);
  if (!provider) return { providerId: "", model: "", reasoningEffort: "" };

  const models = modelsByProvider[provider.id] ?? [];
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
  const model =
    candidates.find((m) => m && (models.length === 0 || models.includes(m)))
    || models[0]
    || "";
  return {
    providerId: provider.id,
    model,
    reasoningEffort: resolveReasoningEffort(saved, provider, role),
  };
}

function ProviderModelPicker({
  label,
  role,
  providers,
  value,
  onChange,
}: {
  label: string;
  role: "main" | "worker";
  providers: Provider[];
  value: RoleConfig;
  onChange: (v: RoleConfig) => void;
}) {
  const { t } = useTranslation();
  const [models, setModels] = useState<string[]>([]);
  const [prevProviderId, setPrevProviderId] = useState("");
  const selectedProvider = providers.find((p) => p.id === value.providerId);

  useEffect(() => {
    if (!value.providerId) {
      setModels([]);
      return;
    }
    if (value.providerId === prevProviderId) return;
    setPrevProviderId(value.providerId);
    const cachedModels = readProviderCache()?.modelsByProvider[value.providerId] ?? [];
    setModels(cachedModels);
    trackedFetch(
      `providers:fetchModels:${value.providerId}`,
      `${API}/api/providers/${value.providerId}/models`,
    )
      .then((r) => r.json())
      .then((d) => {
        const list: string[] = d.models || [];
        cacheProviderModels(value.providerId, list);
        setModels(list);
        if (list.length && !list.includes(value.model)) {
          onChange({ ...value, model: list[0] });
        }
      })
      .catch(() => {});
  }, [value.providerId]);

  return (
    <div className="ns-modal-section">
      <div className="ns-modal-section-title">{label}</div>
      <div className="ns-modal-row">
        <label>{t("newSession.provider")}</label>
        <select
          value={value.providerId}
          onChange={(e) => {
            const p = providers.find((pr) => pr.id === e.target.value);
            onChange({
              providerId: e.target.value,
              model: p?.last_model || p?.default_model || "",
              reasoningEffort: p ? resolveReasoningEffort(undefined, p, role) : "",
            });
          }}
        >
          {providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </div>
      <div className="ns-modal-row">
        <label>{t("newSession.model")}</label>
        <select
          value={value.model}
          onChange={(e) => onChange({ ...value, model: e.target.value })}
        >
          {models.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
          {!models.length && (
            <option value={value.model}>{value.model || "—"}</option>
          )}
        </select>
      </div>
      {selectedProvider?.reasoning_effort_options?.length ? (
        <div className="ns-modal-row">
          <label>{t("newSession.reasoningEffort")}</label>
          <select
            value={value.reasoningEffort}
            onChange={(e) => onChange({ ...value, reasoningEffort: e.target.value as ReasoningEffort })}
          >
            {selectedProvider.reasoning_effort_options.map((effort) => (
              <option key={effort} value={effort}>
                {t(`reasoningEffort.${effort}`)}
              </option>
            ))}
          </select>
        </div>
      ) : null}
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
            return (
              <option key={m.id} value={m.id}>
                {m.id}
                {isLocal ? ` (${t("newSession.machinePrimary")})` : ""}
                {offline ? ` — ${t("newSession.machineOffline")}` : ""}
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
  capabilityPickerClient,
  teamEnabled = true,
  machineNodesEnabled = true,
  browserHarnessEnabled: browserHarnessExtensionEnabled = true,
  extensionOptions = EMPTY_EXTENSION_OPTIONS,
}: Props) {
  const { t } = useTranslation();
  useBackButtonDismiss(open, onClose);
  const { inflight: creating } = useOpProgress("session:create");
  const [providers, setProviders] = useState<Provider[]>([]);
  const [editedPrompt, setEditedPrompt] = useState("");
  const [initialPrompt, setInitialPrompt] = useState("");
  const [initialImages, setInitialImages] = useState<PastedImage[]>([]);
  const [initialFiles, setInitialFiles] = useState<FileAttachment[]>([]);
  const [capabilityContexts, setCapabilityContexts] = useState<CapabilityContext[]>([]);
  const [capabilityPickerOpen, setCapabilityPickerOpen] = useState(false);
  const attachmentInputRef = useRef<HTMLInputElement>(null);
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

  const [orchestrationMode, setOrchestrationMode] = useState<OrchestrationMode>(
    teamEnabled ? "team" : "native",
  );
  const [main, setMain] = useState<RoleConfig>({ providerId: "", model: "", reasoningEffort: "" });
  const [worker, setWorker] = useState<RoleConfig>({ providerId: "", model: "", reasoningEffort: "" });
  const sessionExtensionOptions = useMemo<NewSessionExtensionOption[]>(
    () => [
      ...(
        browserHarnessExtensionEnabled
          ? [
              {
                id: "browser_harness_enabled",
                extensionId: "ofek-dev.browser-harness",
                label: t("orchestration.browserHarness"),
                defaultValue: true,
                applyToSessionConfig: (value: NewSessionExtensionOptionValue) => ({ browserHarnessEnabled: value }),
                children: [
                  {
                    id: "browser_harness_headless",
                    extensionId: "ofek-dev.browser-harness",
                    label: t("orchestration.browserHarnessHeadless"),
                    defaultValue: true,
                    applyToSessionConfig: (value: NewSessionExtensionOptionValue) => ({ browserHarnessHeadless: value }),
                  },
                ],
              },
            ]
          : []
      ),
      ...extensionOptions,
    ],
    [browserHarnessExtensionEnabled, extensionOptions, t],
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
    setEditedPrompt(investigation?.prompt ?? "");
    setInitialPrompt("");
    setInitialImages(investigation?.images ?? []);
    setInitialFiles(investigation?.files ?? []);
    setCapabilityContexts([]);
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
    const defaults = loadDefaults();
    setOrchestrationMode(
      teamEnabled
        ? defaults.orchestrationMode || "team"
        : "native",
    );
    setExtensionOptionValues(extensionOptionDefaults(sessionExtensionOptions, defaults));
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
      setMain(resolveRoleConfig(defaults.main, cached.providers, cached.defaultProviderId, cached.modelsByProvider, "main"));
      setWorker(resolveRoleConfig(defaults.worker, cached.providers, cached.defaultProviderId, cached.modelsByProvider, "worker"));
    }
    trackedFetch("providers:list", `${API}/api/providers`)
      .then((r) => r.json())
      .then((d) => {
        const list: Provider[] = d.providers || [];
        const activeId: string | null = d.default_provider_id;
        cacheProviders(list, activeId);
        setProviders(list);
        const modelsByProvider = readProviderCache()?.modelsByProvider ?? {};
        setMain(resolveRoleConfig(defaults.main, list, activeId, modelsByProvider, "main"));
        setWorker(resolveRoleConfig(defaults.worker, list, activeId, modelsByProvider, "worker"));
      })
      .catch(() => {});
  }, [open, browserHarnessExtensionEnabled, sessionExtensionOptions]);

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

  // Capability gating: only manager-capable providers can drive the
  // persistent "manager" session in manager mode. If the user has no
  // such provider configured, the "manager" button is disabled and
  // the modal forces "native". The main-role provider picker also
  // filters to capable providers when in manager mode so the user
  // can't pick a Gemini as the manager.
  const managerCapableProviders = providers.filter(
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
    const cur = providers.find((p) => p.id === main.providerId);
    if (cur && cur.supports_manager_mode) return;
    const fb = managerCapableProviders[0];
    if (fb) {
      setMain({
        providerId: fb.id,
        model: fb.default_model,
        reasoningEffort: resolveReasoningEffort(undefined, fb, "main"),
      });
    }
  }, [effectiveOrchestrationMode, main.providerId, providers, managerCapableProviders]);

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
  const promptImages = initialImages;
  const promptFiles = initialFiles;
  const folderPathMap = useMemo(() => buildFolderPathMap(folders), [folders]);
  const selectedFolderLabel = folderId
    ? (folderPathMap.get(folderId) ?? t("session.unfiled"))
    : t("session.unfiled");

  const handleCreate = () => {
    const effectiveCwd = cwd || defaultCwd;
    const baseConfig: SessionConfig = {
      orchestrationMode: effectiveOrchestrationMode,
      main,
      worker,
      cwd: effectiveCwd,
      browserHarnessEnabled: false,
      browserHarnessHeadless: true,
      fileEditEnabled,
      fileEditPath: undefined,
      nodeId,
      initialPrompt,
      initialImages,
      initialFiles,
      capabilityContexts,
      folderId,
    };
    const config = applyExtensionOptionsToSessionConfig(
      baseConfig,
      sessionExtensionOptions,
      extensionOptionValues,
    );
    saveDefaults(config);
    const ctx = investigation
      ? { ...investigation, prompt: editedPrompt, images: initialImages, files: initialFiles }
      : undefined;
    onCreate(config, ctx);
  };

  const handlePromptKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== "Enter" || e.shiftKey) return;
    e.preventDefault();
    if (!(cwd || defaultCwd) || creating) return;
    handleCreate();
  };

  const handlePromptPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const files = imageFilesFromClipboard(e.clipboardData);
    if (files.length === 0) return;
    e.preventDefault();
    addAttachments(files);
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
    <div className="modal-overlay" onClick={creating ? undefined : onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{t("newSession.title")}</h2>
          <button className="modal-close" onClick={creating ? undefined : onClose} disabled={creating}>
            <Icon name="x" size={16} />
          </button>
        </div>
        <div className="modal-body">
          <div className="ns-modal-section">
            <div className="ns-modal-section-title">{t("newSession.initialPrompt", "Initial Prompt")}</div>
            {promptImages.length > 0 && (
              <div className="image-previews ns-initial-attachments">
                {promptImages.map((img, i) => (
                  <div key={`ns-img-${i}`} className="image-preview-item">
                    <img
                      src={img.dataUrl}
                      alt={t("input.attachedImageAlt", { index: i + 1, defaultValue: `Attached image ${i + 1}` })}
                    />
                    <button
                      type="button"
                      className="image-remove-btn"
                      onClick={() => setInitialImages((prev) => prev.filter((_, index) => index !== i))}
                      title={t("input.removeImageTitle")}
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            )}
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
            <button
              type="button"
              className="btn-secondary ns-attach-btn"
              onClick={() => attachmentInputRef.current?.click()}
            >
              <Icon name="paperclip" size={14} /> {t("input.attachTitle")}
            </button>
          </div>
          <div className="ns-modal-section">
            <div className="ns-modal-section-title">{t("newSession.capabilities", "Capabilities")}</div>
            <button
              type="button"
              className="btn-secondary ns-attach-btn"
              onClick={() => setCapabilityPickerOpen(true)}
            >
              <Icon name="sparkles" size={14} /> {t("newSession.addCapability", "Add capability")}
            </button>
            {capabilityContexts.length > 0 && (
              <div className="capability-context-list">
                {capabilityContexts.map((capability) => (
                  <span key={capability.source_id} className="capability-context-chip">
                    {capability.name}
                    <button
                      type="button"
                      onClick={() => setCapabilityContexts((prev) => prev.filter((item) => item.source_id !== capability.source_id))}
                      aria-label={`Remove ${capability.name}`}
                    >
                      ×
                    </button>
                  </span>
                ))}
              </div>
            )}
          </div>
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
            <label className="browser-harness-toggle">
              <input
                type="checkbox"
                checked={fileEditEnabled}
                onChange={(e) => setFileEditEnabled(e.target.checked)}
              />
              {t("newSession.fileEdit")}
            </label>
          </div>

          {effectiveOrchestrationMode === "native" && (
            <ProviderModelPicker
              label={t("newSession.sessionProvider")}
              role="main"
              providers={providers}
              value={main}
              onChange={setMain}
            />
          )}

          {effectiveOrchestrationMode === "team" && (
            <>
              <ProviderModelPicker
                label={t("newSession.managerProvider")}
                role="main"
                providers={managerCapableProviders}
                value={main}
                onChange={setMain}
              />
              <ProviderModelPicker
                label={t("newSession.workerProvider")}
                role="worker"
                providers={providers}
                value={worker}
                onChange={setWorker}
              />
            </>
          )}

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
        <div className="modal-footer">
          <button className="btn-secondary" onClick={onClose} disabled={creating}>
            {t("newSession.cancel")}
          </button>
          <ProgressButton
            className="btn-primary"
            opId="session:create"
            onClick={handleCreate}
            extraDisabled={!(cwd || defaultCwd)}
            loadingChildren={t("newSession.creating")}
          >
            {t("newSession.create")}
          </ProgressButton>
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
    {capabilityPickerOpen && (
      <div className="modal-overlay capability-picker-overlay" onClick={() => setCapabilityPickerOpen(false)}>
        <div className="modal-content capability-picker-modal" onClick={(e) => e.stopPropagation()}>
          <ProviderCapabilityPicker
            open
            cwd={cwd || defaultCwd}
            client={capabilityPickerClient}
            onClose={() => setCapabilityPickerOpen(false)}
            onSelect={(source, output) => {
              const next = capabilityContextFromPickerSource(source, output);
              if (next.outputs.length === 0) return;
              setCapabilityContexts((prev) => [
                next,
                ...prev.filter((item) => item.source_id !== next.source_id),
              ]);
              setCapabilityPickerOpen(false);
            }}
          />
        </div>
      </div>
    )}
    </>
  );
}

export type { SessionConfig, RoleConfig };
