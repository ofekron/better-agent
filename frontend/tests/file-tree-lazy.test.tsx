// @vitest-environment happy-dom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { fetchWithRetry } = vi.hoisted(() => ({
  fetchWithRetry: vi.fn(),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, params?: Record<string, unknown>) => {
      if (key === "files.searchPlaceholder") return "Filter files...";
      if (key === "files.resultCount") return `${params?.count ?? 0} files`;
      if (key === "files.truncated") return `${params?.count ?? 0} files`;
      return key;
    },
  }),
}));

vi.mock("../src/components/Icon", () => ({
  default: ({ name }: { name: string }) => <span>{name}</span>,
}));

vi.mock("../src/utils/fetchRetry", () => ({
  fetchWithRetry: (...args: unknown[]) => fetchWithRetry(...args),
}));

vi.unmock("../src/components/FileTree");
const { FileTree } = await import("../src/components/FileTree");

afterEach(() => {
  vi.clearAllMocks();
});

function response(body: unknown) {
  return {
    ok: true,
    json: async () => body,
  } as Response;
}

describe("FileTree lazy loading", () => {
  it("loads children on expansion and propagates node/max_depth", async () => {
    fetchWithRetry
      .mockResolvedValueOnce(response({
        name: "repo",
        path: "/repo",
        type: "directory",
        children_loaded: true,
        has_more_children: false,
        children: [
          {
            name: "src",
            path: "/repo/src",
            type: "directory",
            children: [],
            children_loaded: false,
            has_more_children: true,
          },
        ],
      }))
      .mockResolvedValueOnce(response({
        name: "src",
        path: "/repo/src",
        type: "directory",
        children_loaded: true,
        has_more_children: false,
        children: [
          { name: "app.ts", path: "/repo/src/app.ts", type: "file" },
        ],
      }));

    render(
      <FileTree
        cwd="/repo"
        nodeId="node-a"
        onFileClick={() => {}}
      />,
    );

    expect(await screen.findByText(/src/)).toBeTruthy();
    expect(fetchWithRetry).toHaveBeenCalledTimes(1);
    expect(String(fetchWithRetry.mock.calls[0][0])).toContain("max_depth=1");
    expect(String(fetchWithRetry.mock.calls[0][0])).toContain("node_id=node-a");

    fireEvent.click(screen.getByText(/src/));

    expect(await screen.findByText("app.ts")).toBeTruthy();
    expect(fetchWithRetry).toHaveBeenCalledTimes(2);
    expect(String(fetchWithRetry.mock.calls[1][0])).toContain("path=%2Frepo%2Fsrc");
    expect(String(fetchWithRetry.mock.calls[1][0])).toContain("max_depth=1");
    expect(String(fetchWithRetry.mock.calls[1][0])).toContain("node_id=node-a");
  });

  it("dedupes double expansion requests for the same path", async () => {
    let resolveChild!: (value: Response) => void;
    const childPromise = new Promise<Response>((resolve) => {
      resolveChild = resolve;
    });
    fetchWithRetry
      .mockResolvedValueOnce(response({
        name: "repo",
        path: "/repo",
        type: "directory",
        children: [{
          name: "src",
          path: "/repo/src",
          type: "directory",
          children: [],
          children_loaded: false,
          has_more_children: true,
        }],
      }))
      .mockReturnValue(childPromise);

    render(<FileTree cwd="/repo" onFileClick={() => {}} />);
    const src = await screen.findByText(/src/);
    fireEvent.click(src);
    fireEvent.click(src);
    expect(fetchWithRetry).toHaveBeenCalledTimes(2);

    resolveChild(response({
      name: "src",
      path: "/repo/src",
      type: "directory",
      children: [{ name: "app.ts", path: "/repo/src/app.ts", type: "file" }],
    }));
    expect(await screen.findByText("app.ts")).toBeTruthy();
  });

  it("ignores stale lazy responses after cwd changes", async () => {
    let resolveOldChild!: (value: Response) => void;
    const oldChildPromise = new Promise<Response>((resolve) => {
      resolveOldChild = resolve;
    });
    fetchWithRetry
      .mockResolvedValueOnce(response({
        name: "repo-a",
        path: "/repo-a",
        type: "directory",
        children: [{
          name: "src",
          path: "/repo-a/src",
          type: "directory",
          children: [],
          children_loaded: false,
          has_more_children: true,
        }],
      }))
      .mockReturnValueOnce(oldChildPromise)
      .mockResolvedValueOnce(response({
        name: "repo-b",
        path: "/repo-b",
        type: "directory",
        children: [{ name: "other.ts", path: "/repo-b/other.ts", type: "file" }],
      }));

    const rendered = render(<FileTree cwd="/repo-a" onFileClick={() => {}} />);
    fireEvent.click(await screen.findByText(/src/));
    rendered.rerender(<FileTree cwd="/repo-b" onFileClick={() => {}} />);
    expect(await screen.findByText("other.ts")).toBeTruthy();

    resolveOldChild(response({
      name: "src",
      path: "/repo-a/src",
      type: "directory",
      children: [{ name: "stale.ts", path: "/repo-a/src/stale.ts", type: "file" }],
    }));

    await waitFor(() => expect(screen.queryByText("stale.ts")).toBeNull());
  });

  it("keeps search able to show deep results while the tree is shallow", async () => {
    fetchWithRetry
      .mockResolvedValueOnce(response({
        name: "repo",
        path: "/repo",
        type: "directory",
        children: [],
      }))
      .mockResolvedValueOnce(response({
        root: {
          name: "repo",
          path: "/repo",
          type: "directory",
          children: [{ name: "deep.ts", path: "/repo/src/deep.ts", type: "file" }],
        },
        truncated: false,
        count: 1,
      }));

    render(<FileTree cwd="/repo" onFileClick={() => {}} />);
    fireEvent.change(screen.getByPlaceholderText("Filter files..."), {
      target: { value: "deep" },
    });

    expect(await screen.findByText("deep.ts")).toBeTruthy();
    expect(String(fetchWithRetry.mock.calls[1][0])).toContain("/api/files/search");
  });
});

// ---------------------------------------------------------------------------
// Shared fixtures for the broader render/create/search coverage below.
// ---------------------------------------------------------------------------

const TREE_CHILDREN = [
  { name: "a.ts", path: "/proj/a.ts", type: "file" },
  {
    name: "src",
    path: "/proj/src",
    type: "directory",
    children: [],
    children_loaded: false,
    has_more_children: true,
  },
  {
    name: "docs",
    path: "/proj/docs",
    type: "directory",
    children_loaded: true,
    has_more_children: false,
  },
];

function treeResponse(): Response {
  return response({
    name: "root",
    path: "/proj",
    type: "directory",
    children_loaded: true,
    children: TREE_CHILDREN,
  });
}

function res(body: unknown, { ok = true, status = 200 }: { ok?: boolean; status?: number } = {}): Response {
  return { ok, status, json: async () => body } as Response;
}

describe("FileTree rendering", () => {
  it("renders only the header when collapsed and fires onToggleCollapsed", () => {
    fetchWithRetry.mockResolvedValue(treeResponse());
    const onToggle = vi.fn();
    render(<FileTree cwd="/proj" collapsed onFileClick={() => {}} onToggleCollapsed={onToggle} />);
    expect(screen.queryByText("a.ts")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /files\.header/ }));
    expect(onToggle).toHaveBeenCalled();
  });

  it("collapsing after a search resets the query", async () => {
    fetchWithRetry
      .mockResolvedValueOnce(treeResponse())
      .mockResolvedValueOnce(
        res({
          root: { name: "r", path: "/proj", type: "directory", children: [{ name: "hit.ts", path: "/proj/hit.ts", type: "file" }] },
          truncated: false,
          count: 1,
        }),
      )
      .mockResolvedValue(treeResponse());
    const { rerender } = render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    await screen.findByText("a.ts");
    fireEvent.change(screen.getByPlaceholderText("Filter files..."), { target: { value: "hit" } });
    await screen.findByText("hit.ts");
    rerender(<FileTree cwd="/proj" collapsed onFileClick={() => {}} />);
    await waitFor(() => expect(screen.queryByText("hit.ts")).toBeNull());
  });

  it("shows the empty-workspace message when the tree has no children", async () => {
    fetchWithRetry.mockResolvedValue(
      res({ name: "r", path: "/proj", type: "directory", children: [] }),
    );
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    await screen.findByText("files.noWorkspace");
  });

  it("swallows a refresh failure and shows the empty workspace", async () => {
    fetchWithRetry.mockRejectedValue(new Error("network"));
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    await screen.findByText("files.noWorkspace");
  });

  it("fires onFileClick when a file row is clicked", async () => {
    fetchWithRetry.mockResolvedValue(treeResponse());
    const onFileClick = vi.fn();
    render(<FileTree cwd="/proj" onFileClick={onFileClick} />);
    fireEvent.click(await screen.findByText("a.ts"));
    expect(onFileClick).toHaveBeenCalledWith("/proj/a.ts");
  });

  it("reveals an edit button on file hover that fires onEngineerFile", async () => {
    fetchWithRetry.mockResolvedValue(treeResponse());
    const onEngineerFile = vi.fn();
    render(<FileTree cwd="/proj" onFileClick={() => {}} onEngineerFile={onEngineerFile} />);
    const fileRow = (await screen.findByText("a.ts")).closest(".tree-file")!;
    fireEvent.mouseEnter(fileRow);
    const editBtn = fileRow.querySelector(".tree-file-edit-btn") as HTMLButtonElement;
    fireEvent.click(editBtn);
    expect(onEngineerFile).toHaveBeenCalledWith("/proj/a.ts");
  });

  it("toggles a non-lazy directory's arrow without fetching", async () => {
    fetchWithRetry.mockResolvedValue(treeResponse());
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    const docs = (await screen.findByText("docs")).closest(".tree-dir")!;
    fireEvent.click(docs);
    expect(docs.querySelector(".tree-arrow")?.textContent).toBe("v");
    fireEvent.click(docs);
    expect(docs.querySelector(".tree-arrow")?.textContent).toBe(">");
    // only the initial load — the docs toggle issued no extra request
    expect(fetchWithRetry).toHaveBeenCalledTimes(1);
  });

  it("clears the hover state on mouse leave", async () => {
    fetchWithRetry.mockResolvedValue(treeResponse());
    render(<FileTree cwd="/proj" onFileClick={() => {}} onEngineerFile={vi.fn()} />);
    const fileRow = (await screen.findByText("a.ts")).closest(".tree-file")!;
    fireEvent.mouseEnter(fileRow);
    expect(fileRow.querySelector(".tree-file-edit-btn")).toBeTruthy();
    fireEvent.mouseLeave(fileRow);
    await waitFor(() => expect(fileRow.querySelector(".tree-file-edit-btn")).toBeNull());
  });

  it("re-runs refresh when the refresh button is clicked", async () => {
    fetchWithRetry.mockResolvedValue(treeResponse());
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    await screen.findByText("a.ts");
    fireEvent.click(screen.getByRole("button", { name: "files.refreshButton" }));
    await waitFor(() => expect(fetchWithRetry).toHaveBeenCalledTimes(2));
  });

  it("ignores a stale refresh response superseded by a newer one", async () => {
    let resolveFirst!: (v: Response) => void;
    const first = new Promise<Response>((r) => (resolveFirst = r));
    fetchWithRetry
      .mockReturnValueOnce(first) // initial (v0), kept pending
      .mockResolvedValueOnce(treeResponse()); // manual refresh (v1)
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "files.refreshButton" }));
    // late-resolve the stale v0 with a marker that must be discarded
    resolveFirst(
      res({ name: "r", path: "/proj", type: "directory", children: [{ name: "STALE", path: "/proj/STALE", type: "file" }] }),
    );
    await screen.findByText("a.ts");
    expect(screen.queryByText("STALE")).toBeNull();
  });

  it("no-ops refresh and create when cwd is empty", async () => {
    fetchWithRetry.mockResolvedValue(treeResponse());
    render(<FileTree cwd="" onFileClick={() => {}} />);
    // empty cwd: no initial fetch
    await waitFor(() => expect(fetchWithRetry).toHaveBeenCalledTimes(0));
    fireEvent.change(screen.getByPlaceholderText("filePicker.newEntryPlaceholder"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "filePicker.create" }));
    await waitFor(() => expect(fetchWithRetry).toHaveBeenCalledTimes(0));
  });
});

describe("FileTree lazy-load failure", () => {
  it("keeps the directory collapsed when loading children rejects", async () => {
    fetchWithRetry
      .mockResolvedValueOnce(treeResponse())
      .mockRejectedValueOnce(new Error("boom"));
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    const src = await screen.findByText("src");
    fireEvent.click(src);
    await waitFor(() =>
      expect(src.closest(".tree-dir")!.querySelector(".tree-arrow")?.textContent).toBe(">"),
    );
    expect(fetchWithRetry).toHaveBeenCalledTimes(2);
  });

  it("coerces a lazy response with no children key to an empty list", async () => {
    fetchWithRetry
      .mockResolvedValueOnce(treeResponse())
      .mockResolvedValueOnce(res({ name: "src", path: "/proj/src", type: "directory" }));
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    const src = (await screen.findByText("src")).closest(".tree-dir")!;
    fireEvent.click(src);
    await waitFor(() =>
      expect(src.querySelector(".tree-arrow")?.textContent).toBe("v"),
    );
    // expands (children defaults to []), no child rows, no crash
    expect(screen.getAllByText("src").length).toBeGreaterThanOrEqual(1);
  });
});

describe("FileTree search variants", () => {
  it("renders truncated + symbols_unavailable notices", async () => {
    fetchWithRetry
      .mockResolvedValueOnce(treeResponse())
      .mockResolvedValueOnce(
        res({
          root: { name: "r", path: "/proj", type: "directory", children: [{ name: "hit.ts", path: "/proj/hit.ts", type: "file" }] },
          truncated: true,
          count: 500,
          symbols_unavailable: true,
        }),
      )
      .mockResolvedValue(treeResponse());
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    await screen.findByText("a.ts");
    fireEvent.change(screen.getByPlaceholderText("Filter files..."), { target: { value: "h" } });
    await screen.findByText("files.symbolsUnavailable");
    expect(screen.getByText("500 files")).toBeTruthy();
  });

  it("shows no-results when the search root is null", async () => {
    fetchWithRetry
      .mockResolvedValueOnce(treeResponse())
      .mockResolvedValueOnce(res({ root: null, truncated: false, count: 0 }))
      .mockResolvedValue(treeResponse());
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    await screen.findByText("a.ts");
    fireEvent.change(screen.getByPlaceholderText("Filter files..."), { target: { value: "zz" } });
    await screen.findByText("files.noResults");
  });

  it("recovers to no-results when the search response fails to parse", async () => {
    fetchWithRetry
      .mockResolvedValueOnce(treeResponse())
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => Promise.reject(new Error("x")) } as Response)
      .mockResolvedValue(treeResponse());
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    await screen.findByText("a.ts");
    fireEvent.change(screen.getByPlaceholderText("Filter files..."), { target: { value: "q" } });
    await screen.findByText("files.noResults");
  });

  it("clears the query on Escape", async () => {
    fetchWithRetry
      .mockResolvedValueOnce(treeResponse())
      .mockResolvedValueOnce(
        res({
          root: { name: "r", path: "/proj", type: "directory", children: [{ name: "hit.ts", path: "/proj/hit.ts", type: "file" }] },
          truncated: false,
          count: 1,
        }),
      )
      .mockResolvedValue(treeResponse());
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    const input = (await screen.findByPlaceholderText("Filter files...")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "hit" } });
    await screen.findByText("hit.ts");
    fireEvent.keyDown(input, { key: "Escape" });
    await waitFor(() => expect(input.value).toBe(""));
  });

  it("clears the query via the × button", async () => {
    fetchWithRetry
      .mockResolvedValueOnce(treeResponse())
      .mockResolvedValueOnce(
        res({
          root: { name: "r", path: "/proj", type: "directory", children: [{ name: "hit.ts", path: "/proj/hit.ts", type: "file" }] },
          truncated: false,
          count: 1,
        }),
      )
      .mockResolvedValue(treeResponse());
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    const input = (await screen.findByPlaceholderText("Filter files...")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "hit" } });
    await screen.findByText("hit.ts");
    fireEvent.click(screen.getByTitle("files.clearSearch"));
    await waitFor(() => expect(input.value).toBe(""));
  });

  it("re-runs the search when a method chip is toggled", async () => {
    fetchWithRetry
      .mockResolvedValueOnce(treeResponse())
      .mockResolvedValue(res({ root: null, truncated: false, count: 0 }));
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    await screen.findByText("a.ts");
    fireEvent.change(screen.getByPlaceholderText("Filter files..."), { target: { value: "q" } });
    await waitFor(() => expect(fetchWithRetry.mock.calls.some((c) => String(c[0]).includes("/api/files/search"))).toBe(true));
    const searchCallsBefore = fetchWithRetry.mock.calls.filter((c) => String(c[0]).includes("/api/files/search")).length;
    const chips = document.querySelectorAll(".picker-method-chip");
    fireEvent.click(chips[1]); // toggle "name" off
    fireEvent.click(chips[1]); // toggle back on
    await waitFor(() =>
      expect(fetchWithRetry.mock.calls.filter((c) => String(c[0]).includes("/api/files/search")).length).toBeGreaterThan(searchCallsBefore),
    );
  });

  it("treats a search-result directory as force-expanded (click is a no-op)", async () => {
    fetchWithRetry
      .mockResolvedValueOnce(treeResponse())
      .mockResolvedValueOnce(
        res({
          root: {
            name: "r",
            path: "/proj",
            type: "directory",
            children: [
              { name: "sub", path: "/proj/sub", type: "directory", children_loaded: true, children: [{ name: "inner.ts", path: "/proj/sub/inner.ts", type: "file" }] },
            ],
          },
          truncated: false,
          count: 1,
        }),
      )
      .mockResolvedValue(treeResponse());
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    await screen.findByText("a.ts");
    fireEvent.change(screen.getByPlaceholderText("Filter files..."), { target: { value: "sub" } });
    const subDir = (await screen.findByText("sub")).closest(".tree-dir")!;
    // forceExpanded: click does not toggle and issues no lazy fetch
    fireEvent.click(subDir);
    expect(subDir.querySelector(".tree-arrow")?.textContent).toBe("v");
    expect(screen.getByText("inner.ts")).toBeTruthy();
  });
});

describe("FileTree create", () => {
  it("creates a file via Enter, refreshes, and opens it", async () => {
    fetchWithRetry
      .mockResolvedValueOnce(treeResponse()) // initial load
      .mockResolvedValueOnce(res({ name: "new.ts", path: "/proj/new.ts", type: "file" })) // create
      .mockResolvedValueOnce(treeResponse()); // refresh after create
    const onFileClick = vi.fn();
    render(<FileTree cwd="/proj" onFileClick={onFileClick} />);
    const input = (await screen.findByPlaceholderText("filePicker.newEntryPlaceholder")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "new.ts" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(onFileClick).toHaveBeenCalledWith("/proj/new.ts"));
    expect(input.value).toBe("");
  });

  it("creates a directory without opening it", async () => {
    fetchWithRetry
      .mockResolvedValueOnce(treeResponse())
      .mockResolvedValueOnce(res({ name: "d", path: "/proj/d", type: "directory" }))
      .mockResolvedValueOnce(treeResponse());
    const onFileClick = vi.fn();
    render(<FileTree cwd="/proj" onFileClick={onFileClick} />);
    await screen.findByText("a.ts");
    fireEvent.change(screen.getByPlaceholderText("filePicker.newEntryPlaceholder"), { target: { value: "d" } });
    fireEvent.change(screen.getByRole("combobox", { name: "filePicker.createKind" }), { target: { value: "directory" } });
    fireEvent.click(screen.getByRole("button", { name: "filePicker.create" }));
    await waitFor(() => expect(fetchWithRetry.mock.calls.some((c) => String(c[0]).includes("/api/files/create"))).toBe(true));
    expect(onFileClick).not.toHaveBeenCalled();
  });

  it("disables the create button when the name is empty", async () => {
    fetchWithRetry.mockResolvedValue(treeResponse());
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    const btn = (await screen.findByRole("button", { name: "filePicker.create" })) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("ignores non-Enter keys in the create-name input", async () => {
    fetchWithRetry.mockResolvedValue(treeResponse());
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    const input = (await screen.findByPlaceholderText("filePicker.newEntryPlaceholder")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "z" } });
    fireEvent.keyDown(input, { key: "a" });
    await waitFor(() => expect(fetchWithRetry).toHaveBeenCalledTimes(1));
  });

  it("surfaces the server error detail on a failed create", async () => {
    fetchWithRetry
      .mockResolvedValueOnce(treeResponse())
      .mockResolvedValueOnce(res({ detail: "already exists" }, { ok: false, status: 400 }));
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    await screen.findByText("a.ts");
    fireEvent.change(screen.getByPlaceholderText("filePicker.newEntryPlaceholder"), { target: { value: "dup" } });
    fireEvent.click(screen.getByRole("button", { name: "filePicker.create" }));
    await screen.findByText("already exists");
  });

  it("falls back to the HTTP status when the error body fails to parse", async () => {
    fetchWithRetry
      .mockResolvedValueOnce(treeResponse())
      .mockResolvedValueOnce({ ok: false, status: 400, json: async () => Promise.reject(new Error("x")) } as Response);
    render(<FileTree cwd="/proj" onFileClick={() => {}} />);
    await screen.findByText("a.ts");
    fireEvent.change(screen.getByPlaceholderText("filePicker.newEntryPlaceholder"), { target: { value: "z" } });
    fireEvent.click(screen.getByRole("button", { name: "filePicker.create" }));
    await screen.findByText("HTTP 400");
  });

  it("omits the create row when allowCreate is false", async () => {
    fetchWithRetry.mockResolvedValue(treeResponse());
    render(<FileTree cwd="/proj" onFileClick={() => {}} allowCreate={false} />);
    await screen.findByText("a.ts");
    expect(screen.queryByPlaceholderText("filePicker.newEntryPlaceholder")).toBeNull();
  });
});
