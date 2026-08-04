import type {
  ChatMessage,
  CredentialConsent,
  FileDiscussion,
  ModelCatalog,
  PendingApproval,
  Project,
  Provider,
  Session,
  Trace,
  UserInteractionRequest,
  WorkerInfo,
  WSEvent,
} from "../../src/types";
import type { ProjectSuggestion } from "../../src/components/ProjectSuggestionModal";
import type { BuiltinExtensionKey } from "../../src/extensionIds";
import { finalizeTerminalAssistant } from "../../src/hooks/useSession";

/** Logical key -> extension id served by GET /api/extensions/builtin-ids.
 *  Single source for the `/api/extensions/<id>/backend/...` proxy family:
 *  a known id re-dispatches onto the canonical /api/... handler below, so
 *  each endpoint keeps exactly one mock implementation. */
export const TEST_BUILTIN_EXTENSION_IDS: Record<BuiltinExtensionKey, string> = {
  ask: "ofek-dev.ask",
  team: "ofek-dev.team-orchestration",
  supervisor: "ofek-dev.supervisor",
  projectStructure: "ofek-dev.project-structure",
  machineNodes: "ofek-dev.machine-nodes",
  credentialBroker: "ofek-dev.credential-broker",
  canvas: "ofek-dev.canvas",
  promptEngineer: "ofek-dev.prompt-engineer",
  browserHarness: "ofek-dev.browser-harness",
  agentBoard: "ofek-dev.agent-board",
  requirements: "ofek-dev.requirements",
  sessionBridge: "ofek-dev.session-bridge",
  testape: "ofek-dev.testape",
  scheduler: "ofek-dev.scheduler",
  routines: "ofek-dev.routines",
};

const TEST_EXTENSION_ID_SET = new Set<string>(
  Object.values(TEST_BUILTIN_EXTENSION_IDS),
);

/** Known-id `/api/extensions/<id>/backend/<rest>` requests normalize to the
 *  canonical `/api/<rest>` path before recording and routing, so each
 *  endpoint has exactly one mock handler and restCalls stay stable against
 *  the proxy prefix. Empty/unknown ids pass through and 404 in route(). */
function canonicalizeExtensionBackendPath(path: string): string {
  const match = path.match(/^\/api\/extensions\/([^/]+)\/backend(\/.*)$/);
  if (match && TEST_EXTENSION_ID_SET.has(decodeURIComponent(match[1]))) {
    return `/api${match[2]}`;
  }
  return path;
}

export interface InstallationProfileState {
  status: string;
  setup_required: boolean;
  mode: string | null;
  provider_conversations_enabled: boolean;
  mobile_enabled: boolean;
  integrations_enabled: boolean;
}

export interface BackendState {
  sessions: Session[];
  projects: Project[];
  workers: WorkerInfo[];
  approvals: PendingApproval[];
  credentials: CredentialConsent[];
  userInputs: UserInteractionRequest[];
  traces: Record<string, Trace>;
  models: { id: string; name: string }[];
  providers: Provider[];
  default_provider_id: string | null;
  installationProfile: InstallationProfileState;
  config: Record<string, unknown>;
  capabilitySources: unknown[];
  projectSuggestion: ProjectSuggestion | null;
  summaryMissOnceIds: string[];
  summaryMissingIds: string[];
  uiSelection: {
    selected_project: { path: string; node_id: string } | null;
    remembered_session_by_project: Record<string, Record<string, string>>;
    open_session_tab_ids: string[];
    open_session_tab_joined_at: Record<string, string>;
  };
  /** GET/PATCH `/api/user-prefs`. Without a route for this, App's
   * mount-time `progressTrackedFetch("userPrefs:load", ...)` throws
   * "unhandled", which `fetchWithRetry` treats as a network failure and
   * retries 3x with real 1s/2s/4s backoff — timers that outlive the test
   * and fire a stray fetch against whatever mock is installed later. */
  userPrefs: Record<string, unknown>;
  /** Mocked file content keyed by absolute path. FileEditor polls
   * GET /api/file?path=... while the prompt-engineering overlay is up;
   * tests can pre-seed paths or watch them get fetched. */
  files: Record<string, string>;
}

function modelCatalog(
  provider: Provider,
  discovered: BackendState["models"],
): ModelCatalog {
  const models = Array.from(new Set([
    provider.last_model,
    provider.default_model,
    ...(provider.custom_models ?? []),
    ...discovered.map((model) => model.id),
  ].filter((model): model is string => typeof model === "string" && model.length > 0)));
  return {
    provider_id: provider.id,
    provider_generation: `mock:${provider.id}`,
    authoritative: provider.kind === "codex" || provider.kind === "fugu",
    status: "current",
    models,
    models_current: true,
    retired: [],
    retired_models: [],
    last_refreshed_at: 1,
    reason: "",
    authority_fingerprint: `mock:${provider.id}`,
    last_known_good: null,
    runtime_profiles: [],
  };
}

export interface RestCall {
  method: string;
  path: string;
  query: Record<string, string>;
  body: unknown;
  credentials?: RequestCredentials;
}

const ORIGIN = "http://localhost:8000";

function splitFilter(value: string | undefined): Set<string> {
  if (!value) return new Set();
  return new Set(value.split(",").map((item) => item.trim()).filter(Boolean));
}

function sessionMatchesListQuery(s: Session, query: Record<string, string>): boolean {
  if (query.show_archived !== "true" && s.archived) return false;
  if (query.project_path && s.cwd !== query.project_path) return false;
  if (query.folder_id && (s.folder_id ?? "") !== query.folder_id) return false;
  const tagIds = splitFilter(query.tag_ids);
  if (tagIds.size > 0) {
    const have = new Set([
      ...(s.session_tags ?? []).map((tag) => tag.id),
      ...(s.requirement_tags ?? []).map((tag) => `req:${tag.kind}:${tag.id}`),
    ]);
    for (const id of tagIds) {
      if (!have.has(id)) return false;
    }
  }
  const providerIds = splitFilter(query.provider_ids);
  if (providerIds.size > 0 && !providerIds.has(s.provider_id ?? "")) return false;
  const modelIds = splitFilter(query.model_ids);
  if (modelIds.size > 0 && !modelIds.has(s.model ?? "")) return false;
  const modes = splitFilter(query.modes);
  if (modes.size > 0 && !modes.has(s.orchestration_mode ?? "team")) return false;
  if (query.file_edit_mode === "true" && s.working_mode !== "file_editing") return false;
  if (query.file_edit_mode === "false" && s.working_mode === "file_editing") return false;
  const search = (query.search ?? "").trim().toLowerCase();
  if (search) {
    const fields = [s.name, s.cwd, s.model, s.provider_id, s.orchestration_mode];
    if (!fields.some((field) => field?.toLowerCase().includes(search))) return false;
  }
  return true;
}

function sessionSummary(s: Session): Partial<Session> {
  const summary: Partial<Session> = { ...s };
  delete summary.messages;
  delete summary.forks;
  delete summary.token_usage_total;
  delete summary.token_usage_last;
  return summary;
}

function emptyState(): BackendState {
  return {
    sessions: [],
    projects: [],
    workers: [],
    approvals: [],
    credentials: [],
    userInputs: [],
    traces: {},
    models: [
      { id: "claude-sonnet-4-6", name: "Sonnet 4.6" },
      { id: "claude-opus-4-7", name: "Opus 4.7" },
      { id: "claude-opus-4-8", name: "Opus 4.8" },
      { id: "claude-fable-5", name: "Fable 5" },
    ],
    providers: [{
      id: "codex",
      name: "Codex",
      kind: "codex",
      mode: "subscription",
      base_url: "",
      config_dir: "",
      custom_models: [],
      default_model: "claude-sonnet-4-6",
      runner: "native",
      runner_options: ["native"],
      reasoning_effort_options: [],
      default_reasoning_effort: "",
      has_api_key: true,
      suspended: false,
      supports_fork: true,
      supports_manager_mode: true,
      supports_rewind: true,
      supports_steering: true,
      supports_native_subagents: false,
      supports_reasoning_effort: false,
    }],
    default_provider_id: "codex",
    installationProfile: {
      status: "active",
      setup_required: false,
      mode: "default",
      provider_conversations_enabled: true,
      mobile_enabled: true,
      integrations_enabled: true,
    },
    config: { default_model: "claude-sonnet-4-6" },
    capabilitySources: [],
    projectSuggestion: null,
    summaryMissOnceIds: [],
    summaryMissingIds: [],
    uiSelection: {
      selected_project: null,
      remembered_session_by_project: {},
      open_session_tab_ids: [],
      open_session_tab_joined_at: {},
    },
    // `first_run_wizard_done: true` — a fresh-install `false` would make
    // every renderApp() test navigate to /settings on mount.
    userPrefs: {
      language: "en",
      shortcut_responses: [],
      sessions_tabs_sort: "last_opened_at",
      sessions_tabs_visible: true,
      user_display_name: null,
      first_run_wizard_done: true,
      font_family: "system",
      font_size: 14,
      appearance_theme: "default",
    },
    files: {},
  };
}

export class MockBackend {
  state: BackendState = emptyState();
  calls: RestCall[] = [];
  lastRefreshRequestId: string | null = null;
  restartPostFailure: "before-accept" | "after-accept" | null = null;
  offline = false;
  transientStatus: number | null = null;
  transientStatusPath: string | null = null;
  transientOfflineAfter = false;
  /** Backend-side draft_input_seq per session id. Mirrors the real
   * backend's stale-PATCH guard. Lives outside the Session type
   * because the frontend never reads the seq. */
  draftSeqs: Map<string, number> = new Map();
  private routeHolds: { method: string; path: string | RegExp; promise: Promise<void> }[] = [];
  private originalFetch: typeof fetch | undefined;
  private sessionPageLimit: number | null = null;

  seed(partial: Partial<BackendState>): void {
    this.state = { ...this.state, ...partial };
  }

  /** Cap `GET /api/sessions` responses like the real endpoint does.
   *
   * The real route is `Query(50, ge=1, le=200)`, so a caller that sends no
   * `limit` — which is exactly what `sessionRegistry.bootstrap()` does —
   * gets ONE PAGE, not the whole corpus. This mock defaults to returning
   * every seeded session, which means most harness tests let the frontend
   * observe sessions production would never have returned in that
   * response. Tests that depend on page-boundary behavior opt in here.
   *
   * Flipping this on by default is the correct end state and is tracked
   * separately: several existing suites currently rely on the registry
   * knowing about sessions below page 1, which production cannot
   * guarantee. */
  setSessionPageLimit(limit: number | null): void {
    this.sessionPageLimit = limit;
  }

  /** Keep `state.sessions[].messages` in sync with `messages_replay` /
   *  `messages_delta` WS frames pushed through the harness. The real
   *  backend persists a message before it is ever broadcast over WS, so
   *  REST and WS always agree; here the two are separate mock surfaces
   *  and WS delivery alone never touched `state.sessions`, so a message
   *  that only ever arrived via `emit`/`emitMany` was invisible to `GET
   *  /api/sessions/:id` and silently dropped by any reconcile-style
   *  refetch (e.g. after `turn_complete`). Called by the harness's
   *  `emit`/`emitMany` before delivering the frame to the mock socket. */
  applyWsEvent(event: WSEvent): void {
    if (event.type === "messages_replay" || event.type === "messages_delta") {
      const d = event.data as { app_session_id?: string; messages?: ChatMessage[] };
      if (!d.app_session_id || !Array.isArray(d.messages) || d.messages.length === 0) return;
      const node = this.findSessionNode(d.app_session_id);
      if (!node) return;
      node.messages = mergeMessagesById(node.messages, d.messages);
      return;
    }
    // Same real-backend-consistency reasoning as messages_replay/delta
    // above: `user_message_persisted` is the ack a REAL backend only
    // emits after the message is already durably stored, so a REST
    // reconcile triggered afterward (e.g. applySessionReconciled on
    // turn-terminal) always sees it too. Without this, a test that only
    // `emit`s the ack (never mutates `state.sessions`) has the message
    // vanish the moment any later reconcile refetches the session.
    // Session-level running/monitoring state is backend-owned: the real
    // backend recomputes it, broadcasts the delta, AND serves the same
    // value on every subsequent `/api/sessions` row. Project it onto the
    // mock's store so REST and WS agree the way they do in production —
    // otherwise every REST refresh silently contradicts the WS delta and
    // the harness cannot represent a running session at all.
    if (
      event.type === "session_monitoring_changed" ||
      event.type === "session_running_changed"
    ) {
      const d = event.data as {
        session_id?: string;
        monitoring_state?: string;
        value?: boolean;
      };
      if (!d.session_id) return;
      const node = this.findSessionNode(d.session_id);
      if (!node) return;
      const monitoring = d.monitoring_state
        ?? (d.value ? "active" : "stopped");
      node.monitoring_state = monitoring;
      node.is_running = monitoring !== "stopped";
      return;
    }
    if (event.type === "user_message_persisted") {
      const d = event.data as { session_id?: string; user_message?: ChatMessage };
      if (!d.session_id || !d.user_message) return;
      const node = this.findSessionNode(d.session_id);
      if (!node) return;
      node.messages = mergeMessagesById(node.messages, [d.user_message]);
      return;
    }
    // Turn-terminal frames (turn_complete/turn_stopped/error) finalize the
    // in-flight streaming assistant placeholder. A later REST reconcile
    // (applySessionReconciled) refetches this session, so the mock's
    // stored messages must already reflect the same terminal outcome the
    // frontend computed locally — otherwise the reconcile round-trip
    // resurrects the placeholder the WS event just finalized. Reuses the
    // production reducer so both paths agree by construction.
    if (
      event.type === "turn_complete" ||
      event.type === "turn_stopped" ||
      event.type === "error"
    ) {
      const d = event.data as {
        app_session_id?: string;
        session_id?: string;
        stopped_at?: string;
        interrupted_by_msg_id?: string;
        error?: string;
      };
      const sid = d.app_session_id ?? d.session_id;
      if (!sid) return;
      const node = this.findSessionNode(sid);
      if (!node) return;
      node.messages = finalizeTerminalAssistant(node.messages ?? [], {
        stoppedAt: d.stopped_at,
        interruptedByMsgId: d.interrupted_by_msg_id,
        errorText: event.type === "error" ? (d.error ?? "") : undefined,
      });
    }
  }

  private findSessionNode(appSessionId: string): Session | undefined {
    for (const root of this.state.sessions) {
      const node = findNodeInTree(root, appSessionId);
      if (node) return node;
    }
    return undefined;
  }

  reset(): void {
    this.state = emptyState();
    this.calls = [];
    this.lastRefreshRequestId = null;
    this.restartPostFailure = null;
    this.draftSeqs = new Map();
    this.offline = false;
    this.transientStatus = null;
    this.transientStatusPath = null;
    this.transientOfflineAfter = false;
    this.routeHolds = [];
  }

  setOffline(offline: boolean): void {
    this.offline = offline;
  }

  failNextWithStatus(
    status: number,
    path: string = "/api/sessions",
    offlineAfter: boolean = false,
  ): void {
    this.transientStatus = status;
    this.transientStatusPath = path;
    this.transientOfflineAfter = offlineAfter;
  }

  failRestartPost(position: "before-accept" | "after-accept"): void {
    this.restartPostFailure = position;
  }

  /** Stall the next matching request until the returned callback runs.
   * `path` may be a RegExp — client-minted session ids are not predictable
   * from the test, so exact paths are not always available up front. */
  holdNext(method: string, path: string | RegExp): () => void {
    let release!: () => void;
    const promise = new Promise<void>((resolve) => {
      release = resolve;
    });
    this.routeHolds.push({ method: method.toUpperCase(), path, promise });
    return release;
  }

  install(): void {
    this.originalFetch = globalThis.fetch;
    globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) =>
      this.handle(input, init)) as typeof fetch;
  }

  uninstall(): void {
    if (this.originalFetch) globalThis.fetch = this.originalFetch;
    this.originalFetch = undefined;
  }

  /** Runtime-profile projection DERIVED from the provider seed (one
   * native-runner profile per provider) so harness tests that override
   * providers get matching profiles for free. Mirrors the real backend's
   * snapshot shape. */
  private runtimeProfilesSnapshot() {
    const now = "2026-01-01T00:00:00Z";
    const profiles = this.state.providers.map((provider) => {
      const legacy = provider as unknown as {
        runner?: string;
        default_model?: string;
        default_reasoning_effort?: string;
      };
      return {
        id: `rp-${provider.id}`,
        provider_id: provider.id,
        runner: legacy.runner ?? "native",
        name: provider.name,
        default_model: legacy.default_model ?? "",
        default_reasoning_effort: legacy.default_reasoning_effort ?? "",
        created_at: now,
        updated_at: now,
        deleted_at: null,
      };
    });
    const defaultId = this.state.default_provider_id
      ? `rp-${this.state.default_provider_id}`
      : profiles[0]?.id ?? null;
    return {
      runtime_profiles: profiles,
      default_runtime_profile_id: defaultId,
      deleted_providers: [],
      last_models: {},
      last_reasoning_efforts: {},
    };
  }

  private findMessage(session: Session, messageId: string): unknown {
    const found = session.messages?.find((message) => message.id === messageId);
    if (found) return found;
    for (const fork of session.forks ?? []) {
      const nested = this.findMessage(fork, messageId);
      if (nested) return nested;
    }
    return null;
  }

  private async handle(
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
        ? input.toString()
        : input.url;
    const method = (init?.method ?? "GET").toUpperCase();
    const u = new URL(url, ORIGIN);
    const path = canonicalizeExtensionBackendPath(u.pathname);
    const query: Record<string, string> = {};
    u.searchParams.forEach((v, k) => {
      query[k] = v;
    });
    let body: unknown = undefined;
    if (init?.body && typeof init.body === "string") {
      try {
        body = JSON.parse(init.body);
      } catch {
        body = init.body;
      }
    }
    this.calls.push({ method, path, query, body, credentials: init?.credentials });

    if (this.offline) {
      throw new TypeError("Failed to fetch");
    }

    if (this.transientStatus !== null && this.transientStatusPath === path) {
      const status = this.transientStatus;
      this.transientStatus = null;
      this.transientStatusPath = null;
      if (this.transientOfflineAfter) {
        this.transientOfflineAfter = false;
        this.offline = true;
      }
      return jsonResponse({ detail: `HTTP ${status}` }, status);
    }

    const holdIndex = this.routeHolds.findIndex(
      (h) =>
        h.method === method &&
        (typeof h.path === "string" ? h.path === path : h.path.test(path)),
    );
    if (holdIndex >= 0) {
      const [hold] = this.routeHolds.splice(holdIndex, 1);
      await hold.promise;
    }

    const out = this.route(method, path, query, body);
    return jsonResponse(out);
  }

  private route(
    method: string,
    path: string,
    query: Record<string, string>,
    body: unknown,
  ): unknown {
    if (method === "POST" && path === "/api/admin/restart") {
      const b = body as { request_id?: string };
      if (this.restartPostFailure === "before-accept") {
        this.restartPostFailure = null;
        throw new TypeError("Failed to fetch");
      }
      this.lastRefreshRequestId = b.request_id ?? null;
      if (this.restartPostFailure === "after-accept") {
        this.restartPostFailure = null;
        throw new TypeError("Failed to fetch");
      }
      return { status: "rebuilding", request_id: this.lastRefreshRequestId };
    }
    const restartStatusMatch = path.match(/^\/api\/admin\/restart-status\/([^/]+)$/);
    if (method === "GET" && restartStatusMatch) {
      const requestId = decodeURIComponent(restartStatusMatch[1]);
      const accepted = this.lastRefreshRequestId === requestId;
      return {
        request_id: requestId,
        accepted,
        refresh_result: accepted
          ? {
              request_id: requestId,
              status: "succeeded",
              completed_at: new Date().toISOString(),
              error: null,
            }
          : null,
      };
    }
    if (method === "GET" && path === "/api/build-info") {
      return {
        git_hash: "test-hash",
        refresh_result: this.lastRefreshRequestId
          ? {
              request_id: this.lastRefreshRequestId,
              status: "succeeded",
              completed_at: new Date().toISOString(),
              error: null,
            }
          : null,
      };
    }
    if (method === "GET" && path === "/api/extensions/frontend-entrypoints") {
      // Mirrors the real manifests: team-orchestration serves the workers
      // panel and the worker-approval inline cards from one entry module;
      // credential-broker serves the consent inline cards. The module_urls
      // resolve to tests/harness/extensionModuleStubs.ts via the loader
      // mock in tests/setup.ts.
      return {
        entrypoints: [
          {
            extension_id: "ofek-dev.team-orchestration",
            name: "Team Orchestration",
            frontend_modules: [
              {
                slot: "team-sidebar",
                id: "workers-panel",
                label: "Workers",
                kind: "module",
                module_url: "/api/extensions/ofek-dev.team-orchestration/frontend/ui/team-sidebar.entry.js",
              },
              {
                slot: "chat-inline-actions",
                id: "worker-approvals",
                label: "Worker approvals",
                kind: "module",
                module_url: "/api/extensions/ofek-dev.team-orchestration/frontend/ui/team-sidebar.entry.js",
              },
            ],
          },
          {
            extension_id: "ofek-dev.credential-broker",
            name: "Credential broker",
            frontend_modules: [
              {
                slot: "chat-inline-actions",
                id: "credential-consents",
                label: "Credential consents",
                kind: "module",
                module_url: "/api/extensions/ofek-dev.credential-broker/frontend/ui/credential-broker.entry.js",
              },
            ],
          },
          {
            extension_id: "ofek-dev.prompt-engineer",
            name: "Prompt Engineer",
            frontend_modules: [
              {
                slot: "session-action-modal",
                id: "prompt-engineer-start-modal",
                label: "Prompt Engineer start modal",
                kind: "module",
                module_url: "/api/extensions/ofek-dev.prompt-engineer/frontend/ui/prompt-engineer.entry.js",
              },
              {
                slot: "session-workspace-overlay",
                id: "prompt-engineer-overlay",
                label: "Prompt Engineer overlay",
                kind: "module",
                module_url: "/api/extensions/ofek-dev.prompt-engineer/frontend/ui/prompt-engineer.entry.js",
              },
            ],
          },
        ],
      };
    }
    if (method === "GET" && path === "/api/extensions/builtin-ids") {
      return { ids: { ...TEST_BUILTIN_EXTENSION_IDS } };
    }
    // ---- Mount-time endpoints App fires unconditionally ----
    // Every one of these was previously unhandled, so any renderApp() test
    // threw "MockBackend: unhandled ..." for it. That throw is treated as a
    // real operation failure, which frontendLogger's global console.error
    // patch turns into a `window.setTimeout(send, 0)` POST to
    // /api/logs/frontend — a real timer with no unmount hook that can fire
    // during a LATER, unrelated test and pollute ITS fetch mock/call count.
    if (method === "GET" && path === "/api/startup_tasks") return [];
    if (method === "GET" && path === "/api/extensions/app-settings") {
      return { sections: [] };
    }
    if (method === "GET" && path === "/api/desktop/status") {
      return {
        desktop_shell: false,
        server_url: ORIGIN,
        server_url_source: "test",
        https_available: false,
        https_unavailable_reason: null,
        update_url: `${ORIGIN}/api/desktop/updates`,
        macos: false,
        windows: false,
        update_repo: false,
      };
    }
    if (method === "POST" && path === "/api/logs/frontend") return { ok: true };
    if (method === "GET" && path === "/api/extensions/ui-hooks") return { hooks: {} };
    if (method === "GET" && path === "/api/extensions") {
      // A real installation has the builtin extensions installed+enabled;
      // useBuiltinExtensionFlags matches records by manifest.id against the
      // builtin-ids map, so derive from the same single-source map.
      return {
        extensions: Object.entries(TEST_BUILTIN_EXTENSION_IDS).map(([key, id]) => ({
          manifest: { id, name: key },
          enabled: true,
        })),
      };
    }
    if (method === "GET" && path === "/api/marketplace-bridge") {
      return {
        revision: 0,
        connection_state: "unpaired",
        revocation_pending: false,
        paired_devices: [],
        intents: [],
      };
    }
    if (method === "GET" && path === "/api/session-organization") {
      return { schema_version: 1, folders: [], tags: [], assignments: {}, models: [] };
    }
    if (method === "GET" && path === "/api/git-status") return { is_git: false };
    // Machine-nodes extension backend: App mounts useMachines unconditionally.
    if (method === "GET" && path === "/api/nodes") return [];
    const toolApprovalsMatch = path.match(/^\/api\/sessions\/[^/]+\/tool-approvals\/pending$/);
    if (method === "GET" && toolApprovalsMatch) return { approvals: [] };
    // `/api/extensions/{extension_id}/backend/{path}` proxy family. Known
    // builtin ids were already normalized to the canonical /api/{path} by
    // canonicalizeExtensionBackendPath, so only empty/unknown ids reach
    // here — mirror the real router's 404 (Starlette's path param needs
    // >=1 char; an extension that isn't installed has no backend).
    if (/^\/api\/extensions\/[^/]*\/backend\//.test(path)) return notFound();
    if (method === "GET" && path === "/api/ui-selection") {
      return this.state.uiSelection;
    }
    if (method === "GET" && path === "/api/user-prefs") {
      return this.state.userPrefs;
    }
    if (method === "PATCH" && path === "/api/user-prefs") {
      this.state.userPrefs = { ...this.state.userPrefs, ...(body as Record<string, unknown>) };
      return this.state.userPrefs;
    }
    if (method === "GET" && path === "/api/user-input/pending") {
      return {
        requests: this.state.userInputs.filter(
          (request) =>
            request.status === "pending" &&
            (!query.app_session_id || request.app_session_id === query.app_session_id),
        ),
      };
    }
    const userInputResolveMatch = path.match(/^\/api\/user-input\/([^/]+)\/resolve$/);
    if (method === "POST" && userInputResolveMatch) {
      const requestId = decodeURIComponent(userInputResolveMatch[1]);
      this.state.userInputs = this.state.userInputs.filter(
        (request) => request.request_id !== requestId,
      );
      return { success: true, status: "resolved" };
    }
    const userInputCancelMatch = path.match(/^\/api\/user-input\/([^/]+)\/cancel$/);
    if (method === "POST" && userInputCancelMatch) {
      const requestId = decodeURIComponent(userInputCancelMatch[1]);
      this.state.userInputs = this.state.userInputs.filter(
        (request) => request.request_id !== requestId,
      );
      return { success: true, status: "cancelled" };
    }
    if (method === "PATCH" && path === "/api/ui-selection") {
      const b = body as Partial<BackendState["uiSelection"]> & {
        remembered_session?: {
          path?: string;
          node_id?: string;
          session_id?: string;
        };
      };
      if ("selected_project" in b) {
        this.state.uiSelection.selected_project = b.selected_project ?? null;
      }
      if (b.remembered_session) {
        const path = b.remembered_session.path ?? "";
        const nodeId = b.remembered_session.node_id || "primary";
        const sessionId = b.remembered_session.session_id ?? "";
        if (path && sessionId) {
          this.state.uiSelection.remembered_session_by_project[path] = {
            ...(this.state.uiSelection.remembered_session_by_project[path] ?? {}),
            [nodeId]: sessionId,
          };
        }
      }
      if (Array.isArray(b.open_session_tab_ids)) {
        const seen = new Set<string>();
        this.state.uiSelection.open_session_tab_ids = b.open_session_tab_ids.filter((id) => {
          if (typeof id !== "string" || !id || seen.has(id)) return false;
          seen.add(id);
          return true;
        });
        this.state.uiSelection.open_session_tab_joined_at = Object.fromEntries(
          this.state.uiSelection.open_session_tab_ids.map((id) => [
            id,
            this.state.uiSelection.open_session_tab_joined_at[id] ?? new Date().toISOString(),
          ]),
        );
      }
      if (
        b.open_session_tab_joined_at &&
        typeof b.open_session_tab_joined_at === "object" &&
        !Array.isArray(b.open_session_tab_joined_at)
      ) {
        const joinedAt = b.open_session_tab_joined_at as Record<string, unknown>;
        this.state.uiSelection.open_session_tab_joined_at = Object.fromEntries(
          this.state.uiSelection.open_session_tab_ids
            .map((id) => [id, joinedAt[id]])
            .filter((entry): entry is [string, string] => typeof entry[1] === "string" && Boolean(entry[1])),
        );
      }
      return this.state.uiSelection;
    }
    // ---- Sessions ----
    if (method === "GET" && path === "/api/sessions") {
      // First pass: bucket every eng session by parent so non-eng rows
      // can carry a `pending_eng_session_id` for the resume badge.
      const engByParent = new Map<string, string>();
      for (const s of this.state.sessions) {
        if (
          s.working_mode === "prompt_engineering" &&
          s.working_mode_meta?.parent_session_id
        ) {
          engByParent.set(s.working_mode_meta.parent_session_id, s.id);
        }
      }
      // Second pass: filter eng sessions out of the sidebar and stamp
      // each remaining row with its pending_eng_session_id (if any).
      const offset = Number.parseInt(query.offset ?? "0", 10);
      // A caller that sends no `limit` gets the mock's default page size:
      // the whole corpus unless the test opted into the real endpoint's
      // `Query(50, ge=1, le=200)` via `setSessionPageLimit`. See that
      // method's comment, and backend/scripts/test_session_listing_page_scope.py
      // for the contract it mirrors.
      const defaultLimit = this.sessionPageLimit ?? this.state.sessions.length;
      const limit = Math.min(
        Number.parseInt(query.limit ?? String(defaultLimit), 10) || defaultLimit,
        this.sessionPageLimit === null ? Number.MAX_SAFE_INTEGER : 200,
      );
      const sessions = this.state.sessions
          .filter((s) => s.working_mode !== "prompt_engineering")
          .filter((s) => sessionMatchesListQuery(s, query))
          .map((s) => ({
            ...sessionSummary(s),
            pending_eng_session_id: engByParent.get(s.id) ?? null,
          }))
          .sort((a, b) => {
            const pinnedDelta = Number(Boolean(b.pinned)) - Number(Boolean(a.pinned));
            if (pinnedDelta) return pinnedDelta;
            return Date.parse(b.updated_at || "") - Date.parse(a.updated_at || "");
          });
      const page = sessions.slice(offset, offset + limit);
      return {
        sessions: page,
        offset,
        limit,
        total: sessions.length,
        has_more: offset + limit < sessions.length,
      };
    }
    if (method === "GET" && path === "/api/sessions/topbar-pinned") {
      const sessions = this.state.sessions
        .filter((s) => s.topbar_pinned)
        .map((s) => sessionSummary(s))
        .sort((a, b) =>
          Date.parse((b.topbar_pinned_at as string | undefined) || "") -
          Date.parse((a.topbar_pinned_at as string | undefined) || ""),
        );
      return { sessions };
    }
    if (method === "GET" && path === "/api/sessions/summaries") {
      const ids = splitFilter(query.ids);
      const missOnce = new Set(this.state.summaryMissOnceIds);
      const missing = new Set(this.state.summaryMissingIds);
      if (missOnce.size > 0) {
        this.state.summaryMissOnceIds = this.state.summaryMissOnceIds.filter((id) => !ids.has(id));
      }
      const sessions = this.state.sessions
        .filter((s) => ids.has(s.id) && !missOnce.has(s.id) && !missing.has(s.id))
        .map((s) => sessionSummary(s));
      return { sessions };
    }
    const olderMessagesMatch = path.match(
      /^\/api\/sessions\/([^/]+)\/messages$/,
    );
    if (method === "GET" && olderMessagesMatch) {
      const sessionId = decodeURIComponent(olderMessagesMatch[1]);
      const session = this.state.sessions.find((s) => s.id === sessionId);
      if (!session) return notFound();
      const beforeSeq = Number.parseInt(query.before_seq ?? "", 10);
      const older = (session.messages ?? []).filter(
        (message) => typeof message.seq === "number" && message.seq < beforeSeq,
      );
      const oldestLoadedSeq = older.length > 0
        ? Math.min(...older.map((message) => message.seq as number))
        : null;
      return {
        messages: older,
        has_older: false,
        oldest_loaded_seq: oldestLoadedSeq,
        total_messages: session.messages?.length ?? 0,
      };
    }
    const messageEventsMatch = path.match(
      /^\/api\/sessions\/([^/]+)\/messages\/([^/]+)\/events$/,
    );
    if (method === "GET" && messageEventsMatch) {
      const sessionId = decodeURIComponent(messageEventsMatch[1]);
      const messageId = decodeURIComponent(messageEventsMatch[2]);
      const session = this.state.sessions.find((s) => s.id === sessionId);
      return session ? this.findMessage(session, messageId) ?? notFound() : notFound();
    }
    // ---- File (used by FileEditor's poll) ----
    if (method === "GET" && path === "/api/file") {
      const p = query.path;
      const content = p && this.state.files[p];
      return { content: content ?? "", language: "markdown" };
    }
    if (method === "POST" && path === "/api/file") {
      const b = body as { path?: string; content?: string };
      if (!b.path) return notFound();
      this.state.files[b.path] = b.content ?? "";
      return { ok: true };
    }
    if (method === "POST" && path === "/api/file-editor") {
      const b = body as {
        file_path?: string;
        cwd?: string;
        model?: string;
        provider_id?: string;
        reasoning_effort?: string;
      };
      if (!b.file_path) return notFound();
      const existing = this.state.sessions.find(
        (s) =>
          s.working_mode === "file_editing" &&
          s.working_mode_meta?.file_paths?.[0] === b.file_path,
      );
      if (existing) return { session_id: existing.id };
      const id = `file-edit-${this.state.sessions.length + 1}`;
      const session: Session = {
        id,
        name: `Edit ${b.file_path.split("/").pop() || b.file_path}`,
        model: b.model || "claude-sonnet-4-6",
        provider_id: b.provider_id || this.state.default_provider_id || "codex",
        reasoning_effort: b.reasoning_effort || "",
        cwd: b.cwd || "",
        orchestration_mode: "native",
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        messages: [],
        working_mode: "file_editing",
        working_mode_meta: {
          persistent: true,
          project_cwd: b.cwd || "",
          file_paths: [b.file_path],
          original_contents: { [b.file_path]: this.state.files[b.file_path] ?? "" },
          file_discussions: [],
        },
      };
      this.state.sessions.unshift(session);
      return { session_id: id };
    }
    const fileDiscussionMatch = path.match(/^\/api\/file-editor\/([^/]+)\/discussions$/);
    if (method === "POST" && fileDiscussionMatch) {
      const sessionId = decodeURIComponent(fileDiscussionMatch[1]);
      const session = this.state.sessions.find((s) => s.id === sessionId);
      if (!session || session.working_mode !== "file_editing") return notFound();
      const b = body as { file_path?: string; line?: number; client_id?: string };
      const discussion: FileDiscussion = {
        id: `discussion-${(session.working_mode_meta?.file_discussions ?? []).length + 1}`,
        file_path: b.file_path ?? "",
        line: Number(b.line),
        title: "",
        collapsed: false,
        opened_by: "user",
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };
      session.working_mode_meta = {
        ...(session.working_mode_meta ?? {}),
        file_discussions: [
          ...(session.working_mode_meta?.file_discussions ?? []),
          discussion,
        ],
      };
      return { discussion };
    }
    if (method === "POST" && path === "/api/sessions") {
      const b = body as Partial<Session> & {
        client_session_id?: string;
        runtime_profile_id?: string;
      };
      const existing = b.client_session_id
        ? this.state.sessions.find((s) => s.id === b.client_session_id)
        : undefined;
      if (existing) return existing;
      // Runtime profile stamps provider (like the real backend); the raw
      // provider_id param remains the legacy/explicit-override path.
      const profile = typeof b.runtime_profile_id === "string" && b.runtime_profile_id
        ? this.runtimeProfilesSnapshot().runtime_profiles.find(
            (p) => p.id === b.runtime_profile_id,
          )
        : undefined;
      const providerId = profile?.provider_id
        ?? (typeof b.provider_id === "string" && b.provider_id
          ? b.provider_id
          : this.state.default_provider_id || this.state.providers[0]?.id || "");
      const s: Session = {
        id: b.client_session_id || `sess-${this.state.sessions.length + 1}`,
        name: b.name || "New Session",
        model: b.model || "claude-sonnet-4-6",
        provider_id: providerId,
        runtime_profile_id: profile?.id ?? null,
        reasoning_effort: b.reasoning_effort || "",
        permission: b.permission || {},
        harness_profile_id: b.harness_profile_id || "",
        harness_profile_revision: b.harness_profile_revision || "",
        cwd: b.cwd || "",
        orchestration_mode: b.orchestration_mode || "manager",
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        messages: [],
      };
      this.state.sessions.unshift(s);
      return s;
    }
    const sessionMatch = path.match(/^\/api\/sessions\/([^/]+)(\/.*)?$/);
    if (sessionMatch) {
      const id = decodeURIComponent(sessionMatch[1]);
      const sub = sessionMatch[2] ?? "";
      const session = this.state.sessions.find((s) => s.id === id);
      if (sub === "" && method === "GET") {
        // Backend returns the ROOT tree containing `id` (works for
        // either a root or an embedded fork id). Walk every root
        // looking for the id; when found, return that root.
        for (const r of this.state.sessions) {
          if (findNodeInTree(r, id)) return r;
        }
        return session ?? notFound();
      }
      if (sub === "/stats" && method === "GET") {
        if (!session) return notFound();
        return {
          token_usage_total: session.token_usage_total ?? null,
          token_usage_last: session.token_usage_last ?? null,
          context_window: session.context_window ?? null,
        };
      }
      if (sub === "/opened" && method === "POST") {
        if (!session) return notFound();
        const at = new Date().toISOString();
        session.last_opened_at = at;
        return { id, last_opened_at: at };
      }
      if (sub === "/stop" && method === "POST") {
        if (!session) return notFound();
        return { stopped: true };
      }
      if (sub === "" && method === "DELETE") {
        // Cascade-delete: drop any prompt-engineering session whose
        // parent is this id, and tear down its temp file. Mirrors the
        // real backend's cleanup loop in main.py.
        const cascadedEng: string[] = [];
        for (const s of this.state.sessions) {
          if (
            s.working_mode === "prompt_engineering" &&
            s.working_mode_meta?.parent_session_id === id
          ) {
            cascadedEng.push(s.id);
            if (s.working_mode_meta.temp_file_path) {
              delete this.state.files[s.working_mode_meta.temp_file_path];
            }
          }
        }
        this.state.sessions = this.state.sessions.filter(
          (s) => s.id !== id && !cascadedEng.includes(s.id),
        );
        return { ok: true };
      }
      if (sub === "/fork" && method === "POST") {
        if (!session) return notFound();
        const child: Session = {
          ...session,
          id: `${session.id}-fork-${this.state.sessions.length}`,
          name: (body as { name?: string })?.name || `${session.name} (fork)`,
          parent_session_id: session.id,
          fork_point_seq: (session.messages?.length ?? 1) - 1,
          fork_closed: false,
          forks: [],
          messages: [],
        };
        // Embed under the parent like the real backend does (schema v2).
        session.forks = [...(session.forks ?? []), child];
        return child;
      }
      if (sub === "/fork_and_send" && method === "POST") {
        if (!session) return notFound();
        const b = body as { prompt?: string };
        if (!b.prompt || !b.prompt.trim()) {
          return { error: "prompt is required" };
        }
        const child: Session = {
          ...session,
          id: `${session.id}-fork-${this.state.sessions.length}`,
          name: `${session.name} (fork)`,
          parent_session_id: session.id,
          fork_point_seq: (session.messages?.length ?? 1) - 1,
          fork_closed: false,
          forks: [],
          messages: [],
        };
        session.forks = [...(session.forks ?? []), child];
        return { child, fork_point_seq: child.fork_point_seq };
      }
      if (sub === "/close_fork" && method === "POST") {
        if (!session) return notFound();
        const node = findNodeInTree(session, id);
        if (node) node.fork_closed = true;
        // Walk every root looking for a fork by id (the close_fork
        // request comes in with the FORK id as the path param).
        for (const r of this.state.sessions) {
          const target = findNodeInTree(r, id);
          if (target) target.fork_closed = true;
        }
        return { id, fork_closed: true };
      }
      if (sub === "/reopen_fork" && method === "POST") {
        for (const r of this.state.sessions) {
          const target = findNodeInTree(r, id);
          if (target) target.fork_closed = false;
        }
        return { id, fork_closed: false };
      }
      if (sub === "/rename" && method === "PUT") {
        if (session) session.name = (body as { name: string }).name;
        return { ok: true };
      }
      if (sub === "/selectors" && method === "PATCH") {
        if (session) {
          const b = body as Partial<Session>;
          if (b.runtime_profile_id) {
            const profile = this.runtimeProfilesSnapshot().runtime_profiles.find(
              (p) => p.id === b.runtime_profile_id,
            );
            if (profile) {
              session.runtime_profile_id = profile.id;
              session.provider_id = profile.provider_id;
              session.runner = profile.runner as Session["runner"];
            }
          }
          if (b.provider_id) session.provider_id = b.provider_id;
          if (b.model) session.model = b.model;
          if (b.reasoning_effort !== undefined)
            session.reasoning_effort = b.reasoning_effort;
          if (b.permission !== undefined) session.permission = b.permission;
          if (b.harness_profile_id !== undefined)
            session.harness_profile_id = b.harness_profile_id;
          if (b.harness_profile_revision !== undefined)
            session.harness_profile_revision = b.harness_profile_revision;
          if (b.cwd) session.cwd = b.cwd;
          if (b.orchestration_mode)
            session.orchestration_mode = b.orchestration_mode;
        }
        return {
          id,
          updates: body,
        };
      }
      if (sub === "/right-panel" && method === "PATCH") {
        const target = this.state.sessions
          .map((root) => findNodeInTree(root, id))
          .find((node): node is Session => Boolean(node));
        if (!target) return notFound();
        const b = body as {
          open?: boolean;
          tab?: Session["right_panel_active_tab"];
          width?: number;
          mobile_height?: number;
          todos_dismissed?: boolean;
          auto_opened_by?: Session["right_panel_auto_opened_by"];
          sidebar_minimized?: boolean;
        };
        if (b.open !== undefined) target.right_panel_open = b.open;
        if (b.tab !== undefined) target.right_panel_active_tab = b.tab;
        if (b.width !== undefined) target.right_panel_width = b.width;
        if (b.mobile_height !== undefined) target.right_panel_mobile_height = b.mobile_height;
        if (b.todos_dismissed !== undefined) target.right_panel_todos_dismissed = b.todos_dismissed;
        if (b.auto_opened_by !== undefined) target.right_panel_auto_opened_by = b.auto_opened_by;
        if (b.sidebar_minimized !== undefined) target.sidebar_minimized = b.sidebar_minimized;
        return {
          right_panel_open: target.right_panel_open,
          right_panel_active_tab: target.right_panel_active_tab,
          right_panel_width: target.right_panel_width,
          right_panel_mobile_height: target.right_panel_mobile_height,
          right_panel_todos_dismissed: target.right_panel_todos_dismissed,
          right_panel_auto_opened_by: target.right_panel_auto_opened_by ?? [],
          sidebar_minimized: target.sidebar_minimized,
        };
      }
      if (sub === "/rewind" && method === "POST") return { ok: true };
      if (sub === "/tags" && method === "POST") {
        if (!session) return notFound();
        const tag = body as NonNullable<Session["inline_tags"]>[number];
        session.inline_tags = [...(session.inline_tags ?? []), tag];
        return { ok: true };
      }
      if (sub === "/tags" && method === "DELETE") {
        if (!session) return notFound();
        session.inline_tags = [];
        return { ok: true };
      }
      if (sub.startsWith("/tags/") && method === "DELETE") {
        if (!session) return notFound();
        const tagId = decodeURIComponent(sub.slice("/tags/".length));
        session.inline_tags = (session.inline_tags ?? []).filter((tag) => tag.id !== tagId);
        return { ok: true };
      }
      if (sub.startsWith("/tags/") && method === "PATCH") {
        if (!session) return notFound();
        const tagId = decodeURIComponent(sub.slice("/tags/".length));
        const updates = body as Partial<NonNullable<Session["inline_tags"]>[number]>;
        session.inline_tags = (session.inline_tags ?? []).map((tag) =>
          tag.id === tagId ? { ...tag, ...updates } : tag,
        );
        return { ok: true };
      }
      if (sub === "/schedules" && method === "GET") {
        return { schedules: [] };
      }
      if (sub === "/notes" && method === "POST") {
        if (!session) return notFound();
        const b = body as { text?: string };
        session.notes = [
          ...(session.notes ?? []),
          {
            id: `note-${(session.notes ?? []).length + 1}`,
            text: b.text ?? "",
            created_at: new Date().toISOString(),
          },
        ];
        return { notes: session.notes };
      }
      if (sub.startsWith("/notes/") && method === "DELETE") {
        if (!session) return notFound();
        const noteId = decodeURIComponent(sub.slice("/notes/".length));
        session.notes = (session.notes ?? []).filter((note) => note.id !== noteId);
        return { notes: session.notes };
      }
      if (sub.startsWith("/notes/") && method === "PATCH") {
        if (!session) return notFound();
        const noteId = decodeURIComponent(sub.slice("/notes/".length));
        const b = body as { text?: string };
        session.notes = (session.notes ?? []).map((note) =>
          note.id === noteId ? { ...note, text: b.text ?? "" } : note,
        );
        return { notes: session.notes };
      }
      // ---- Prompt-engineering ----
      if (sub === "/prompt-engineer" && method === "POST") {
        if (!session) return notFound();
        const b = body as { draft?: string; mode?: "fork" | "new" };
        const draft = b.draft ?? "";
        const mode = b.mode === "fork" ? "fork" : "new";
        // Idempotent: if a live eng session already exists for this
        // parent, return it without touching the temp file. Mirrors the
        // real backend's single-eng-per-parent invariant.
        const existing = this.state.sessions.find(
          (s) =>
            s.working_mode === "prompt_engineering" &&
            s.working_mode_meta?.parent_session_id === session.id,
        );
        if (existing) {
          const meta = existing.working_mode_meta;
          return {
            eng_session_id: existing.id,
            temp_file_path: meta?.temp_file_path ?? "",
            original_content: meta?.original_content ?? "",
            session: existing,
            resumed: true,
          };
        }
        const engId = `eng-${this.state.sessions.length + 1}`;
        const tempPath = `/tmp/prompt-eng/${engId}/prompt.md`;
        const eng = {
          id: engId,
          name: mode === "fork"
            ? `⚙ Engineer — ${session.name}`
            : "⚙ Engineer — fresh",
          model: session.model,
          cwd: session.cwd,
          orchestration_mode: session.orchestration_mode,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          messages: [],
          working_mode: "prompt_engineering",
          working_mode_meta: {
            parent_session_id: session.id,
            temp_file_path: tempPath,
            original_content: draft,
            mode,
          },
        } as unknown as Session;
        this.state.sessions.unshift(eng);
        this.state.files[tempPath] = draft;
        return {
          eng_session_id: engId,
          temp_file_path: tempPath,
          original_content: draft,
          session: eng,
          resumed: false,
        };
      }
      if (sub === "/prompt-engineer" && method === "GET") {
        // Resume-path lookup: live eng session whose parent is `id`.
        if (!session) return notFound();
        const eng = this.state.sessions.find(
          (s) =>
            s.working_mode === "prompt_engineering" &&
            s.working_mode_meta?.parent_session_id === id,
        );
        if (!eng) return notFound();
        const meta = eng.working_mode_meta;
        return {
          eng_session_id: eng.id,
          temp_file_path: meta?.temp_file_path ?? "",
          original_content: meta?.original_content ?? "",
          session: eng,
          resumed: true,
        };
      }
      if (sub === "/prompt-engineer" && method === "DELETE") {
        if (!session) return notFound();
        const meta = session.working_mode === "prompt_engineering"
          ? session.working_mode_meta
          : undefined;
        if (meta?.temp_file_path) {
          delete this.state.files[meta.temp_file_path];
        }
        this.state.sessions = this.state.sessions.filter((s) => s.id !== id);
        return { deleted: true };
      }
      if (sub === "/prompt-eng-comment" && method === "POST") {
        if (!session) return notFound();
        return { submitted: true };
      }
      if (sub === "/prompt-eng-result" && method === "GET") {
        if (!session) return notFound();
        const meta = session.working_mode === "prompt_engineering"
          ? session.working_mode_meta
          : undefined;
        const path = meta?.temp_file_path;
        return {
          content: (path && this.state.files[path]) ?? "",
          parent_session_id: meta?.parent_session_id ?? null,
          original_content: meta?.original_content ?? "",
        };
      }
      if (sub === "/draft" && method === "PATCH") {
        if (!session) return notFound();
        const b = body as {
          draft_input?: string;
          client_seq?: number;
          client_id?: string;
        };
        const stored = this.draftSeqs.get(id) ?? 0;
        if (typeof b.client_seq !== "number" || b.client_seq <= stored) {
          return {
            rejected: true,
            draft_input: session.draft_input ?? "",
            draft_input_seq: stored,
          };
        }
        session.draft_input = b.draft_input ?? "";
        this.draftSeqs.set(id, b.client_seq);
        return {
          draft_input: session.draft_input,
          draft_input_seq: b.client_seq,
        };
      }
    }
    // ---- Projects ----
    if (method === "GET" && path === "/api/projects") {
      return {
        epoch: "mock-projects",
        revision: 0,
        projects: this.state.projects.map((project) => ({
          ...project,
          running_count: project.running_count ?? 0,
          unread_session_count: project.unread_session_count ?? 0,
          waiting_for_user_count: project.waiting_for_user_count ?? 0,
          errored_count: project.errored_count ?? 0,
        })),
      };
    }
    if (method === "POST" && path === "/api/projects") {
      const p = body as { path: string };
      const proj: Project = {
        path: p.path,
        name: p.path.split("/").pop() || p.path,
        created_at: new Date().toISOString(),
        last_used: new Date().toISOString(),
      };
      this.state.projects.push(proj);
      return proj;
    }
    if (method === "POST" && path === "/api/projects/touch") {
      return { ok: true };
    }
    if (method === "DELETE" && path === "/api/projects") {
      this.state.projects = this.state.projects.filter(
        (p) => p.path !== query.path,
      );
      return { ok: true };
    }
    // ---- Workers ----
    if (method === "GET" && path === "/api/workers") {
      const pools = new Map<string, WorkerInfo[]>();
      for (const worker of this.state.workers) {
        for (const tag of worker.tags ?? []) {
          pools.set(tag, [...(pools.get(tag) ?? []), worker]);
        }
      }
      return {
        workers: this.state.workers.filter(() => true),
        pools: Array.from(pools.entries()).map(([tag, workers]) => ({
          tag,
          workers,
          queued_count: 0,
        })),
        teams: [],
      };
    }
    if (method === "POST" && path === "/api/workers") return { ok: true };
    if (method === "POST" && path === "/api/workers/from_session")
      return { ok: true };
    const workerMatch = path.match(/^\/api\/workers\/([^/]+)(\/.*)?$/);
    if (workerMatch) {
      if (method === "DELETE") return { ok: true };
      if (workerMatch[2] === "/reset_forks" && method === "POST")
        return { ok: true };
    }
    // ---- Approvals ----
    if (method === "GET" && path === "/api/pending_approvals") {
      return {
        approvals: this.state.approvals.filter(
          (a) => !query.cwd || a.cwd === query.cwd,
        ),
      };
    }
    const approveMatch = path.match(/^\/api\/pending_approvals\/([^/]+)\/(approve|deny)$/);
    if (approveMatch && method === "POST") {
      const id = decodeURIComponent(approveMatch[1]);
      const ap = this.state.approvals.find((a) => a.delegation_id === id);
      if (ap) ap.status = approveMatch[2] === "approve" ? "approved" : "denied";
      return { ok: true };
    }
    // ---- Credential consents ----
    if (method === "GET" && path === "/api/credentials/pending") {
      return {
        consents: this.state.credentials.filter(
          (c) =>
            c.status === "pending" &&
            (!query.app_session_id || c.app_session_id === query.app_session_id),
        ),
      };
    }
    const credMatch = path.match(
      /^\/api\/credentials\/([^/]+)\/(approve|deny|revoke)$/,
    );
    if (credMatch && method === "POST") {
      const id = decodeURIComponent(credMatch[1]);
      const c = this.state.credentials.find((x) => x.consent_id === id);
      if (c) {
        c.status =
          credMatch[2] === "approve"
            ? "approved"
            : credMatch[2] === "deny"
              ? "denied"
              : "revoked";
      }
      return { status: "ok" };
    }
    // ---- File / trace / config / models ----
    if (method === "POST" && path === "/api/file-before-edit") {
      return { before_content: "", after_content: "" };
    }
    if (method === "GET" && path === "/api/models") {
      const provider = this.state.providers.find(
        (record) => record.id === this.state.default_provider_id,
      );
      return provider
        ? modelCatalog(provider, this.state.models)
        : {
            provider_id: "",
            provider_generation: "",
            authoritative: false,
            status: "unavailable",
            models: [],
            models_current: false,
            retired: [],
            retired_models: [],
            last_refreshed_at: 0,
            reason: "no_provider",
            authority_fingerprint: "",
            last_known_good: null,
            runtime_profiles: [],
          };
    }
    if (method === "GET" && path === "/api/model-catalogs") {
      return {
        catalogs: this.state.providers
          .filter((provider) => (
            (provider.kind === "codex" || provider.kind === "fugu")
            && !provider.suspended
          ))
          .map((provider) => modelCatalog(provider, this.state.models)),
      };
    }
    const providerModelsMatch = path.match(/^\/api\/providers\/([^/]+)\/models$/);
    if (method === "GET" && providerModelsMatch) {
      const providerId = decodeURIComponent(providerModelsMatch[1]);
      const provider = this.state.providers.find((p) => p.id === providerId);
      if (!provider) return notFound();
      return modelCatalog(provider, this.state.models);
    }
    const providerModelsRefreshMatch = path.match(
      /^\/api\/providers\/([^/]+)\/models\/refresh$/,
    );
    if (method === "POST" && providerModelsRefreshMatch) {
      const providerId = decodeURIComponent(providerModelsRefreshMatch[1]);
      const provider = this.state.providers.find((record) => record.id === providerId);
      if (!provider) return notFound();
      return {
        accepted: true,
        provider_id: provider.id,
        provider_generation: `mock:${provider.id}`,
        catalog: modelCatalog(provider, this.state.models),
      };
    }
    if (method === "GET" && path === "/api/installation-profile") {
      return this.state.installationProfile;
    }
    if (method === "GET" && path === "/api/providers") {
      if (this.state.installationProfile.setup_required) return setupGate();
      return {
        default_provider_id: this.state.default_provider_id,
        providers: this.state.providers,
      };
    }
    if (method === "GET" && path === "/api/runtime-profiles") {
      if (this.state.installationProfile.setup_required) return setupGate();
      return this.runtimeProfilesSnapshot();
    }
    if (method === "GET" && path === "/api/config") return this.state.config;
    if (method === "POST" && path === "/api/config") return { ok: true };
    const traceMatch = path.match(/^\/api\/traces\/([^/]+)$/);
    if (traceMatch && method === "GET") {
      return this.state.traces[traceMatch[1]] ?? notFound();
    }

    // ---- Auth ----
    if (method === "GET" && path === "/api/auth/me") {
      return { username: "test-user" };
    }
    if (method === "GET" && path === "/api/auth/needs_setup") {
      return { needs_setup: false };
    }
    if (method === "POST" && path === "/api/auth/logout") return { ok: true };

    if (method === "POST" && /^\/api\/sessions\/[^/]+\/project-suggestion$/.test(path))
      return { suggestion: this.state.projectSuggestion };

    throw new Error(`MockBackend: unhandled ${method} ${path}`);
  }
}

function jsonResponse(data: unknown, status: number = 200): Response {
  if (data && typeof data === "object" && (data as { __notFound?: true }).__notFound) {
    return new Response(JSON.stringify({ error: "not found" }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    });
  }
  if (data && typeof data === "object" && (data as { __setupGate?: true }).__setupGate) {
    return new Response(JSON.stringify({ detail: "installation setup is required" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  }
  return new Response(JSON.stringify(data ?? null), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Id-based upsert: replaces an existing message in place, appends a new
 *  one. Shared by every `applyWsEvent` branch that projects a WS message
 *  onto the mock's `state.sessions`, so they all merge the same way. */
function mergeMessagesById(
  existing: ChatMessage[] | undefined,
  incoming: ChatMessage[],
): ChatMessage[] {
  const byId = new Map((existing ?? []).map((m, i) => [m.id, i]));
  const merged = [...(existing ?? [])];
  for (const m of incoming) {
    const idx = byId.get(m.id);
    if (idx !== undefined) merged[idx] = m;
    else {
      byId.set(m.id, merged.length);
      merged.push(m);
    }
  }
  return merged;
}

function findNodeInTree(root: Session, id: string): Session | null {
  if (root.id === id) return root;
  for (const f of root.forks ?? []) {
    const hit = findNodeInTree(f, id);
    if (hit) return hit;
  }
  return null;
}

function notFound(): { __notFound: true } {
  return { __notFound: true };
}

function setupGate(): { __setupGate: true } {
  return { __setupGate: true };
}
