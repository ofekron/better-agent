import fs from "node:fs";
import path from "node:path";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GitTreeView } from "../src/components/GitTreeView";
import { ProjectGitStatus } from "../src/components/ProjectGitStatus";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, values?: Record<string, unknown>) => {
      const labels: Record<string, string> = {
        "gitTree.title": "Repository history",
        "gitTree.open": "Open repository history",
        "gitTree.close": "Close repository history",
        "gitTree.refresh": "Refresh repository history",
        "gitTree.loading": "Loading repository history…",
        "gitTree.loadFailed": "Repository history could not be loaded.",
        "gitTree.retry": "Try again",
        "gitTree.empty": "No commits yet.",
        "gitTree.commits": `Commits · ${values?.count}`,
        "gitTree.changed": `Changes · ${values?.count}`,
      };
      return labels[key] ?? key;
    },
    i18n: { language: "en" },
  }),
}));

const realFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = realFetch;
  vi.restoreAllMocks();
});

describe("GitTreeView", () => {
  it("renders repository history and returns to chat through close", async () => {
    globalThis.fetch = vi.fn(async () => ({
      ok: true,
      json: async () => ({
        is_git: true,
        branch: "dev",
        dirty_count: 2,
        commits: [
          {
            hash: "1234567890abcdef",
            parents: [],
            refs: ["HEAD -> dev", "origin/dev"],
            author: "Ofek",
            authored_at: "2026-07-21T10:00:00Z",
            subject: "Add repository history",
          },
        ],
      }),
    })) as unknown as typeof fetch;
    const onClose = vi.fn();

    render(<GitTreeView cwd="/repo" nodeId="primary" onClose={onClose} />);

    expect(await screen.findByText("Add repository history")).toBeTruthy();
    expect(screen.getAllByText("dev")).toHaveLength(2);
    expect(screen.getByText("Changes · 2")).toBeTruthy();
    expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/git-tree?"),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );

    fireEvent.click(screen.getByRole("button", { name: "Close repository history" }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("opens from the project Git status control", async () => {
    globalThis.fetch = vi.fn(async () => ({
      ok: true,
      json: async () => ({ is_git: true, branch: "dev", modified: [], added: [], deleted: [], untracked: [] }),
    })) as unknown as typeof fetch;
    const onOpenTree = vi.fn();

    render(<ProjectGitStatus cwd="/repo" nodeId="primary" onOpenTree={onOpenTree} />);
    const openButton = await screen.findByRole("button", { name: "Open repository history" });
    fireEvent.click(openButton);

    expect(onOpenTree).toHaveBeenCalledOnce();
  });

  it("replaces the chat branch in the center panel while open", () => {
    const appSource = fs.readFileSync(path.resolve(__dirname, "../src/App.tsx"), "utf8");
    const treeBranch = appSource.indexOf("if (gitTreeOpen && selectedProjectPath)");
    const chatBranch = appSource.indexOf("const chatElement = (");

    expect(treeBranch).toBeGreaterThan(-1);
    expect(treeBranch).toBeLessThan(chatBranch);
    expect(appSource).toContain("onClose={() => setGitTreeOpen(false)}");
  });

  it("shows a retry state when loading fails", async () => {
    globalThis.fetch = vi.fn(async () => ({ ok: false, status: 500 })) as unknown as typeof fetch;

    render(<GitTreeView cwd="/repo" nodeId="primary" onClose={() => undefined} />);

    await waitFor(() => expect(screen.getByText("Repository history could not be loaded.")).toBeTruthy());
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });
});
