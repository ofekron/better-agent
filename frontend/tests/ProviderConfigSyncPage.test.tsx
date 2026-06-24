import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProviderConfigSyncPage, createFetchProviderConfigSyncClient } from "@better-agent/provider-config-sync-ui";
import { eventBus } from "../src/lib/eventBus";

const RESPONSE = {
  files: [
    {
      entry_id: "unified:project:instructions:instructions:/tmp/bc/provider-config-sync/projects/hash/instructions.md",
      path: "/tmp/bc/provider-config-sync/projects/hash/instructions.md",
      content_kind: "file",
      scope: "project",
      category: "instructions",
      capability_id: "instructions",
      capability_key: "project:instructions:instructions",
      capability_name: "General instructions",
      role: "unified",
      label: "General instructions unified",
      language: "markdown",
      content: "UNIFIED",
      token_count: 3,
      exists: true,
      read_error: null,
      writable: true,
      backup_exists: false,
      provider_names: ["Unified"],
      provider_kinds: ["unified"],
    },
  ],
  capabilities: [],
  providers: [
    { kind: "claude", name: "Claude" },
    { kind: "gemini", name: "Gemini" },
  ],
  token_totals: {
    unified: 3,
    specifics: 6,
    all_tracked: 9,
    by_provider: [
      { provider_kind: "claude", provider_name: "Claude", token_count: 2 },
      { provider_kind: "gemini", provider_name: "Gemini", token_count: 4 },
    ],
  },
  groups: {
    global: [],
    project: [
      {
        id: "project:instructions:instructions",
        capability_id: "instructions",
        name: "General instructions",
        scope: "project",
        category: "instructions",
        language: "markdown",
        unified_token_count: 3,
        specific_token_count: 6,
        total_token_count: 9,
        provider_token_counts: [
          { provider_kind: "claude", provider_name: "Claude", token_count: 2 },
          { provider_kind: "gemini", provider_name: "Gemini", token_count: 4 },
        ],
        has_diffs: true,
        specific_count: 2,
        missing_count: 0,
        unified: {
          entry_id: "unified:project:instructions:instructions:/tmp/bc/provider-config-sync/projects/hash/instructions.md",
          path: "/tmp/bc/provider-config-sync/projects/hash/instructions.md",
          content_kind: "file",
          scope: "project",
          category: "instructions",
          capability_id: "instructions",
          capability_key: "project:instructions:instructions",
          capability_name: "General instructions",
          role: "unified",
          label: "General instructions unified",
          language: "markdown",
          content: "UNIFIED",
          token_count: 3,
          exists: true,
          read_error: null,
          writable: true,
          backup_exists: false,
          provider_names: ["Unified"],
          provider_kinds: ["unified"],
        },
        specifics: [
          {
            entry_id: "project:instructions:instructions:file:/tmp/project/CLAUDE.md",
            path: "/tmp/project/CLAUDE.md",
            content_kind: "file",
            scope: "project",
            category: "instructions",
            capability_id: "instructions",
            capability_key: "project:instructions:instructions",
            capability_name: "General instructions",
            role: "specific",
            label: "Claude instructions",
            language: "markdown",
            content: "CLAUDE",
            token_count: 2,
            exists: true,
            read_error: null,
            writable: true,
            backup_exists: false,
            provider_names: ["Claude"],
            provider_kinds: ["claude"],
          },
          {
            entry_id: "project:instructions:instructions:file:/tmp/project/GEMINI.md",
            path: "/tmp/project/GEMINI.md",
            content_kind: "file",
            scope: "project",
            category: "instructions",
            capability_id: "instructions",
            capability_key: "project:instructions:instructions",
            capability_name: "General instructions",
            role: "specific",
            label: "Gemini instructions",
            language: "markdown",
            content: "GEMINI",
            token_count: 4,
            exists: true,
            read_error: null,
            writable: true,
            backup_exists: false,
            provider_names: ["Gemini"],
            provider_kinds: ["gemini"],
          },
        ],
      },
    ],
  },
};

const MCP_RESPONSE = {
  ...RESPONSE,
  groups: {
    global: [],
    project: [
      {
        id: "project:config:mcp",
        capability_id: "mcp",
        name: "MCP servers",
        scope: "project",
        category: "config",
        language: "json",
        has_diffs: true,
        specific_count: 1,
        missing_count: 0,
        unified: {
          entry_id: "unified:project:config:mcp:/tmp/bc/provider-config-sync/projects/hash/mcp.json",
          path: "/tmp/bc/provider-config-sync/projects/hash/mcp.json",
          content_kind: "file",
          scope: "project",
          category: "config",
          capability_id: "mcp",
          capability_key: "project:config:mcp",
          capability_name: "MCP servers",
          role: "unified",
          label: "MCP servers unified",
          language: "json",
          content: '{\n  "mcpServers": {\n    "demo": {\n      "command": "echo",\n      "args": ["hello"]\n    }\n  }\n}\n',
          exists: true,
          read_error: null,
          writable: true,
          backup_exists: false,
          provider_names: ["Unified"],
          provider_kinds: ["unified"],
        },
        specifics: [
          {
            entry_id: "project:config:mcp:file:/tmp/project/.mcp.json",
            path: "/tmp/project/.mcp.json",
            content_kind: "file",
            scope: "project",
            category: "config",
            capability_id: "mcp",
            capability_key: "project:config:mcp",
            capability_name: "MCP servers",
            role: "specific",
            label: "Claude MCP",
            language: "json",
            content: '{\n  "mcpServers": {\n    "other": {\n      "command": "node"\n    }\n  }\n}\n',
            exists: true,
            read_error: null,
            writable: true,
            backup_exists: false,
            provider_names: ["Claude"],
            provider_kinds: ["claude"],
          },
        ],
      },
    ],
  },
};

const AGENT_RESPONSE = {
  ...RESPONSE,
  groups: {
    global: [],
    project: [
      {
        id: "project:agent:agent-reviewer",
        capability_id: "agent-reviewer",
        name: "Custom agent: reviewer",
        scope: "project",
        category: "agent",
        language: "json",
        has_diffs: true,
        specific_count: 1,
        missing_count: 0,
        unified: {
          entry_id: "unified:project:agent:agent-reviewer:/tmp/bc/provider-config-sync/projects/hash/agent-reviewer.json",
          path: "/tmp/bc/provider-config-sync/projects/hash/agent-reviewer.json",
          content_kind: "file",
          scope: "project",
          category: "agent",
          capability_id: "agent-reviewer",
          capability_key: "project:agent:agent-reviewer",
          capability_name: "Custom agent: reviewer",
          role: "unified",
          label: "Custom agent: reviewer unified",
          language: "json",
          content: '{\n  "name": "reviewer",\n  "description": "Reviews code",\n  "instructions": "Review carefully.\\n",\n  "metadata": {\n    "model": "sonnet"\n  }\n}\n',
          exists: true,
          read_error: null,
          writable: true,
          backup_exists: false,
          provider_names: ["Unified"],
          provider_kinds: ["unified"],
        },
        specifics: [
          {
            entry_id: "project:agent:agent-reviewer:markdown_agent:/tmp/project/.claude/agents/reviewer.md",
            path: "/tmp/project/.claude/agents/reviewer.md",
            content_kind: "markdown_agent",
            scope: "project",
            category: "agent",
            capability_id: "agent-reviewer",
            capability_key: "project:agent:agent-reviewer",
            capability_name: "Custom agent: reviewer",
            role: "specific",
            label: "Claude agent",
            language: "json",
            content: '{\n  "name": "reviewer",\n  "description": "Reviews code in Claude",\n  "instructions": "Review carefully.\\n",\n  "metadata": {\n    "model": "sonnet"\n  }\n}\n',
            exists: true,
            read_error: null,
            writable: true,
            backup_exists: false,
            provider_names: ["Claude"],
            provider_kinds: ["claude"],
          },
        ],
      },
    ],
  },
};

const SKILL_RESPONSE = {
  ...RESPONSE,
  groups: {
    global: [],
    project: [
      {
        id: "project:skill:skill-reviewer",
        capability_id: "skill-reviewer",
        name: "Skill: reviewer",
        scope: "project",
        category: "skill",
        language: "json",
        has_diffs: false,
        specific_count: 2,
        missing_count: 1,
        unified: {
          entry_id: "unified:project:skill:skill-reviewer:/tmp/bc/provider-config-sync/projects/hash/skill-reviewer.json",
          path: "/tmp/bc/provider-config-sync/projects/hash/skill-reviewer.json",
          content_kind: "file",
          scope: "project",
          category: "skill",
          capability_id: "skill-reviewer",
          capability_key: "project:skill:skill-reviewer",
          capability_name: "Skill: reviewer",
          role: "unified",
          label: "Skill: reviewer unified",
          language: "json",
          content: '{\n  "name": "reviewer",\n  "description": "Review code",\n  "instructions": "Review carefully.\\n",\n  "metadata": {\n    "allowed-tools": ["Read"]\n  }\n}\n',
          exists: true,
          read_error: null,
          writable: true,
          backup_exists: false,
          provider_names: ["Unified"],
          provider_kinds: ["unified"],
        },
        specifics: [
          {
            entry_id: "project:skill:skill-reviewer:markdown_skill:/tmp/project/.claude/skills/reviewer/SKILL.md",
            path: "/tmp/project/.claude/skills/reviewer/SKILL.md",
            content_kind: "markdown_skill",
            scope: "project",
            category: "skill",
            capability_id: "skill-reviewer",
            capability_key: "project:skill:skill-reviewer",
            capability_name: "Skill: reviewer",
            role: "specific",
            label: "Skill (reviewer)",
            language: "json",
            content: '{\n  "name": "reviewer",\n  "description": "Review code",\n  "instructions": "Review carefully.\\n",\n  "metadata": {\n    "allowed-tools": ["Read"]\n  }\n}\n',
            exists: true,
            read_error: null,
            writable: true,
            backup_exists: false,
            provider_names: ["Claude"],
            provider_kinds: ["claude"],
          },
          {
            entry_id: "project:skill:skill-reviewer:markdown_skill:/tmp/project/.agents/skills/reviewer/SKILL.md",
            path: "/tmp/project/.agents/skills/reviewer/SKILL.md",
            content_kind: "markdown_skill",
            scope: "project",
            category: "skill",
            capability_id: "skill-reviewer",
            capability_key: "project:skill:skill-reviewer",
            capability_name: "Skill: reviewer",
            role: "specific",
            label: "Skill (reviewer)",
            language: "json",
            content: "",
            exists: false,
            read_error: null,
            writable: true,
            backup_exists: false,
            provider_names: ["Gemini", "Codex"],
            provider_kinds: ["gemini", "codex"],
          },
        ],
      },
    ],
  },
};

describe("ProviderConfigSyncPage", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("applies a provider-specific instructions file into the unified tracking file", async () => {
    const applyRequests: RequestInit[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") {
        return Response.json({
          projects: [{ path: "/tmp/project", name: "Project", created_at: "", last_used: "" }],
        });
      }
      if (url.pathname === "/api/provider-config-sync/apply") {
        applyRequests.push(init ?? {});
        return Response.json({ ok: true });
      }
      if (url.pathname === "/api/provider-config-sync") return Response.json(RESPONSE);
      return Response.json({});
    });

    const { container } = render(<ProviderConfigSyncPage open cwd="/tmp/project" onClose={() => {}} client={createFetchProviderConfigSyncClient({ baseUrl: "" })} subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())} />);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);

    expect(await screen.findByText("Provider Config Sync")).toBeTruthy();
    expect(container.querySelectorAll(".provider-config-sync-editor-card")).toHaveLength(1);
    expect(container.querySelector(".provider-config-sync-unified-inline")).toBeNull();
    expect(container.querySelector(".provider-config-sync-specific-content")).toBeTruthy();
    expect(container.querySelector(".provider-config-sync-diff")).toBeNull();
    await waitFor(() => expect(screen.getAllByText("UNIFIED").length).toBeGreaterThan(0));
    expect(screen.getByText("1 changed")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Next diff" })).toBeTruthy();
    expect(container.querySelector(".provider-config-sync-status-dot.diff")).toBeTruthy();
    expect(container.querySelector(".provider-config-sync-diff-cell-left.changed")).toBeTruthy();
    expect(container.querySelector(".provider-config-sync-diff-cell-right.changed")).toBeTruthy();
    expect(screen.getAllByText("CLAUDE").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/9 tok/).length).toBeGreaterThan(0);
    expect(screen.getByText(/estimated tracked config/)).toBeTruthy();
    expect(screen.getAllByText("2 tok").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "From Claude" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "To Claude" })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /Gemini/ }));
    expect(screen.getByRole("button", { name: "From Gemini" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "To Gemini" })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /Claude/ }));
    fireEvent.click(screen.getByRole("button", { name: "From Claude" }));

    await waitFor(() => expect(applyRequests).toHaveLength(1));
    expect(confirm).toHaveBeenCalledWith("This will overwrite existing content in Unified. Continue?");
    expect(JSON.parse(applyRequests[0].body as string)).toMatchObject({
      cwd: "/tmp/project",
      capability_id: "instructions",
      source_entry_id: "project:instructions:instructions:file:/tmp/project/CLAUDE.md",
      target_entry_id: "unified:project:instructions:instructions:/tmp/bc/provider-config-sync/projects/hash/instructions.md",
      expected_source: "CLAUDE",
      expected_target: "UNIFIED",
    });
  });

  it("does not apply over non-empty content when overwrite warning is canceled", async () => {
    const applyRequests: RequestInit[] = [];
    vi.spyOn(window, "confirm").mockReturnValue(false);
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") {
        return Response.json({
          projects: [{ path: "/tmp/project", name: "Project", created_at: "", last_used: "" }],
        });
      }
      if (url.pathname === "/api/provider-config-sync/apply") {
        applyRequests.push(init ?? {});
        return Response.json({ ok: true });
      }
      if (url.pathname === "/api/provider-config-sync") return Response.json(RESPONSE);
      return Response.json({});
    });

    render(<ProviderConfigSyncPage open cwd="/tmp/project" onClose={() => {}} client={createFetchProviderConfigSyncClient({ baseUrl: "" })} subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())} />);

    fireEvent.click(await screen.findByRole("button", { name: "From Claude" }));

    expect(applyRequests).toHaveLength(0);
  });

  it("saves edited diff lines", async () => {
    const writeRequests: RequestInit[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") {
        return Response.json({
          projects: [{ path: "/tmp/project", name: "Project", created_at: "", last_used: "" }],
        });
      }
      if (url.pathname === "/api/provider-config-sync/file") {
        writeRequests.push(init ?? {});
        return Response.json({ ok: true });
      }
      if (url.pathname === "/api/provider-config-sync") return Response.json(RESPONSE);
      return Response.json({});
    });

    render(<ProviderConfigSyncPage open cwd="/tmp/project" onClose={() => {}} client={createFetchProviderConfigSyncClient({ baseUrl: "" })} subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())} />);

    fireEvent.doubleClick(await screen.findByText("CLAUDE"));
    const specificEditor = await screen.findByLabelText("Claude line 1");
    fireEvent.change(specificEditor, { target: { value: "CHANGED" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Claude" }));

    await waitFor(() => expect(writeRequests).toHaveLength(1));
    expect(JSON.parse(writeRequests[0].body as string)).toMatchObject({
      cwd: "/tmp/project",
      entry_id: "project:instructions:instructions:file:/tmp/project/CLAUDE.md",
      expected_content: "CLAUDE",
      content: "CHANGED",
    });
  });

  it("autosaves diff controls into the target side", async () => {
    const writeRequests: RequestInit[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") {
        return Response.json({
          projects: [{ path: "/tmp/project", name: "Project", created_at: "", last_used: "" }],
        });
      }
      if (url.pathname === "/api/provider-config-sync/file") {
        writeRequests.push(init ?? {});
        return Response.json({ ok: true });
      }
      if (url.pathname === "/api/provider-config-sync") return Response.json(RESPONSE);
      return Response.json({});
    });

    render(<ProviderConfigSyncPage open cwd="/tmp/project" onClose={() => {}} client={createFetchProviderConfigSyncClient({ baseUrl: "" })} subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())} />);

    expect(await screen.findByRole("button", { name: "Apply block to Unified" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Apply hunk to Unified" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Apply line to Unified" }));

    await waitFor(() => expect(writeRequests).toHaveLength(1));
    expect(JSON.parse(writeRequests[0].body as string)).toMatchObject({
      entry_id: "unified:project:instructions:instructions:/tmp/bc/provider-config-sync/projects/hash/instructions.md",
      expected_content: "UNIFIED",
      content: "CLAUDE",
    });
  });

  it("restores a provider file from its sync backup", async () => {
    const restoreRequests: RequestInit[] = [];
    const response = JSON.parse(JSON.stringify(RESPONSE));
    response.groups.project[0].specifics[0].backup_exists = true;
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") {
        return Response.json({
          projects: [{ path: "/tmp/project", name: "Project", created_at: "", last_used: "" }],
        });
      }
      if (url.pathname === "/api/provider-config-sync/file/restore") {
        restoreRequests.push(init ?? {});
        return Response.json({ ok: true });
      }
      if (url.pathname === "/api/provider-config-sync") return Response.json(response);
      return Response.json({});
    });

    render(<ProviderConfigSyncPage open cwd="/tmp/project" onClose={() => {}} client={createFetchProviderConfigSyncClient({ baseUrl: "" })} subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())} />);

    fireEvent.click(await screen.findByRole("button", { name: "Rollback Claude" }));

    await waitFor(() => expect(restoreRequests).toHaveLength(1));
    expect(JSON.parse(restoreRequests[0].body as string)).toMatchObject({
      cwd: "/tmp/project",
      entry_id: "project:instructions:instructions:file:/tmp/project/CLAUDE.md",
      expected_content: "CLAUDE",
    });
  });

  it("removes the whole selected capability", async () => {
    const deleteRequests: RequestInit[] = [];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") {
        return Response.json({
          projects: [{ path: "/tmp/project", name: "Project", created_at: "", last_used: "" }],
        });
      }
      if (url.pathname === "/api/provider-config-sync/capability" && init?.method === "DELETE") {
        deleteRequests.push(init ?? {});
        return Response.json({ ok: true, capability_id: "instructions", deleted_paths: ["/tmp/project/CLAUDE.md"] });
      }
      if (url.pathname === "/api/provider-config-sync") return Response.json(RESPONSE);
      return Response.json({});
    });

    render(<ProviderConfigSyncPage open cwd="/tmp/project" onClose={() => {}} client={createFetchProviderConfigSyncClient({ baseUrl: "" })} subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())} />);

    fireEvent.click(await screen.findByRole("button", { name: "Remove capability" }));

    await waitFor(() => expect(deleteRequests).toHaveLength(1));
    expect(window.confirm).toHaveBeenCalledWith("Remove the whole capability General instructions (3 existing files) across unified and provider-specific files?");
    expect(JSON.parse(deleteRequests[0].body as string)).toMatchObject({
      cwd: "/tmp/project",
      scope: "project",
      capability_id: "instructions",
      expected_contents: {
        "unified:project:instructions:instructions:/tmp/bc/provider-config-sync/projects/hash/instructions.md": "UNIFIED",
        "project:instructions:instructions:file:/tmp/project/CLAUDE.md": "CLAUDE",
        "project:instructions:instructions:file:/tmp/project/GEMINI.md": "GEMINI",
      },
    });
  });

  it("creates a unified capability for all selected providers from the sidebar", async () => {
    const createRequests: RequestInit[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") {
        return Response.json({
          projects: [{ path: "/tmp/project", name: "Project", created_at: "", last_used: "" }],
        });
      }
      if (url.pathname === "/api/provider-config-sync/capability" && init?.method === "POST") {
        createRequests.push(init ?? {});
        return Response.json({ ok: true, capability: { id: "project:skill:new-skill" } });
      }
      if (url.pathname === "/api/provider-config-sync") return Response.json(RESPONSE);
      return Response.json({});
    });

    render(<ProviderConfigSyncPage open cwd="/tmp/project" onClose={() => {}} client={createFetchProviderConfigSyncClient({ baseUrl: "" })} subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())} />);

    fireEvent.click(await screen.findByRole("button", { name: "Add capability" }));
    fireEvent.change(screen.getByLabelText("New capability category"), { target: { value: "skill" } });
    fireEvent.change(screen.getByLabelText("New capability name"), { target: { value: "new-skill" } });
    fireEvent.change(screen.getByLabelText("New capability description"), { target: { value: "New skill" } });
    fireEvent.change(screen.getByLabelText("New capability instructions"), { target: { value: "Do the thing." } });
    fireEvent.click(screen.getAllByRole("button", { name: "Add capability" }).at(-1)!);

    await waitFor(() => expect(createRequests).toHaveLength(1));
    expect(JSON.parse(createRequests[0].body as string)).toMatchObject({
      cwd: "/tmp/project",
      scope: "project",
      category: "skill",
      provider_kinds: ["claude", "gemini"],
      name: "new-skill",
      description: "New skill",
      instructions: "Do the thing.",
      metadata: {},
    });
  });

  it("keeps the new capability form open when the sidebar add button is clicked more than once", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") {
        return Response.json({
          projects: [{ path: "/tmp/project", name: "Project", created_at: "", last_used: "" }],
        });
      }
      if (url.pathname === "/api/provider-config-sync") return Response.json(RESPONSE);
      return Response.json({});
    });

    render(<ProviderConfigSyncPage open cwd="/tmp/project" onClose={() => {}} client={createFetchProviderConfigSyncClient({ baseUrl: "" })} subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())} />);

    const addCapabilityButton = await screen.findByRole("button", { name: "Add capability" });
    fireEvent.click(addCapabilityButton);
    expect(screen.getByLabelText("New capability name")).toBeTruthy();

    fireEvent.click(addCapabilityButton);
    expect(screen.getByLabelText("New capability name")).toBeTruthy();
  });

  it("allows changing providers before capability creation", async () => {
    const createRequests: RequestInit[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") {
        return Response.json({
          projects: [{ path: "/tmp/project", name: "Project", created_at: "", last_used: "" }],
        });
      }
      if (url.pathname === "/api/provider-config-sync/capability" && init?.method === "POST") {
        createRequests.push(init ?? {});
        return Response.json({ ok: true, capability: { id: "project:skill:new-skill" } });
      }
      if (url.pathname === "/api/provider-config-sync") return Response.json(RESPONSE);
      return Response.json({});
    });

    render(<ProviderConfigSyncPage open cwd="/tmp/project" onClose={() => {}} client={createFetchProviderConfigSyncClient({ baseUrl: "" })} subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())} />);

    fireEvent.click(await screen.findByRole("button", { name: "Add capability" }));
    fireEvent.click(screen.getByLabelText("Gemini"));
    fireEvent.change(screen.getByLabelText("New capability name"), { target: { value: "new-skill" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Add capability" }).at(-1)!);

    await waitFor(() => expect(createRequests).toHaveLength(1));
    expect(JSON.parse(createRequests[0].body as string)).toMatchObject({
      provider_kinds: ["claude"],
    });
  });

  it("collapses and expands capability groups", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") {
        return Response.json({
          projects: [{ path: "/tmp/project", name: "Project", created_at: "", last_used: "" }],
        });
      }
      if (url.pathname === "/api/provider-config-sync") return Response.json(RESPONSE);
      return Response.json({});
    });

    render(<ProviderConfigSyncPage open cwd="/tmp/project" onClose={() => {}} client={createFetchProviderConfigSyncClient({ baseUrl: "" })} subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())} />);

    const group = await screen.findByRole("button", { name: /Instructions/ });
    expect(screen.getByRole("button", { name: /General instructions/ })).toBeTruthy();
    fireEvent.click(group);
    expect(screen.queryByRole("button", { name: /General instructions/ })).toBeNull();
    fireEvent.click(group);
    expect(screen.getByRole("button", { name: /General instructions/ })).toBeTruthy();
  });

  it("runs LLM auto-fix from provider config sync settings only", async () => {
    const autoRequests: RequestInit[] = [];
    const response = {
      ...RESPONSE,
      auto_settings: {
        global: { additive: "off", removal: "off", change: "off" },
        capabilities: {},
        projects: {
          "/tmp/project": {
            capabilities: {
              instructions: { additive: "review", change: "auto" },
            },
          },
        },
        effective: { additive: "review", removal: "off", change: "auto" },
      },
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") {
        return Response.json({
          projects: [{ path: "/tmp/project", name: "Project", created_at: "", last_used: "" }],
        });
      }
      if (url.pathname === "/api/provider-config-sync/auto-sync") {
        autoRequests.push(init ?? {});
        return Response.json({
          ok: true,
          source_entry_id: "unified:project:instructions:instructions:/tmp/bc/provider-config-sync/projects/hash/instructions.md",
          target_entry_id: "project:instructions:instructions:file:/tmp/project/CLAUDE.md",
          source_path: "/tmp/bc/provider-config-sync/projects/hash/instructions.md",
          target_path: "/tmp/project/CLAUDE.md",
          target_side: "specific",
          applied_count: autoRequests.length,
          pending_count: 1,
          skipped_count: 0,
          log_head: [{
            hunk_id: "h1:changed:1:1",
            operation: "change",
            mode: autoRequests.length === 1 ? "llm" : "off",
            status: autoRequests.length === 1 ? "skipped" : "applied",
            row_count: 1,
            preview: "CLAUDE",
          }],
        });
      }
      if (url.pathname === "/api/provider-config-sync") return Response.json(response);
      return Response.json({});
    });

    render(<ProviderConfigSyncPage open cwd="/tmp/project" onClose={() => {}} client={createFetchProviderConfigSyncClient({ baseUrl: "" })} subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())} />);

    await screen.findByRole("button", { name: "Apply hunk to Claude" });
    expect(screen.queryByRole("button", { name: "AUTO Unified → Specific" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.click(await screen.findByRole("button", { name: "LLM fix selected provider" }));

    await waitFor(() => expect(autoRequests).toHaveLength(1));
    expect(JSON.parse(autoRequests[0].body as string)).toMatchObject({
      source_entry_id: "unified:project:instructions:instructions:/tmp/bc/provider-config-sync/projects/hash/instructions.md",
      target_entry_id: "project:instructions:instructions:file:/tmp/project/CLAUDE.md",
      policy: { additive: "llm", removal: "llm", change: "llm" },
    });

    fireEvent.click(await screen.findByRole("button", { name: "LLM hunk" }));
    await waitFor(() => expect(autoRequests).toHaveLength(2));
    expect(JSON.parse(autoRequests[1].body as string)).toMatchObject({
      policy: { additive: "off", removal: "off", change: "off" },
      llm_hunk_ids: ["h1:changed:1:1"],
    });
  });

  it("saves provider config sync auto policy at the project capability override level", async () => {
    const patchBodies: unknown[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") {
        return Response.json({
          projects: [{ path: "/tmp/project", name: "Project", created_at: "", last_used: "" }],
        });
      }
      if (url.pathname === "/api/provider-config-sync/settings" && init?.method === "PATCH") {
        patchBodies.push(JSON.parse(init.body as string));
        return Response.json({
          global: { additive: "off", removal: "off", change: "off" },
          capabilities: {},
          projects: {
            "/tmp/project": {
              capabilities: {
                instructions: { change: "llm" },
              },
            },
          },
          effective: { additive: "off", removal: "off", change: "llm" },
        });
      }
      if (url.pathname === "/api/provider-config-sync") {
        return Response.json({
          ...RESPONSE,
          auto_settings: {
            global: { additive: "off", removal: "off", change: "off" },
            capabilities: {},
            projects: {},
            effective: { additive: "off", removal: "off", change: "off" },
          },
        });
      }
      return Response.json({});
    });

    render(<ProviderConfigSyncPage open cwd="/tmp/project" onClose={() => {}} client={createFetchProviderConfigSyncClient({ baseUrl: "" })} subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())} />);

    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    fireEvent.change(await screen.findByLabelText("Project capability Edit"), { target: { value: "llm" } });

    await waitFor(() => expect(patchBodies).toContainEqual({
      level: "project_capability",
      cwd: "/tmp/project",
      capability_id: "instructions",
      policy: { additive: "inherit", removal: "inherit", change: "llm" },
    }));
  });

  it("refetches when provider config sync changes", async () => {
    let gets = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") {
        return Response.json({ projects: [] });
      }
      if (url.pathname === "/api/provider-config-sync") {
        gets += 1;
        return Response.json(RESPONSE);
      }
      return Response.json({});
    });

    render(<ProviderConfigSyncPage open cwd="/tmp/project" onClose={() => {}} client={createFetchProviderConfigSyncClient({ baseUrl: "" })} subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())} />);
    await waitFor(() => expect(gets).toBeGreaterThan(0));

    eventBus.publish("provider_config_sync_changed", {
      scope: "project",
      category: "instructions",
      capability_id: "instructions",
      path: "/tmp/project/CLAUDE.md",
      cwd: "/tmp/project",
    });

    await waitFor(() => expect(gets).toBeGreaterThan(1));
  });

  it("collapses and expands a capability group", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") return Response.json({ projects: [] });
      if (url.pathname === "/api/provider-config-sync") return Response.json(RESPONSE);
      return Response.json({});
    });

    const { container } = render(
      <ProviderConfigSyncPage
        open
        cwd="/tmp/project"
        onClose={() => {}}
        client={createFetchProviderConfigSyncClient({ baseUrl: "" })}
        subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())}
      />,
    );
    await screen.findByText("Provider Config Sync");

    const items = () => container.querySelector(".provider-config-sync-capability-group-items");
    expect(items()).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Instructions/ }));
    expect(items()).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Instructions/ }));
    expect(items()).toBeTruthy();
  });

  it("renders MCP as structured server fields", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") {
        return Response.json({
          projects: [{ path: "/tmp/project", name: "Project", created_at: "", last_used: "" }],
        });
      }
      if (url.pathname === "/api/provider-config-sync") return Response.json(MCP_RESPONSE);
      return Response.json({});
    });

    render(<ProviderConfigSyncPage open cwd="/tmp/project" onClose={() => {}} client={createFetchProviderConfigSyncClient({ baseUrl: "" })} subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())} />);

    expect(await screen.findByText(/Missing: demo/)).toBeTruthy();
    expect(screen.getByText(/Only here: other/)).toBeTruthy();
    expect(screen.getAllByText("Command").length).toBeGreaterThan(0);
    expect(screen.getByDisplayValue("other")).toBeTruthy();
    expect(screen.getByDisplayValue("node")).toBeTruthy();
    expect(screen.getByRole("tab", { name: /Claude.*diff/i })).toBeTruthy();
    expect(screen.queryByText('"mcpServers"')).toBeNull();
  });

  it("renders custom agents as structured fields", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") {
        return Response.json({
          projects: [{ path: "/tmp/project", name: "Project", created_at: "", last_used: "" }],
        });
      }
      if (url.pathname === "/api/provider-config-sync") return Response.json(AGENT_RESPONSE);
      return Response.json({});
    });

    render(<ProviderConfigSyncPage open cwd="/tmp/project" onClose={() => {}} client={createFetchProviderConfigSyncClient({ baseUrl: "" })} subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())} />);

    expect((await screen.findAllByText("reviewer")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Description").length).toBeGreaterThan(0);
    expect(await screen.findByText("Reviews code")).toBeTruthy();
    expect(screen.getAllByText("Reviews code in Claude").length).toBeGreaterThan(0);
    expect(screen.getByText("1 changed")).toBeTruthy();
    expect(screen.getAllByText("Instructions").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Provider extensions").length).toBeGreaterThan(0);
    expect(screen.queryByText('"instructions"')).toBeNull();
  });

  it("renders skills as common fields plus provider extensions", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/projects" || url.pathname === "/api/provider-config-sync/projects") {
        return Response.json({
          projects: [{ path: "/tmp/project", name: "Project", created_at: "", last_used: "" }],
        });
      }
      if (url.pathname === "/api/provider-config-sync") return Response.json(SKILL_RESPONSE);
      return Response.json({});
    });

    render(<ProviderConfigSyncPage open cwd="/tmp/project" onClose={() => {}} client={createFetchProviderConfigSyncClient({ baseUrl: "" })} subscribeExternalChanges={(cb) => eventBus.subscribe("provider_config_sync_changed", () => cb())} />);

    expect((await screen.findAllByText("reviewer")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Review code").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Provider extensions").length).toBeGreaterThan(0);
    await screen.findByRole("tab", { name: /Claude.*aligned/i });
    fireEvent.click(screen.getByRole("tab", { name: /Gemini, Codex.*missing/i }));
    expect(screen.getByText("Not configured yet.")).toBeTruthy();
    expect(screen.getByText("Apply unified to create Skill (reviewer).")).toBeTruthy();
    expect(screen.queryByText("---")).toBeNull();
    await waitFor(() => {
      expect(screen.queryByText("This item needs a valid converted shape before it can be shown here.")).toBeNull();
    });
  });
});
