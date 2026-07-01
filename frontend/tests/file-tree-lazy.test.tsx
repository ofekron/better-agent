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
