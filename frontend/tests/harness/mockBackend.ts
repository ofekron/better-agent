import type {
  CredentialConsent,
  FileDiscussion,
  PendingApproval,
  Project,
  Provider,
  Session,
  Trace,
  UserInteractionRequest,
  WorkerInfo,
} from "../../src/types";
import type { ProjectSuggestion } from "../../src/components/ProjectSuggestionModal";

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
  /** Mocked file content keyed by absolute path. FileEditor polls
   * GET /api/file?path=... while the prompt-engineering overlay is up;
   * tests can pre-seed paths or watch them get fetched. */
  files: Record<string, string>;
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
      reasoning_effort_options: [],
      default_reasoning_effort: "",
      has_api_key: true,
      supports_fork: true,
      supports_manager_mode: true,
      supports_rewind: true,
      supports_steering: true,
      supports_native_subagents: false,
      supports_reasoning_effort: false,
    }],
    default_provider_id: "codex",
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
  private routeHolds: Map<string, Promise<void>[]> = new Map();
  private originalFetch: typeof fetch | undefined;

  seed(partial: Partial<BackendState>): void {
    this.state = { ...this.state, ...partial };
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
    this.routeHolds = new Map();
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

  holdNext(method: string, path: string): () => void {
    let release!: () => void;
    const promise = new Promise<void>((resolve) => {
      release = resolve;
    });
    const key = `${method.toUpperCase()} ${path}`;
    this.routeHolds.set(key, [...(this.routeHolds.get(key) ?? []), promise]);
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
    const path = u.pathname;
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

    const holdKey = `${method} ${path}`;
    const holds = this.routeHolds.get(holdKey);
    const hold = holds?.shift();
    if (holds && holds.length === 0) this.routeHolds.delete(holdKey);
    if (hold) await hold;

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
      return {
        entrypoints: [{
          extension_id: "ofek-dev.team-orchestration",
          name: "Team Orchestration",
          frontend_modules: [{
            slot: "team-sidebar",
            id: "workers-panel",
            label: "Workers",
            kind: "module",
            module_url: "/api/extensions/ofek-dev.team-orchestration/frontend/ui/team-sidebar.entry.js",
          }],
        }],
      };
    }
    if (method === "GET" && path === "/api/extensions/builtin-ids") {
      return { ids: {} };
    }
    if (method === "GET" && path === "/api/ui-selection") {
      return this.state.uiSelection;
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
        const meta = s as Session & {
          is_prompt_engineering?: boolean;
          prompt_eng_meta?: { parent_session_id?: string };
        };
        if (meta.is_prompt_engineering && meta.prompt_eng_meta?.parent_session_id) {
          engByParent.set(meta.prompt_eng_meta.parent_session_id, s.id);
        }
      }
      // Second pass: filter eng sessions out of the sidebar and stamp
      // each remaining row with its pending_eng_session_id (if any).
      const offset = Number.parseInt(query.offset ?? "0", 10);
      const limit = Number.parseInt(query.limit ?? String(this.state.sessions.length), 10);
      const sessions = this.state.sessions
          .filter(
            (s) => !(s as Session & { is_prompt_engineering?: boolean })
              .is_prompt_engineering,
          )
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
    if (
      method === "GET" &&
      (path === "/api/provider-config-sync/capability-picker" ||
        path === "/api/provider-config-sync/capability-picker")
    ) {
      return { sources: this.state.capabilitySources };
    }
    if (method === "POST" && path === "/api/sessions") {
      const b = body as Partial<Session> & { client_session_id?: string };
      const existing = b.client_session_id
        ? this.state.sessions.find((s) => s.id === b.client_session_id)
        : undefined;
      if (existing) return existing;
      const providerId = typeof b.provider_id === "string" && b.provider_id
        ? b.provider_id
        : this.state.default_provider_id || this.state.providers[0]?.id || "";
      const s: Session = {
        id: b.client_session_id || `sess-${this.state.sessions.length + 1}`,
        name: b.name || "New Session",
        model: b.model || "claude-sonnet-4-6",
        provider_id: providerId,
        reasoning_effort: b.reasoning_effort || "",
        permission: b.permission || {},
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
          const meta = s as Session & {
            is_prompt_engineering?: boolean;
            prompt_eng_meta?: {
              parent_session_id?: string;
              temp_file_path?: string;
            };
          };
          if (
            meta.is_prompt_engineering &&
            meta.prompt_eng_meta?.parent_session_id === id
          ) {
            cascadedEng.push(s.id);
            if (meta.prompt_eng_meta.temp_file_path) {
              delete this.state.files[meta.prompt_eng_meta.temp_file_path];
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
          if (b.provider_id) session.provider_id = b.provider_id;
          if (b.model) session.model = b.model;
          if (b.reasoning_effort !== undefined)
            session.reasoning_effort = b.reasoning_effort;
          if (b.permission !== undefined) session.permission = b.permission;
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
          (s) => {
            const meta = (s as Session & {
              is_prompt_engineering?: boolean;
              prompt_eng_meta?: { parent_session_id?: string };
            });
            return (
              meta.is_prompt_engineering &&
              meta.prompt_eng_meta?.parent_session_id === session.id
            );
          },
        );
        if (existing) {
          const meta = (existing as Session & {
            prompt_eng_meta?: {
              temp_file_path?: string;
              original_content?: string;
            };
          }).prompt_eng_meta;
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
          is_prompt_engineering: true,
          prompt_eng_meta: {
            parent_session_id: session.id,
            temp_file_path: tempPath,
            original_content: draft,
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
        const eng = this.state.sessions.find((s) => {
          const meta = (s as Session & {
            is_prompt_engineering?: boolean;
            prompt_eng_meta?: { parent_session_id?: string };
          });
          return (
            meta.is_prompt_engineering &&
            meta.prompt_eng_meta?.parent_session_id === id
          );
        });
        if (!eng) return notFound();
        const meta = (eng as Session & {
          prompt_eng_meta?: {
            temp_file_path?: string;
            original_content?: string;
          };
        }).prompt_eng_meta;
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
        const meta = (
          session as Session & {
            prompt_eng_meta?: { temp_file_path?: string };
          }
        ).prompt_eng_meta;
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
        const meta = (
          session as Session & {
            prompt_eng_meta?: {
              temp_file_path?: string;
              parent_session_id?: string;
              original_content?: string;
            };
          }
        ).prompt_eng_meta;
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
      return { projects: this.state.projects };
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
      return { models: this.state.models };
    }
    const providerModelsMatch = path.match(/^\/api\/providers\/([^/]+)\/models$/);
    if (method === "GET" && providerModelsMatch) {
      const providerId = decodeURIComponent(providerModelsMatch[1]);
      const provider = this.state.providers.find((p) => p.id === providerId);
      if (!provider) return { models: [] };
      const models = [
        provider.last_model,
        provider.default_model,
        ...(provider.custom_models ?? []),
        ...this.state.models.map((model) => model.id),
      ].filter((model): model is string => typeof model === "string" && model.length > 0);
      return { models: Array.from(new Set(models)) };
    }
    if (method === "GET" && path === "/api/providers") {
      return {
        default_provider_id: this.state.default_provider_id,
        providers: this.state.providers,
      };
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
  return new Response(JSON.stringify(data ?? null), {
    status,
    headers: { "Content-Type": "application/json" },
  });
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
