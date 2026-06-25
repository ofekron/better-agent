import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionList } from "../src/components/SessionList";
import { MobileActionSheetProvider } from "../src/components/MobileActionSheet";
import type { Provider, Session } from "../src/types";
import { makeSession, makeWorker } from "./fixtures";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const providers: Provider[] = [
  {
    id: "codex",
    name: "Codex",
    kind: "codex",
    mode: "subscription",
    base_url: "",
    config_dir: "",
    custom_models: [],
    default_model: "gpt-5-codex",
    reasoning_effort_options: [],
    default_reasoning_effort: "",
    has_api_key: false,
    supports_fork: true,
    supports_manager_mode: false,
    supports_rewind: true,
    supports_steering: true,
    supports_native_subagents: true,
    supports_reasoning_effort: true,
    capability_overrides: {},
  },
  {
    id: "claude",
    name: "Claude",
    kind: "claude",
    mode: "subscription",
    base_url: "",
    config_dir: "",
    custom_models: [],
    default_model: "claude-sonnet-4-6",
    reasoning_effort_options: [],
    default_reasoning_effort: "",
    has_api_key: false,
    supports_fork: true,
    supports_manager_mode: true,
    supports_rewind: true,
    supports_steering: false,
    supports_native_subagents: false,
    supports_reasoning_effort: false,
    capability_overrides: {},
  },
];

function renderList(
  sessions: Session[],
  props: Partial<ComponentProps<typeof SessionList>> = {},
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => new Promise<Response>(() => {})),
  );
  return render(
    <SessionList
      sessions={sessions}
      providers={providers}
      onSelect={() => {}}
      onDelete={() => {}}
      onRename={() => {}}
      onPin={() => {}}
      onUnpinOthers={() => {}}
      onArchive={() => {}}
      onWorkerEligible={() => {}}
      onAgentRenameAllowed={() => {}}
      onDetails={() => {}}
      {...props}
    />,
  );
}

function visibleSessionNames(): string[] {
  return within(screen.getByTestId("session-list")).getAllByTestId("session-item")
    .filter((item) => item.closest(".session-list-items"))
    .map((item) => item.querySelector(".session-item-name")?.textContent?.trim() ?? "");
}

function rowBySessionId(id: string): HTMLElement {
  const row = screen.getAllByTestId("session-item").find(
    (item) => item.getAttribute("data-session-id") === id,
  );
  if (!row) throw new Error(`Session row not found: ${id}`);
  return row;
}

function longPressSession(id: string) {
  const row = rowBySessionId(id);
  fireEvent.pointerDown(row, { button: 0 });
  act(() => {
    vi.advanceTimersByTime(500);
  });
  fireEvent.pointerUp(row);
}

function renderListWithMobileSheet(
  sessions: Session[],
  props: Partial<ComponentProps<typeof SessionList>> = {},
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => new Promise<Response>(() => {})),
  );
  return render(
    <MobileActionSheetProvider>
      <SessionList
        sessions={sessions}
        providers={providers}
        onSelect={() => {}}
        onDelete={() => {}}
        onRename={() => {}}
        onPin={() => {}}
        onUnpinOthers={() => {}}
        onArchive={() => {}}
        onWorkerEligible={() => {}}
        onAgentRenameAllowed={() => {}}
        onDetails={() => {}}
        {...props}
      />
    </MobileActionSheetProvider>,
  );
}

describe("SessionList advanced filters", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("counts todo and task progress with completed duplicates winning", () => {
    renderList([
      makeSession({
        id: "todo-task-session",
        name: "Todo task session",
        current_todos: [
          { content: "Shared item", status: "pending" },
          { content: "Todo only", status: "in_progress" },
        ],
        current_tasks: [
          { content: "Shared item", status: "completed" },
          { content: "Task only", status: "pending" },
        ],
      }),
    ]);

    const badge = within(rowBySessionId("todo-task-session")).getByTestId("session-todo-badge");
    expect(badge.textContent).toBe("1/3");
    expect(badge.getAttribute("data-todo-state")).toBe("progress");
  });

  it("unpins a specific pinned session from the row button", () => {
    const onPin = vi.fn();
    renderList(
      [makeSession({ id: "pinned", name: "Pinned", pinned: true })],
      { onPin },
    );

    fireEvent.click(screen.getByRole("button", { name: "session.unpinTitle" }));

    expect(onPin).toHaveBeenCalledWith("pinned", false);
  });

  it("embeds bound workers and policy controls inside team session rows", () => {
    const onWorkerCreationPolicyChange = vi.fn();
    renderList(
      [
        makeSession({
          id: "team-session",
          name: "Team session",
          orchestration_mode: "team",
          worker_creation_policy: "ask",
        }),
      ],
      {
        teamWorkersBySession: {
          "team-session": [
            makeWorker({
              agent_session_id: "worker-1",
              name: "Reviewer",
              orchestration_mode: "native",
              team_role: "reviewer",
            }),
          ],
        },
        onWorkerCreationPolicyChange,
      },
    );

    const row = rowBySessionId("team-session");
    expect(row.textContent).toContain("1 session.workers");
    fireEvent.click(within(row).getByRole("button", { name: "session.expandTeamWorkers" }));

    expect(row.textContent).toContain("Reviewer");
    expect(row.textContent).toContain("reviewer");
    fireEvent.change(within(row).getByRole("combobox"), { target: { value: "deny" } });
    expect(onWorkerCreationPolicyChange).toHaveBeenCalledWith("team-session", "deny");
  });

  it("splits advanced search filters into global and project sections", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/session-organization")) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                schema_version: 1,
                folders: [
                  {
                    id: "folder-client",
                    project_id: "/tmp/project",
                    parent_folder_id: null,
                    name: "Client",
                    order: 0,
                    created_at: "2026-01-01T00:00:00Z",
                    updated_at: "2026-01-01T00:00:00Z",
                  },
                ],
                tags: [
                  {
                    id: "tag-important",
                    project_id: "/tmp/project",
                    name: "Important",
                    color: null,
                    created_at: "2026-01-01T00:00:00Z",
                    updated_at: "2026-01-01T00:00:00Z",
                  },
                ],
                assignments: {},
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        return Promise.resolve(
          new Response(JSON.stringify({ results: [] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );

    render(
      <SessionList
        sessions={[makeSession({ id: "alpha", name: "Alpha", cwd: "/tmp/project" })]}
        providers={providers}
        onSelect={() => {}}
        onDelete={() => {}}
        onRename={() => {}}
        onPin={() => {}}
        onUnpinOthers={() => {}}
        onArchive={() => {}}
        onWorkerEligible={() => {}}
        onAgentRenameAllowed={() => {}}
        onDetails={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "session.advancedFilterPanel" }));

    const globalSection = await screen.findByText("session.globalFilters");
    const projectSection = await screen.findByText("session.projectFilters");
    const global = globalSection.closest(".session-filter-section");
    const project = projectSection.closest(".session-filter-section");

    expect(global).toBeTruthy();
    expect(project).toBeTruthy();
    expect(within(global as HTMLElement).getByText("session.searchIn")).toBeTruthy();
    expect(within(global as HTMLElement).getByText("session.providerFilter")).toBeTruthy();
    expect(within(project as HTMLElement).getByText("session.folder")).toBeTruthy();
    expect(within(project as HTMLElement).getByText("session.tags")).toBeTruthy();
  });

  it("sends provider, model, and mode chips to backend filters", async () => {
    const onBackendFiltersChange = vi.fn();
    renderList(
      [
        makeSession({
          id: "codex-codex-model",
          name: "Codex target",
          provider_id: "codex",
          model: "gpt-5-codex",
          orchestration_mode: "native",
        }),
        makeSession({
          id: "codex-other-model",
          name: "Codex other",
          provider_id: "codex",
          model: "gpt-5",
          orchestration_mode: "native",
        }),
        makeSession({
          id: "claude-team",
          name: "Claude team",
          provider_id: "claude",
          model: "claude-sonnet-4-6",
          orchestration_mode: "team",
        }),
      ],
      { onBackendFiltersChange },
    );

    fireEvent.click(screen.getByRole("button", { name: "session.advancedFilterPanel" }));
    fireEvent.click(screen.getByRole("button", { name: "Codex" }));
    fireEvent.click(screen.getByRole("button", { name: "gpt-5-codex" }));
    fireEvent.click(screen.getByRole("button", { name: "session.native" }));

    await waitFor(() =>
      expect(onBackendFiltersChange).toHaveBeenLastCalledWith(
        expect.objectContaining({
          providerIds: ["codex"],
          modelIds: ["gpt-5-codex"],
          modes: ["native"],
        }),
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "session.clearFilters" }));

    await waitFor(() =>
      expect(onBackendFiltersChange).toHaveBeenLastCalledWith(
        expect.objectContaining({
          providerIds: [],
          modelIds: [],
          modes: [],
        }),
      ),
    );
  });

  it("sends source/user-awareness chips to backend filters", async () => {
    const onBackendFiltersChange = vi.fn();
    renderList(
      [
        makeSession({ id: "human", name: "Human", source: "web", user_initiated: true }),
        makeSession({ id: "system", name: "System", source: "internal", user_initiated: false }),
      ],
      { onBackendFiltersChange },
    );

    fireEvent.click(screen.getByRole("button", { name: "session.advancedFilterPanel" }));
    fireEvent.click(screen.getByRole("button", { name: "session.source.user" }));
    fireEvent.click(screen.getByRole("button", { name: "session.source.internal" }));

    await waitFor(() =>
      expect(onBackendFiltersChange).toHaveBeenLastCalledWith(
        expect.objectContaining({ sources: ["user", "internal"] }),
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "session.clearFilters" }));

    await waitFor(() =>
      expect(onBackendFiltersChange).toHaveBeenLastCalledWith(
        expect.objectContaining({ sources: [] }),
      ),
    );
  });

  it("sends file edit mode choices to backend filters", async () => {
    const onBackendFiltersChange = vi.fn();
    renderList(
      [
        makeSession({ id: "normal", name: "Normal" }),
        makeSession({
          id: "file-edit",
          name: "File edit",
          working_mode: "file_editing",
        }),
      ],
      { onBackendFiltersChange },
    );

    fireEvent.click(screen.getByRole("button", { name: "session.advancedFilterPanel" }));
    fireEvent.click(screen.getByRole("button", { name: "session.fileEditMode.yes" }));

    await waitFor(() =>
      expect(onBackendFiltersChange).toHaveBeenLastCalledWith(
        expect.objectContaining({ fileEditMode: "yes" }),
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "session.fileEditMode.no" }));

    await waitFor(() =>
      expect(onBackendFiltersChange).toHaveBeenLastCalledWith(
        expect.objectContaining({ fileEditMode: "no" }),
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "session.clearFilters" }));

    await waitFor(() =>
      expect(onBackendFiltersChange).toHaveBeenLastCalledWith(
        expect.objectContaining({ fileEditMode: "any" }),
      ),
    );
  });

  it("persists search text and advanced filters per project and restores them on project switch", async () => {
    localStorage.clear();
    const onBackendFiltersChange = vi.fn();
    const { rerender } = renderList(
      [makeSession({ id: "a1", name: "A1", cwd: "/tmp/project-a" })],
      { backendProjectPath: "/tmp/project-a", onBackendFiltersChange },
    );

    fireEvent.click(screen.getByRole("button", { name: "session.advancedFilterPanel" }));
    fireEvent.click(screen.getByRole("button", { name: "session.fileEditMode.yes" }));
    await waitFor(() =>
      expect(onBackendFiltersChange).toHaveBeenLastCalledWith(
        expect.objectContaining({ fileEditMode: "yes" }),
      ),
    );

    const stored = JSON.parse(
      localStorage.getItem("better-agent-session-filters-by-project") || "{}",
    );
    expect(stored["/tmp/project-a"]).toEqual(
      expect.objectContaining({ fileEditModeFilter: "yes" }),
    );

    rerender(
      <SessionList
        sessions={[makeSession({ id: "b1", name: "B1", cwd: "/tmp/project-b" })]}
        providers={providers}
        onSelect={() => {}}
        onDelete={() => {}}
        onRename={() => {}}
        onPin={() => {}}
        onUnpinOthers={() => {}}
        onArchive={() => {}}
        onWorkerEligible={() => {}}
        onAgentRenameAllowed={() => {}}
        onDetails={() => {}}
        backendProjectPath="/tmp/project-b"
        onBackendFiltersChange={onBackendFiltersChange}
      />,
    );

    await waitFor(() =>
      expect(onBackendFiltersChange).toHaveBeenLastCalledWith(
        expect.objectContaining({ fileEditMode: "any", projectPath: "/tmp/project-b" }),
      ),
    );

    rerender(
      <SessionList
        sessions={[makeSession({ id: "a1", name: "A1", cwd: "/tmp/project-a" })]}
        providers={providers}
        onSelect={() => {}}
        onDelete={() => {}}
        onRename={() => {}}
        onPin={() => {}}
        onUnpinOthers={() => {}}
        onArchive={() => {}}
        onWorkerEligible={() => {}}
        onAgentRenameAllowed={() => {}}
        onDetails={() => {}}
        backendProjectPath="/tmp/project-a"
        onBackendFiltersChange={onBackendFiltersChange}
      />,
    );

    await waitFor(() =>
      expect(onBackendFiltersChange).toHaveBeenLastCalledWith(
        expect.objectContaining({ fileEditMode: "yes", projectPath: "/tmp/project-a" }),
      ),
    );
  });

  it("requests another page when scrolled near the bottom", () => {
    const onLoadMore = vi.fn();
    const { container } = renderList(
      [makeSession({ id: "s1", name: "One", cwd: "/tmp/project" })],
      { hasMore: true, loadingMore: false, onLoadMore },
    );
    const list = container.querySelector(".session-list-items") as HTMLDivElement;
    Object.defineProperties(list, {
      scrollHeight: { value: 1000, configurable: true },
      scrollTop: { value: 850, configurable: true },
      clientHeight: { value: 100, configurable: true },
    });

    fireEvent.scroll(list);

    expect(onLoadMore).toHaveBeenCalledTimes(1);
  });

  it("keeps the non-selected list in backend recency order and pins the selected session", () => {
    renderList(
      [
        makeSession({
          id: "newer",
          name: "Newer",
          updated_at: "2026-06-16T00:00:00Z",
        }),
        makeSession({
          id: "selected-old",
          name: "Selected old",
          updated_at: "2026-05-29T00:00:00Z",
        }),
      ],
      { currentSessionId: "selected-old" },
    );

    expect(visibleSessionNames()).toEqual(["Newer"]);
    expect(within(screen.getByTestId("session-list-selected")).getByText("Selected old")).toBeTruthy();
  });

  it("does not show search loading for plain list refresh", () => {
    renderList(
      [makeSession({ id: "s1", name: "One", cwd: "/tmp/project" })],
      { searching: true },
    );

    expect(screen.queryByText("session.searching")).toBeNull();
  });

  it("shows search loading for typed session search", () => {
    renderList(
      [makeSession({ id: "s1", name: "Needle", cwd: "/tmp/project" })],
      { searching: true },
    );

    fireEvent.change(
      screen.getByRole("textbox", { name: "session.searchPlaceholder" }),
      { target: { value: "needle" } },
    );

    expect(screen.getByText("session.searching")).toBeTruthy();
  });

  it("renders folderized sessions before unfiled sessions", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/session-organization")) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                schema_version: 1,
                folders: [
                  {
                    id: "folder-parent",
                    project_id: "/tmp/project",
                    parent_folder_id: null,
                    name: "Parent",
                    order: 0,
                    created_at: "2026-01-01T00:00:00Z",
                    updated_at: "2026-01-01T00:00:00Z",
                  },
                  {
                    id: "folder-child",
                    project_id: "/tmp/project",
                    parent_folder_id: "folder-parent",
                    name: "Child",
                    order: 0,
                    created_at: "2026-01-01T00:00:00Z",
                    updated_at: "2026-01-01T00:00:00Z",
                  },
                ],
                tags: [],
                assignments: {},
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        return Promise.resolve(
          new Response(JSON.stringify({ results: [] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );

    render(
      <SessionList
        sessions={[
          makeSession({
            id: "unfiled",
            name: "Unfiled session",
            cwd: "/tmp/project",
          }),
          makeSession({
            id: "foldered",
            name: "Foldered session",
            cwd: "/tmp/project",
            folder_id: "folder-child",
          }),
        ]}
        providers={providers}
        onSelect={() => {}}
        onDelete={() => {}}
        onRename={() => {}}
        onPin={() => {}}
        onUnpinOthers={() => {}}
        onArchive={() => {}}
        onWorkerEligible={() => {}}
        onAgentRenameAllowed={() => {}}
        onDetails={() => {}}
      />,
    );

    await waitFor(() => expect(screen.getByText("Parent")).toBeTruthy());
    expect(screen.getByText("Child")).toBeTruthy();
    expect(screen.getByText("session.unfiled")).toBeTruthy();
    expect(visibleSessionNames()).toEqual(["Foldered session", "Unfiled session"]);
  });

  it("omits the unfiled heading when no folders exist", () => {
    renderList([makeSession({ id: "plain", name: "Plain" })]);

    expect(screen.queryByText("session.unfiled")).toBeNull();
    expect(visibleSessionNames()).toEqual(["Plain"]);
  });

  it("sends missing-provider filters to the backend", async () => {
    const onBackendFiltersChange = vi.fn();
    renderList(
      [
        makeSession({
          id: "missing-provider-target",
          name: "Missing provider target",
          provider_id: "deleted-provider",
          model: "orphan-model",
          orchestration_mode: "native",
        }),
        makeSession({
          id: "known-provider-target",
          name: "Known provider target",
          provider_id: "codex",
          model: "gpt-5-codex",
          orchestration_mode: "native",
        }),
      ],
      { onBackendFiltersChange },
    );

    fireEvent.click(screen.getByRole("button", { name: "session.advancedFilterPanel" }));
    fireEvent.click(screen.getByRole("button", { name: "deleted-provider" }));

    await waitFor(() =>
      expect(onBackendFiltersChange).toHaveBeenLastCalledWith(
        expect.objectContaining({ providerIds: ["deleted-provider"] }),
      ),
    );
  });

  it("applies a folder assignment from the PATCH ack before parent refresh", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/session-organization")) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                schema_version: 1,
                folders: [
                  {
                    id: "folder-client",
                    project_id: "/tmp/project",
                    parent_folder_id: null,
                    name: "Client",
                    order: 0,
                    created_at: "2026-01-01T00:00:00Z",
                    updated_at: "2026-01-01T00:00:00Z",
                  },
                ],
                tags: [],
                assignments: {},
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        if (url.includes("/api/sessions/alpha/organization") && init?.method === "PATCH") {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                session_id: "alpha",
                organization: { folder_id: "folder-client", tag_ids: [] },
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        return Promise.resolve(
          new Response(JSON.stringify({ results: [] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );

    render(
      <SessionList
        sessions={[
          makeSession({ id: "alpha", name: "Alpha", cwd: "/tmp/project" }),
          makeSession({ id: "beta", name: "Beta", cwd: "/tmp/project" }),
        ]}
        providers={providers}
        onSelect={() => {}}
        onDelete={() => {}}
        onRename={() => {}}
        onPin={() => {}}
        onUnpinOthers={() => {}}
        onArchive={() => {}}
        onWorkerEligible={() => {}}
        onAgentRenameAllowed={() => {}}
        onDetails={() => {}}
      />,
    );

    const alphaRow = screen
      .getAllByTestId("session-item")
      .find((item) => item.querySelector(".session-item-name")?.textContent?.trim() === "Alpha");
    expect(alphaRow).toBeTruthy();
    fireEvent.click(within(alphaRow as HTMLElement).getByRole("button", { name: "session.folder" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Client" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Client" }));

    fireEvent.click(screen.getByRole("button", { name: "session.advancedFilterPanel" }));
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Client" }).length).toBeGreaterThan(0));
    const folderFilterChip = screen
      .getAllByRole("button", { name: "Client" })
      .find((button) => button.classList.contains("session-tag-toggle"));
    expect(folderFilterChip).toBeTruthy();
    fireEvent.click(folderFilterChip as HTMLElement);

    expect(screen.getByText("Alpha")).toBeTruthy();
    expect(screen.getByText("Beta")).toBeTruthy();
  });

  it("shows the AI search action only while search is expanded", () => {
    renderList(
      [
        makeSession({
          id: "search-target",
          name: "Search target",
        }),
      ],
      {
        onAiSearch: vi.fn(async () => ({
          results: [],
          reasoning: "",
          error: null,
        })),
      },
    );

    expect(screen.queryByTitle("session.aiSearchRun")).toBeNull();

    fireEvent.focus(screen.getByRole("textbox", { name: "session.searchPlaceholder" }));
    expect(screen.getByTitle("session.aiSearchRun")).toBeTruthy();

    fireEvent.blur(screen.getByRole("textbox", { name: "session.searchPlaceholder" }));
    expect(screen.queryByTitle("session.aiSearchRun")).toBeNull();

    fireEvent.change(screen.getByRole("textbox", { name: "session.searchPlaceholder" }), {
      target: { value: "target" },
    });
    expect(screen.getByTitle("session.aiSearchRun")).toBeTruthy();
  });

  it("renders AI search matches that are outside the loaded session pool", async () => {
    // The loaded pool is paginated: matched sessions may not be loaded.
    // The backend returns full rows for every match — the list must
    // render them instead of intersecting ids with the local pool.
    const loaded = makeSession({ id: "loaded-1", name: "Loaded session" });
    const unloaded = makeSession({ id: "unloaded-1", name: "Unloaded match" });
    renderList([loaded], {
      allSessions: [loaded],
      onAiSearch: vi.fn(async () => ({
        results: [unloaded, loaded],
        reasoning: "matched",
        error: null,
      })),
    });

    fireEvent.change(screen.getByRole("textbox", { name: "session.searchPlaceholder" }), {
      target: { value: "who got a mssg" },
    });
    fireEvent.click(screen.getByTitle("session.aiSearchRun"));

    await waitFor(() => {
      expect(visibleSessionNames()).toEqual(["Unloaded match", "Loaded session"]);
    });
  });

  it("selects AI search matches that are outside the loaded session pool", async () => {
    const onSelect = vi.fn();
    const loaded = makeSession({ id: "loaded-1", name: "Loaded session" });
    const unloaded = makeSession({ id: "unloaded-1", name: "Unloaded match" });
    renderList([loaded], {
      allSessions: [loaded],
      onSelect,
      onAiSearch: vi.fn(async () => ({
        results: [unloaded],
        reasoning: "matched",
        error: null,
      })),
    });

    const searchBox = screen.getByRole("textbox", { name: "session.searchPlaceholder" });
    fireEvent.change(searchBox, {
      target: { value: "who got a mssg" },
    });
    fireEvent.click(screen.getByTitle("session.aiSearchRun"));

    await waitFor(() => {
      expect(visibleSessionNames()).toEqual(["Unloaded match"]);
    });

    fireEvent.click(rowBySessionId("unloaded-1"));
    expect(onSelect).toHaveBeenLastCalledWith("unloaded-1", unloaded);

    onSelect.mockClear();
    fireEvent.keyDown(searchBox, { key: "ArrowDown" });
    fireEvent.keyDown(searchBox, { key: "Enter" });
    expect(onSelect).toHaveBeenLastCalledWith("unloaded-1", unloaded);
  });

  it("shows a loading indicator while backend search is refreshing", () => {
    renderList(
      [makeSession({ id: "search-target", name: "Search target" })],
      { searching: true },
    );

    fireEvent.change(screen.getByRole("textbox", { name: "session.searchPlaceholder" }), {
      target: { value: "target" },
    });

    expect(screen.getByText("session.searching")).toBeTruthy();
  });

  it("keeps bulk selection closed on normal session clicks", () => {
    const onSelect = vi.fn();
    const session = makeSession({ id: "alpha", name: "Alpha" });
    renderList(
      [session],
      { onSelect },
    );

    expect(screen.queryByLabelText("session.selectSession")).toBeNull();
    fireEvent.click(rowBySessionId("alpha"));

    expect(onSelect).toHaveBeenCalledWith("alpha", session);
    expect(screen.queryByTestId("session-bulk-bar")).toBeNull();
    expect(screen.queryByLabelText("session.selectSession")).toBeNull();
  });

  it("starts bulk selection by long pressing a session", () => {
    vi.useFakeTimers();
    const onSelect = vi.fn();
    renderList(
      [
        makeSession({ id: "alpha", name: "Alpha" }),
        makeSession({ id: "beta", name: "Beta" }),
      ],
      { onSelect },
    );

    longPressSession("alpha");
    vi.useRealTimers();

    expect(onSelect).not.toHaveBeenCalled();
    expect(rowBySessionId("alpha").getAttribute("data-selected")).toBe("true");
    expect(screen.getByTestId("session-bulk-bar")).toBeTruthy();
    expect(screen.getAllByLabelText("session.selectSession")).toHaveLength(2);
  });

  it("opens the session action sheet by long pressing a session on mobile", async () => {
    vi.stubGlobal("innerWidth", 390);
    vi.useFakeTimers();
    const onSelect = vi.fn();
    renderListWithMobileSheet(
      [makeSession({ id: "alpha", name: "Alpha" })],
      { onSelect },
    );

    longPressSession("alpha");
    vi.useRealTimers();

    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.queryByTestId("session-bulk-bar")).toBeNull();
    await waitFor(() => expect(document.querySelector(".mobile-action-sheet-header")?.textContent).toBe("Alpha"));
    const sheet = document.querySelector(".mobile-action-sheet") as HTMLElement;
    expect(within(sheet).getByRole("button", { name: "session.pinTitle" })).toBeTruthy();
    expect(within(sheet).getByRole("button", { name: "session.copyAction" })).toBeTruthy();
  });

  it("bulk deletes selected sessions", () => {
    vi.useFakeTimers();
    const onDelete = vi.fn();
    renderList(
      [
        makeSession({ id: "alpha", name: "Alpha" }),
        makeSession({ id: "beta", name: "Beta" }),
        makeSession({ id: "gamma", name: "Gamma" }),
      ],
      { onDelete },
    );

    longPressSession("alpha");
    vi.useRealTimers();
    const checkboxes = screen.getAllByLabelText("session.selectSession");
    fireEvent.click(checkboxes[2]);

    const bulkBar = screen.getByTestId("session-bulk-bar");
    expect(bulkBar.textContent).toContain("session.selectedCount");
    fireEvent.click(within(bulkBar).getByRole("button", { name: /session.deleteSelected/ }));

    expect(onDelete).toHaveBeenCalledTimes(2);
    expect(onDelete).toHaveBeenNthCalledWith(1, "alpha");
    expect(onDelete).toHaveBeenNthCalledWith(2, "gamma");
    expect(screen.queryByTestId("session-bulk-bar")).toBeNull();
  });

  it("bulk moves and tags selected sessions", async () => {
    vi.useFakeTimers();
    const organizationRequests: Array<{ url: string; body: unknown }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/session-organization")) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                schema_version: 1,
                folders: [
                  {
                    id: "folder-client",
                    project_id: "/tmp/project",
                    parent_folder_id: null,
                    name: "Client",
                    order: 0,
                    created_at: "2026-01-01T00:00:00Z",
                    updated_at: "2026-01-01T00:00:00Z",
                  },
                ],
                tags: [
                  {
                    id: "tag-important",
                    project_id: "/tmp/project",
                    name: "Important",
                    color: null,
                    created_at: "2026-01-01T00:00:00Z",
                    updated_at: "2026-01-01T00:00:00Z",
                  },
                ],
                assignments: {},
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        if (url.includes("/api/sessions/") && url.includes("/organization")) {
          const body = JSON.parse(String(init?.body ?? "{}"));
          organizationRequests.push({ url, body });
          return Promise.resolve(
            new Response(
              JSON.stringify({
                session_id: url.includes("alpha") ? "alpha" : "beta",
                organization: {
                  folder_id: body.folder_id,
                  tags: body.tag_ids?.map((id: string) => ({
                    id,
                    project_id: "/tmp/project",
                    name: "Important",
                    color: null,
                    created_at: "2026-01-01T00:00:00Z",
                    updated_at: "2026-01-01T00:00:00Z",
                  })),
                },
              }),
              { status: 200, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        return Promise.resolve(
          new Response(JSON.stringify({ results: [] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );

    render(
      <SessionList
        sessions={[
          makeSession({ id: "alpha", name: "Alpha", cwd: "/tmp/project" }),
          makeSession({ id: "beta", name: "Beta", cwd: "/tmp/project" }),
        ]}
        providers={providers}
        onSelect={() => {}}
        onDelete={() => {}}
        onRename={() => {}}
        onPin={() => {}}
        onUnpinOthers={() => {}}
        onArchive={() => {}}
        onWorkerEligible={() => {}}
        onAgentRenameAllowed={() => {}}
        onDetails={() => {}}
      />,
    );

    longPressSession("alpha");
    vi.useRealTimers();
    const checkboxes = screen.getAllByLabelText("session.selectSession");
    fireEvent.click(checkboxes[1]);

    await waitFor(() => expect(screen.getByTestId("session-bulk-bar")).toBeTruthy());
    const bulkBar = screen.getByTestId("session-bulk-bar");
    fireEvent.click(within(bulkBar).getByRole("button", { name: /session.folder/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Client" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Client" }));

    await waitFor(() =>
      expect(organizationRequests.filter((req) => req.body && (req.body as { folder_id?: string }).folder_id === "folder-client")).toHaveLength(2),
    );

    fireEvent.click(within(bulkBar).getByRole("button", { name: /session.tags/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Important" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Important" }));

    await waitFor(() =>
      expect(organizationRequests.filter((req) => JSON.stringify(req.body).includes("tag-important"))).toHaveLength(2),
    );
  });
});
