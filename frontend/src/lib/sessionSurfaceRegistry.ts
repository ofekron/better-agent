// Singleton v2 session/project/folder/tag surface store (ADR 0008,
// Package B frontend consumption).
//
// One `/ws/v2/surface` connection, subscribed to the non-cursor `sessions`
// feed (`{"feeds": ["sessions"]}` — adapter_api.py's `_FEED_SURFACES`; one
// feed name covers all 9 `SessionFrame` union members unfiltered). Same
// "process-wide, ref-counted-by-usage, never-closed" idiom `lib/
// runSummaryRegistry.ts` (the `runs` feed) and `hooks/useProviderSurfaceSocket.ts`
// (the `providers` feed) already use — a third instance of the ONE
// established mechanism, not a new one. Class-based (like
// runSummaryRegistry) since this is read from both hooks and plain
// functions (`useSession.ts`'s mutation gating doesn't want a React hook
// dependency).
//
// ADDITIVE to `lib/sessionRegistry.ts` (the legacy `/ws/chat`-sourced
// running/unread/error/marker/todo state), never a replacement for it:
// `SessionSummaryWire` has no `unread_count`/`pending_user_input_count`/
// marker color-tooltip/`current_todos`/`current_tasks`/cwd-routing fields
// — see that module's own per-subscription doc comments for the exhaustive
// gap list. This registry owns the NEW v2-only data instead: `project_ref`,
// `folder_ref`, `tag_refs`, and the rollup `state` — used today by
// `SessionList.tsx`'s folder/tag catalog UI (replacing the legacy
// `GET /api/session-organization` snapshot-refetch-on-`session_organization_changed`
// pattern with real incremental push, including cross-tab sync the legacy
// path never had) and by `useSession.ts`'s session-scoped mutations
// (archive/rename/mark_opened — submitted as `SessionIntent`s when this
// socket is open, REST otherwise; same "native-when-socket-open/legacy-
// fallback" gating `Chat.tsx`'s native send path uses, generalized here to
// the sibling plane exactly like `useProviderSurfaceSocket.ts` already does
// for providers).
//
// Deliberately NOT gated behind `surface/flag.ts`'s `ba.surface_native` —
// that flag governs whether Chat.tsx renders CHAT CONTENT via the native
// node surface (its own docstring: "native Contract-Node rendering
// layer"), not "should any v2 endpoint be used at all". `hooks/
// useProviderSurfaceSocket.ts`'s sibling `providers` feed connection
// established this same reasoning for a non-chat-content plane; every
// consumer here keeps its legacy REST/eventBus path active regardless
// (see `useSession.ts`'s dual-path mutation wrappers), so there is no
// user-visible regression if this connection is ever unreachable.

import { useSyncExternalStore } from "react";

import { SurfaceClient } from "../adapter/client";
import {
  type DistributiveOmit,
  type ProjectRef,
  type ProjectWire,
  type SessionFolderWire,
  type SessionIntentWire,
  type SessionsFrame,
  type SessionSummaryWire,
  type SessionTagWire,
  type TransportAckFrame,
} from "../adapter/wire";
import { createFeedSocketRegistry } from "./surfaceFeedSocket";
import { uuidv4 } from "./uuid";

/** Transport for the `sessions` feed, built on the shared ref-counted
 * factory (`surfaceFeedSocket.ts`) — `focus: "warm"` (the factory's own
 * default) since this connection isn't tied to any single session being
 * the user's current view, same rationale `runSummaryRegistry`'s `runs`
 * feed uses. */
const feedSocket = createFeedSocketRegistry<SessionsFrame>(["sessions"], (handlers, dispatch) => {
  handlers.onSessionsFrame = dispatch;
});

type Listener = () => void;

/** Key for the "no project filter" folder/tag catalog bucket — distinct
 * from any real `project_ref` (which is always a non-empty filesystem
 * path, `session_surface.py`'s `Project.project_ref` docstring), so it
 * never collides. */
const ALL_PROJECTS = "";

class SessionSurfaceRegistry {
  private readonly client = new SurfaceClient();
  private transportOpened = false;

  private sessionsById = new Map<string, SessionSummaryWire>();
  private projectsByRef = new Map<ProjectRef, ProjectWire>();
  private foldersByRef = new Map<string, SessionFolderWire>();
  private tagsByRef = new Map<string, SessionTagWire>();

  private sessionListeners = new Map<string, Set<Listener>>();
  private projectsListeners = new Set<Listener>();
  /** Keyed by `project_ref` (or `ALL_PROJECTS`) — a folder/tag catalog
   * read is always scoped that way (`fetchFolders`/`fetchTags`'s own
   * `project_ref?` param), so a listener only needs to know about
   * mutations inside its own scope. */
  private folderListeners = new Map<string, Set<Listener>>();
  private tagListeners = new Map<string, Set<Listener>>();
  /** Global (unscoped) — every subscriber recomputes its own project-scoped
   * facet via `getModelFacet`'s per-scope cache, same tolerance
   * `notifyAllSessions` already has for firing broader than one specific
   * consumer strictly needs. */
  private modelFacetListeners = new Set<Listener>();

  private sessionsHydrated = false;
  private sessionsHydrating: Promise<void> | null = null;
  private projectsHydrated = false;
  private projectsHydrating: Promise<void> | null = null;
  private hydratedFolderScopes = new Set<string>();
  private inflightFolderHydrate = new Map<string, Promise<void>>();
  private hydratedTagScopes = new Set<string>();
  private inflightTagHydrate = new Map<string, Promise<void>>();

  // Version counters + cached array snapshots — `useSyncExternalStore`
  // equality-checks `getSnapshot()`'s return by reference every render;
  // a fresh `[...map.values()]` allocation each call would infinite-loop
  // (same invariant `sessionRegistry.ts`'s `metaCache` documents). Bumped
  // in `notifyProjects`/`notifyFolderScope`/`notifyTagScope`.
  private projectsVersion = 0;
  private cachedProjects: { at: number; value: ProjectWire[] } | null = null;
  private folderVersions = new Map<string, number>();
  private cachedFolders = new Map<string, { at: number; value: SessionFolderWire[] }>();
  private tagVersions = new Map<string, number>();
  private cachedTags = new Map<string, { at: number; value: SessionTagWire[] }>();
  // B11 (parity audit): `modelFacet` — the distinct `selectors.model` set
  // used anywhere in a project, driving the session-list "Model" filter's
  // option universe (`SessionList.tsx`'s `modelOptions`). Legacy sourced
  // this from a one-shot `GET /api/session-organization` REST snapshot
  // with no live push; this store already fully hydrates every session
  // (bounded pagination, `hydrateSessions` above) and receives every
  // `session_summary_upsert`/`session_removed` live, so the SAME data this
  // needs is already resident — no new backend read model required, only
  // a derived accessor over the existing `sessionsById` map. Bumped
  // whenever a session's own `selectors.model`/`project_ref` changes (the
  // only fields that can move it into/out of a scope's distinct set).
  private modelFacetVersion = 0;
  private cachedModelFacet = new Map<string, { at: number; value: string[] }>();

  /** Opens the shared transport (once, lazily) via the factory's own
   * ref-counted `subscribeFrames` — this registry never calls the
   * returned unsubscribe, so the connection stays open forever once a
   * first caller opens it, same "permanent internal subscription" shape
   * `runSummaryRegistry.ts` uses for its own never-closing `runs` feed. */
  private ensureOpen(): void {
    if (this.transportOpened) return;
    this.transportOpened = true;
    feedSocket.subscribeFrames((frame) => this.applyFrame(frame));
    void this.hydrateSessions();
    void this.hydrateProjects();
  }

  // ── Live frame application ─────────────────────────────────────────

  private applyFrame(frame: SessionsFrame): void {
    for (const cb of this.refWatchers) cb(frame);
    switch (frame.type) {
      case "session_summary_upsert": {
        const previous = this.sessionsById.get(frame.summary.session_id);
        this.sessionsById.set(frame.summary.session_id, frame.summary);
        this.notifySession(frame.summary.session_id);
        for (const cb of this.summaryWatchers) cb(frame.summary);
        if (
          previous?.selectors.model !== frame.summary.selectors.model
          || previous?.project_ref !== frame.summary.project_ref
        ) {
          this.notifyModelFacet();
        }
        return;
      }
      case "session_removed": {
        const previous = this.sessionsById.get(frame.session_id);
        if (this.sessionsById.delete(frame.session_id)) {
          this.notifySession(frame.session_id);
          if (previous?.selectors.model) this.notifyModelFacet();
        }
        return;
      }
      case "project_upsert": {
        this.projectsByRef.set(frame.project.project_ref, frame.project);
        this.notifyProjects();
        return;
      }
      case "project_removed": {
        if (this.projectsByRef.delete(frame.project_ref)) this.notifyProjects();
        return;
      }
      case "folder_upsert": {
        this.foldersByRef.set(frame.folder.folder_ref, frame.folder);
        this.notifyFolderScope(frame.folder.project_ref);
        this.notifyFolderScope(ALL_PROJECTS);
        return;
      }
      case "folder_removed": {
        const prev = this.foldersByRef.get(frame.folder_ref);
        if (this.foldersByRef.delete(frame.folder_ref)) {
          if (prev) this.notifyFolderScope(prev.project_ref);
          this.notifyFolderScope(ALL_PROJECTS);
        }
        return;
      }
      case "tag_upsert": {
        this.tagsByRef.set(frame.tag.tag_ref, frame.tag);
        this.notifyTagScope(frame.tag.project_ref ?? ALL_PROJECTS);
        this.notifyTagScope(ALL_PROJECTS);
        return;
      }
      case "tag_removed": {
        const prev = this.tagsByRef.get(frame.tag_ref);
        if (this.tagsByRef.delete(frame.tag_ref)) {
          if (prev) this.notifyTagScope(prev.project_ref ?? ALL_PROJECTS);
          this.notifyTagScope(ALL_PROJECTS);
        }
        return;
      }
      case "session_tree_changed":
        // No `GET .../tree` REST route exists yet (see wire.ts's
        // `SessionTreeChangedFrame` doc) — nothing actionable to do with
        // this frame from the frontend today. Fork-tree UI stays 100%
        // legacy (`session_forked` on `/ws/chat`).
        return;
    }
  }

  private notifySession(sessionId: string): void {
    for (const cb of this.sessionListeners.get(sessionId) ?? []) cb();
  }

  private notifyProjects(): void {
    this.projectsVersion++;
    for (const cb of this.projectsListeners) cb();
  }

  private notifyFolderScope(scope: string): void {
    this.folderVersions.set(scope, (this.folderVersions.get(scope) ?? 0) + 1);
    for (const cb of this.folderListeners.get(scope) ?? []) cb();
  }

  private notifyTagScope(scope: string): void {
    this.tagVersions.set(scope, (this.tagVersions.get(scope) ?? 0) + 1);
    for (const cb of this.tagListeners.get(scope) ?? []) cb();
  }

  private notifyModelFacet(): void {
    this.modelFacetVersion++;
    for (const cb of this.modelFacetListeners) cb();
  }

  // ── Cold hydration ───────────────────────────────────────────────

  private hydrateSessions(): Promise<void> {
    if (this.sessionsHydrated) return Promise.resolve();
    if (this.sessionsHydrating) return this.sessionsHydrating;
    this.sessionsHydrating = (async () => {
      // Bounded pagination follow — 50/page (adapter_api.py's `_PAGE_SIZE`),
      // capped so a runaway `next_cursor` loop (server bug) can't hang this
      // forever. 400 pages = 20,000 sessions, comfortably above any real
      // installation.
      let cursor: string | undefined;
      for (let page = 0; page < 400; page++) {
        let envelope;
        try {
          envelope = await this.client.fetchSessions({ cursor });
        } catch {
          return; // Left un-hydrated — no retry is wired for the full
          // list (unlike `hydrateFolders`/`hydrateTags`'s per-scope
          // retry-on-next-subscribe); a future reconnect/remount picks it
          // up. Live upserts still apply meanwhile.
        }
        if (envelope.kind !== "ok") return;
        for (const summary of envelope.sessions) {
          this.sessionsById.set(summary.session_id, summary);
        }
        if (!envelope.next_cursor) break;
        cursor = envelope.next_cursor;
      }
      this.sessionsHydrated = true;
      this.notifyAllSessions();
      this.notifyModelFacet();
    })().finally(() => {
      this.sessionsHydrating = null;
    });
    return this.sessionsHydrating;
  }

  private notifyAllSessions(): void {
    for (const listeners of this.sessionListeners.values()) {
      for (const cb of listeners) cb();
    }
  }

  private hydrateProjects(): Promise<void> {
    if (this.projectsHydrated) return Promise.resolve();
    if (this.projectsHydrating) return this.projectsHydrating;
    this.projectsHydrating = this.client
      .fetchProjectsV2()
      .then((envelope) => {
        if (envelope.kind !== "ok") return;
        for (const project of envelope.value) {
          this.projectsByRef.set(project.project_ref, project);
        }
        this.projectsHydrated = true;
        this.notifyProjects();
      })
      .catch(() => {
        // Left un-hydrated; live `project_upsert` still applies.
      })
      .finally(() => {
        this.projectsHydrating = null;
      });
    return this.projectsHydrating;
  }

  private hydrateFolders(scope: string): Promise<void> {
    if (this.hydratedFolderScopes.has(scope)) return Promise.resolve();
    const inflight = this.inflightFolderHydrate.get(scope);
    if (inflight) return inflight;
    const projectRef = scope === ALL_PROJECTS ? undefined : scope;
    const promise = this.client
      .fetchFolders(projectRef)
      .then((envelope) => {
        this.hydratedFolderScopes.add(scope);
        if (envelope.kind !== "ok") return;
        for (const folder of envelope.value) this.foldersByRef.set(folder.folder_ref, folder);
        this.notifyFolderScope(scope);
      })
      .catch(() => {
        // Left un-hydrated: the next `subscribeFolders` call retries.
      })
      .finally(() => {
        this.inflightFolderHydrate.delete(scope);
      });
    this.inflightFolderHydrate.set(scope, promise);
    return promise;
  }

  private hydrateTags(scope: string): Promise<void> {
    if (this.hydratedTagScopes.has(scope)) return Promise.resolve();
    const inflight = this.inflightTagHydrate.get(scope);
    if (inflight) return inflight;
    const projectRef = scope === ALL_PROJECTS ? undefined : scope;
    const promise = this.client
      .fetchTags(projectRef)
      .then((envelope) => {
        this.hydratedTagScopes.add(scope);
        if (envelope.kind !== "ok") return;
        for (const tag of envelope.value) this.tagsByRef.set(tag.tag_ref, tag);
        this.notifyTagScope(scope);
      })
      .catch(() => {
        // Left un-hydrated: the next `subscribeTags` call retries.
      })
      .finally(() => {
        this.inflightTagHydrate.delete(scope);
      });
    this.inflightTagHydrate.set(scope, promise);
    return promise;
  }

  // ── Public subscribe API ─────────────────────────────────────────

  private summaryWatchers = new Set<(summary: SessionSummaryWire) => void>();

  /** Every `SessionSummaryUpsert` this connection observes, unfiltered by
   * session id — for a caller that folds specific fields (e.g. `has_error`/
   * `attention_marker_details`) into a DIFFERENT registry's own per-session
   * entries rather than reading `SessionSummaryWire` objects directly (see
   * `lib/sessionRegistry.ts`'s `onSummaryV2`). Same "global watcher" shape
   * as `refWatchers` above, generalized to every upsert instead of just
   * intent-correlated ones. */
  subscribeAllSummaries(cb: (summary: SessionSummaryWire) => void): () => void {
    this.ensureOpen();
    this.summaryWatchers.add(cb);
    return () => {
      this.summaryWatchers.delete(cb);
    };
  }

  subscribeSession(sessionId: string, cb: Listener): () => void {
    this.ensureOpen();
    let set = this.sessionListeners.get(sessionId);
    if (!set) {
      set = new Set();
      this.sessionListeners.set(sessionId, set);
    }
    set.add(cb);
    return () => {
      set!.delete(cb);
      if (set!.size === 0) this.sessionListeners.delete(sessionId);
    };
  }

  subscribeProjects(cb: Listener): () => void {
    this.ensureOpen();
    this.projectsListeners.add(cb);
    return () => this.projectsListeners.delete(cb);
  }

  /** `projectRef` omitted (or `undefined`) subscribes to the global
   * (unfiltered) catalog — mirrors `fetchFolders(projectRef?)`'s own
   * optional param. */
  subscribeFolders(projectRef: string | undefined, cb: Listener): () => void {
    this.ensureOpen();
    const scope = projectRef ?? ALL_PROJECTS;
    let set = this.folderListeners.get(scope);
    if (!set) {
      set = new Set();
      this.folderListeners.set(scope, set);
    }
    set.add(cb);
    void this.hydrateFolders(scope);
    return () => {
      set!.delete(cb);
      if (set!.size === 0) this.folderListeners.delete(scope);
    };
  }

  subscribeTags(projectRef: string | undefined, cb: Listener): () => void {
    this.ensureOpen();
    const scope = projectRef ?? ALL_PROJECTS;
    let set = this.tagListeners.get(scope);
    if (!set) {
      set = new Set();
      this.tagListeners.set(scope, set);
    }
    set.add(cb);
    void this.hydrateTags(scope);
    return () => {
      set!.delete(cb);
      if (set!.size === 0) this.tagListeners.delete(scope);
    };
  }

  /** B11: live-pushed model facet (see `modelFacetVersion`'s doc comment
   * above) — global list, no per-scope hydration call needed (piggybacks
   * on `hydrateSessions`, already triggered by `ensureOpen`/any other
   * subscriber of this registry). */
  subscribeModelFacet(cb: Listener): () => void {
    this.ensureOpen();
    this.modelFacetListeners.add(cb);
    void this.hydrateSessions();
    return () => {
      this.modelFacetListeners.delete(cb);
    };
  }

  /** Subscribe to this connection's open/close transitions (e.g. to gate
   * "submit via intent vs. fall back to REST" eligibility). Fires once
   * synchronously with the CURRENT state on subscribe, same contract as
   * `useProviderSurfaceSocket.ts`'s `subscribeProviderSocketConnection`. */
  subscribeConnection(cb: (open: boolean) => void): () => void {
    this.ensureOpen();
    return feedSocket.subscribeConnection(cb);
  }

  isSocketOpen(): boolean {
    return feedSocket.isOpen();
  }

  // ── Public readers ───────────────────────────────────────────────

  getSession(sessionId: string): SessionSummaryWire | undefined {
    return this.sessionsById.get(sessionId);
  }

  /** Stable-reference cache — see the version-counter fields' doc comment.
   * Recomputes only when `projectsVersion` moved since the last call. */
  getProjects(): ProjectWire[] {
    if (this.cachedProjects && this.cachedProjects.at === this.projectsVersion) {
      return this.cachedProjects.value;
    }
    const value = [...this.projectsByRef.values()];
    this.cachedProjects = { at: this.projectsVersion, value };
    return value;
  }

  getFolders(projectRef: string | undefined): SessionFolderWire[] {
    const scope = projectRef ?? ALL_PROJECTS;
    const version = this.folderVersions.get(scope) ?? 0;
    const cached = this.cachedFolders.get(scope);
    if (cached && cached.at === version) return cached.value;
    const all = [...this.foldersByRef.values()];
    const filtered = scope === ALL_PROJECTS ? all : all.filter((f) => f.project_ref === scope);
    const value = filtered.sort((a, b) => a.order - b.order);
    this.cachedFolders.set(scope, { at: version, value });
    return value;
  }

  getTags(projectRef: string | undefined): SessionTagWire[] {
    const scope = projectRef ?? ALL_PROJECTS;
    const version = this.tagVersions.get(scope) ?? 0;
    const cached = this.cachedTags.get(scope);
    if (cached && cached.at === version) return cached.value;
    const all = [...this.tagsByRef.values()];
    // Project-scoped catalogs include global (`project_ref: null`) tags
    // too — same union `store_access.list_tags`'s `snapshot(project_id)`
    // already applies server-side, mirrored here for a cache hit that
    // skips a round trip once hydrated.
    const value =
      scope === ALL_PROJECTS
        ? all
        : all.filter((t) => t.project_ref === scope || t.project_ref === null);
    this.cachedTags.set(scope, { at: version, value });
    return value;
  }

  /** Distinct `selectors.model` across every hydrated+live-patched session
   * in `projectRef` (or every session when omitted) — see
   * `modelFacetVersion`'s doc comment above for why this needs no REST
   * call of its own. */
  getModelFacet(projectRef: string | undefined): string[] {
    const scope = projectRef ?? ALL_PROJECTS;
    const cached = this.cachedModelFacet.get(scope);
    if (cached && cached.at === this.modelFacetVersion) return cached.value;
    const models = new Set<string>();
    for (const summary of this.sessionsById.values()) {
      if (scope !== ALL_PROJECTS && summary.project_ref !== scope) continue;
      const model = summary.selectors.model;
      if (model) models.add(model);
    }
    const value = [...models].sort((a, b) => a.localeCompare(b));
    this.cachedModelFacet.set(scope, { at: this.modelFacetVersion, value });
    return value;
  }

  // ── Command plane ────────────────────────────────────────────────
  //
  // Late-rejection handling (a mutation's synchronous `intent_accepted`
  // still needing a bounded wait for a late async `intent_rejected` echo
  // — `SessionSurfaceAdapter.submit()` always admits synchronously and
  // only discovers a business-logic failure, e.g. `create_project` with
  // an empty `path`, once its fire-and-forget task actually runs) is the
  // shared factory's own job now (`feedSocket.submitIntent`) — this
  // registry no longer duplicates it.

  /** Bounded window `waitForRefUpsert`'s default `timeoutMs` uses —
   * matches the factory's own internal late-rejection window so a
   * ref-correlation wait and the ack's late-rejection wait race on
   * comparable timescales (see `submitIntentAwaitingRef`). */
  private static readonly REF_CORRELATION_WINDOW_MS = 1500;

  /** Submits a `SessionIntent` (ADR 0008) over the shared connection.
   * Returns `null` synchronously — exactly `SurfaceSocket.submit`'s own
   * not-open contract — when the connection isn't OPEN; the caller falls
   * back to REST (`useSession.ts`'s dual-path mutation wrappers). `cv`/
   * `intent_id` are stamped by the factory so every call site only
   * supplies the intent-specific fields. The resolved promise reflects
   * the FINAL outcome, not just admission (the factory's own late-
   * rejection wait). */
  submitIntent(
    intent: DistributiveOmit<SessionIntentWire, "cv" | "intent_id">,
  ): Promise<TransportAckFrame> | null {
    this.ensureOpen();
    return feedSocket.submitIntent(intent as { kind: string } & Record<string, unknown>);
  }

  // ── Event-driven ref correlation ─────────────────────────────────
  //
  // A create-shaped intent (`assign_project`'s continuation-style move,
  // `create_folder`/`create_tag`) has no synchronous RPC result — the
  // server-generated ref it produces arrives as an ordinary live frame
  // (`SessionSummaryUpsert`/`FolderUpsert`/`TagUpsert`) carrying the
  // creating intent's `intent_id` (`backend/adapters/session_adapter.py`'s
  // `_run_command` stamps it — see `CommandResult.ref`'s docstring). A
  // caller that needs the ref (e.g. to navigate, or to immediately assign
  // it) waits for that ONE frame instead of a bespoke response body — no
  // new RPC shape, just observing the existing live plane.

  private refWatchers = new Set<(frame: SessionsFrame) => void>();

  /** Resolves with the first `SessionsFrame` matching `matches` whose own
   * `intent_id` field equals `intentId`, or `undefined` after
   * `timeoutMs` (the mutation itself already succeeded per the caller's
   * already-resolved `submitIntent` promise — a ref-correlation timeout
   * means "accepted but this connection didn't observe the resulting
   * frame in time", never a rejection). */
  waitForRefUpsert<F extends SessionsFrame & { intent_id: string | null }>(
    intentId: string,
    matches: (frame: SessionsFrame) => frame is F,
    timeoutMs = SessionSurfaceRegistry.REF_CORRELATION_WINDOW_MS,
  ): Promise<F | undefined> {
    this.ensureOpen();
    return new Promise((resolve) => {
      let settled = false;
      const unsubscribe = (): void => {
        this.refWatchers.delete(watcher);
      };
      const watcher = (frame: SessionsFrame): void => {
        if (settled || !matches(frame) || frame.intent_id !== intentId) return;
        settled = true;
        clearTimeout(timer);
        unsubscribe();
        resolve(frame);
      };
      this.refWatchers.add(watcher);
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        unsubscribe();
        resolve(undefined);
      }, timeoutMs);
    });
  }

  /** Submits a create-shaped `SessionIntent` and resolves once its
   * server-generated ref is known — accept/reject via `submitIntent`,
   * then the ref via `waitForRefUpsert`. `null` when the connection isn't
   * open (caller falls back to REST, same as `submitIntent` alone);
   * `{ok: false}` on a real rejection; `{ok: true, frame}` on success,
   * `frame` itself `undefined` if the ref-correlation window elapsed
   * before the live plane delivered the matching upsert. */
  async submitIntentAwaitingRef<F extends SessionsFrame & { intent_id: string | null }>(
    intent: DistributiveOmit<SessionIntentWire, "cv" | "intent_id">,
    matches: (frame: SessionsFrame) => frame is F,
  ): Promise<{ ok: true; frame: F | undefined } | { ok: false; code: string; message: string } | null> {
    this.ensureOpen();
    if (!feedSocket.isOpen()) return null;
    // Generated up front (rather than left to the factory) so the
    // ref-watcher can be armed with the SAME id the submitted intent ends
    // up carrying — the factory's `submitIntent` uses a caller-supplied
    // `intent_id` verbatim instead of minting its own when one is present.
    const intentId = uuidv4();
    // Race the ref-watcher against the ack from the moment of submission —
    // a fast backend can deliver the upsert before the factory's own
    // late-rejection window resolves.
    const refPromise = this.waitForRefUpsert(intentId, matches);
    const submitted = feedSocket.submitIntent({
      ...intent,
      intent_id: intentId,
    } as { kind: string; intent_id: string } & Record<string, unknown>);
    if (!submitted) return null;
    const ack = await submitted;
    if (ack.type === "intent_rejected") {
      return { ok: false, code: ack.code, message: ack.message };
    }
    return { ok: true, frame: await refPromise };
  }
}

export const sessionSurfaceRegistry = new SessionSurfaceRegistry();

// ── React hooks ───────────────────────────────────────────────────────
// `useSyncExternalStore`-based, same shape as `sessionRegistry.ts`'s
// `useSessionMeta`/`useProjectAggregate` — subscribe wires the registry's
// listener (which also lazily opens the shared connection + kicks off
// hydration), snapshot/serverSnapshot both read the registry's cached,
// stable-reference getters.

export function useSessionSummaryV2(
  sessionId: string | null | undefined,
): SessionSummaryWire | undefined {
  return useSyncExternalStore(
    (cb) => {
      if (!sessionId) return () => {};
      return sessionSurfaceRegistry.subscribeSession(sessionId, cb);
    },
    () => (sessionId ? sessionSurfaceRegistry.getSession(sessionId) : undefined),
    () => (sessionId ? sessionSurfaceRegistry.getSession(sessionId) : undefined),
  );
}

const EMPTY_PROJECTS: ProjectWire[] = [];
const EMPTY_FOLDERS: SessionFolderWire[] = [];
const EMPTY_TAGS: SessionTagWire[] = [];

export function useProjectsV2(): ProjectWire[] {
  return useSyncExternalStore(
    (cb) => sessionSurfaceRegistry.subscribeProjects(cb),
    () => sessionSurfaceRegistry.getProjects(),
    () => EMPTY_PROJECTS,
  );
}

/** `projectRef` omitted subscribes to the global (unfiltered) folder
 * catalog. */
export function useFoldersV2(projectRef: string | undefined): SessionFolderWire[] {
  return useSyncExternalStore(
    (cb) => sessionSurfaceRegistry.subscribeFolders(projectRef, cb),
    () => sessionSurfaceRegistry.getFolders(projectRef),
    () => EMPTY_FOLDERS,
  );
}

export function useTagsV2(projectRef: string | undefined): SessionTagWire[] {
  return useSyncExternalStore(
    (cb) => sessionSurfaceRegistry.subscribeTags(projectRef, cb),
    () => sessionSurfaceRegistry.getTags(projectRef),
    () => EMPTY_TAGS,
  );
}

const EMPTY_MODEL_FACET: string[] = [];

/** B11 (parity audit): live replacement for the legacy one-shot
 * `GET /api/session-organization` `models` facet — `projectRef` omitted
 * subscribes to the global (unfiltered) facet, matching `useFoldersV2`/
 * `useTagsV2`'s own contract. */
export function useModelFacetV2(projectRef: string | undefined): string[] {
  return useSyncExternalStore(
    (cb) => sessionSurfaceRegistry.subscribeModelFacet(cb),
    () => sessionSurfaceRegistry.getModelFacet(projectRef),
    () => EMPTY_MODEL_FACET,
  );
}

/** Mirrors `useProviderSurfaceSocket.ts`'s `subscribeProviderSocketConnection`
 * contract — fires synchronously with the current state on subscribe. */
export function useSessionSocketOpenV2(): boolean {
  return useSyncExternalStore(
    (cb) => sessionSurfaceRegistry.subscribeConnection(() => cb()),
    () => sessionSurfaceRegistry.isSocketOpen(),
    () => false,
  );
}
