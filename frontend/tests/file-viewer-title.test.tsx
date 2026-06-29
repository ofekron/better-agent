import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";

vi.unmock("../src/components/FileViewer");
vi.doMock("@monaco-editor/react", () => ({
  default: ({ value, onChange }: { value?: string; onChange?: (value: string) => void }) => (
    <textarea
      data-testid="mock-editor"
      value={value ?? ""}
      onChange={(event) => onChange?.(event.currentTarget.value)}
    />
  ),
  Editor: ({ value, onChange }: { value?: string; onChange?: (value: string) => void }) => (
    <textarea
      data-testid="mock-editor"
      value={value ?? ""}
      onChange={(event) => onChange?.(event.currentTarget.value)}
    />
  ),
  DiffEditor: ({ original, modified }: { original?: string; modified?: string }) => (
    <div data-testid="mock-diff-editor">
      <span data-testid="mock-diff-original">{original}</span>
      <span data-testid="mock-diff-modified">{modified}</span>
    </div>
  ),
}));

const { FileViewer } = await import("../src/components/FileViewer");

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("FileViewer title", () => {
  it("shows the full file path in the title line", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ exists: false, content: "", language: "typescript" }),
    } as Response);

    render(
      <FileViewer
        filePath="/tmp/project/src/nested/app.ts"
        onClose={() => {}}
      />,
    );

    expect(screen.getByText("/tmp/project/src/nested/app.ts")).toBeTruthy();
    expect(await screen.findByText("Synced")).toBeTruthy();
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
  });

  it("keeps markdown in edit mode until View is pressed", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => ({
      ok: true,
      json: async () => String(url).includes("/api/file/draft")
        ? { exists: false }
        : { content: "# Title", language: "markdown" },
    } as Response));

    render(
      <FileViewer
        filePath="/tmp/project/notes.md"
        onClose={() => {}}
      />,
    );

    const formatted = await screen.findByTestId("file-viewer-md-formatted");
    fireEvent.doubleClick(formatted);

    expect(screen.getByTestId("file-viewer-md-monaco")).toBeTruthy();
    expect(screen.getByTestId("file-viewer-md-view")).toBeTruthy();

    fireEvent.click(screen.getByTestId("file-viewer-md-view"));

    expect(await screen.findByTestId("file-viewer-md-formatted")).toBeTruthy();
    expect(screen.queryByTestId("file-viewer-md-monaco")).toBeNull();
  });

  it("copies the current content in its original form", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => ({
      ok: true,
      json: async () => String(url).includes("/api/file/draft")
        ? { exists: false }
        : { content: "# Title\n\n**Bold**", language: "markdown" },
    } as Response));

    render(
      <FileViewer
        filePath="/tmp/project/notes.md"
        onClose={() => {}}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId("file-viewer-md-formatted").textContent).toContain("# Title");
    });
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));

    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith("# Title\n\n**Bold**");
    });
    expect(await screen.findByRole("button", { name: "Copied" })).toBeTruthy();
  });

  it("copies rendered selections with styled html", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => ({
      ok: true,
      json: async () => String(url).includes("/api/file/draft")
        ? { exists: false }
        : { content: "**Bold**", language: "markdown" },
    } as Response));

    render(
      <FileViewer
        filePath="/tmp/project/notes.md"
        onClose={() => {}}
      />,
    );

    const formatted = await screen.findByTestId("file-viewer-md-formatted");
    await waitFor(() => expect(formatted.textContent).toContain("**Bold**"));
    const bold = formatted.querySelector("[data-test-md]");
    expect(bold).toBeTruthy();
    const range = document.createRange();
    range.selectNode(bold as Node);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);

    const data: Record<string, string> = {};
    const event = new Event("copy", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", {
      value: {
        setData: vi.fn((type: string, value: string) => {
          data[type] = value;
        }),
      },
    });
    document.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(true);
    expect(data["text/plain"]).toBe("**Bold**");
    expect(data["text/html"]).toContain("**Bold**");
    expect(data["text/html"]).toContain("style=");
  });

  it("shows when the loaded file changed on disk", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      const text = String(url);
      if (text.includes("/api/file/draft")) {
        return {
          ok: true,
          json: async () => ({ exists: false }),
        } as Response;
      }
      if (text.includes("/api/file/metadata")) {
        return {
          ok: true,
          json: async () => ({ path: "/tmp/project/app.ts", mtime_ns: 2, size: 4 }),
        } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          content: "one",
          language: "typescript",
          path: "/tmp/project/app.ts",
          mtime_ns: 1,
          size: 3,
        }),
      } as Response;
    });

    render(
      <FileViewer
        filePath="/tmp/project/app.ts"
        onClose={() => {}}
      />,
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("Changed")).toBeTruthy();
  });

  it("reloads a changed file panel to the latest disk content", async () => {
    let fileReadCount = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      const text = String(url);
      if (text.includes("/api/file/draft")) {
        return {
          ok: true,
          json: async () => ({ exists: false, method: init?.method }),
        } as Response;
      }
      if (text.includes("/api/file/metadata")) {
        return {
          ok: true,
          json: async () => ({ path: "/tmp/project/notes.md", mtime_ns: 2, size: 4 }),
        } as Response;
      }
      fileReadCount += 1;
      return {
        ok: true,
        json: async () => ({
          content: fileReadCount === 1 ? "one" : "two",
          language: "markdown",
          path: "/tmp/project/notes.md",
          mtime_ns: fileReadCount === 1 ? 1 : 2,
          size: fileReadCount === 1 ? 3 : 4,
        }),
      } as Response;
    });

    render(
      <FileViewer
        filePath="/tmp/project/notes.md"
        onClose={() => {}}
      />,
    );

    expect(await screen.findByText("one")).toBeTruthy();
    await screen.findByText("Changed");

    fireEvent.click(screen.getByRole("button", { name: "Reload" }));

    expect(await screen.findByText("two")).toBeTruthy();
    await waitFor(() => expect(screen.queryByText("Changed")).toBeNull());
    expect(fileReadCount).toBe(2);
    expect(fetchMock).toHaveBeenCalled();
  });

  it("shows latest disk diff before updating a changed file panel", async () => {
    let fileReadCount = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      const text = String(url);
      if (text.includes("/api/file/draft")) {
        return {
          ok: true,
          json: async () => ({ exists: false }),
        } as Response;
      }
      if (text.includes("/api/file/metadata")) {
        return {
          ok: true,
          json: async () => ({ path: "/tmp/project/app.ts", mtime_ns: 2, size: 4 }),
        } as Response;
      }
      fileReadCount += 1;
      return {
        ok: true,
        json: async () => ({
          content: fileReadCount === 1 ? "one" : "two",
          language: "typescript",
          path: "/tmp/project/app.ts",
          mtime_ns: fileReadCount === 1 ? 1 : 2,
          size: fileReadCount === 1 ? 3 : 4,
        }),
      } as Response;
    });

    render(
      <FileViewer
        filePath="/tmp/project/app.ts"
        onClose={() => {}}
      />,
    );

    await screen.findByText("Changed");
    fireEvent.click(screen.getByRole("button", { name: "Diff" }));

    expect(await screen.findByTestId("file-viewer-latest-diff")).toBeTruthy();
    expect(screen.getByTestId("mock-diff-original").textContent).toBe("one");
    expect(screen.getByTestId("mock-diff-modified").textContent).toBe("two");

    fireEvent.click(screen.getByRole("button", { name: "Reload" }));

    await waitFor(() => expect(screen.queryByText("Changed")).toBeNull());
    expect(fileReadCount).toBe(2);
    expect(fetchMock).toHaveBeenCalled();
  });

  it("loads a persisted draft and saves it to the original file", async () => {
    const writes: Array<{ url: string; body: Record<string, unknown> | null; method: string }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      const text = String(url);
      const method = init?.method ?? "GET";
      if (method !== "GET") {
        writes.push({
          url: text,
          method,
          body: init?.body ? JSON.parse(String(init.body)) : null,
        });
      }
      if (text.includes("/api/file/draft") && method === "GET") {
        return {
          ok: true,
          json: async () => ({
            exists: true,
            content: "draft",
            base_identity: { mtime_ns: 1, size: 3 },
          }),
        } as Response;
      }
      if (text.includes("/api/file/metadata")) {
        return {
          ok: true,
          json: async () => ({ path: "/tmp/project/app.ts", mtime_ns: 3, size: 5 }),
        } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          content: "disk",
          language: "typescript",
          path: "/tmp/project/app.ts",
          mtime_ns: 1,
          size: 3,
        }),
      } as Response;
    });

    render(
      <FileViewer
        filePath="/tmp/project/app.ts"
        onClose={() => {}}
      />,
    );

    expect(await screen.findByDisplayValue("draft")).toBeTruthy();
    expect(await screen.findByText("Draft")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(writes.some((write) => write.url.includes("/api/file") && write.method === "POST" && write.body?.content === "draft")).toBe(true);
      expect(writes.some((write) => write.url.includes("/api/file/draft") && write.method === "DELETE")).toBe(true);
    });
  });

  it("autosaves editor changes to the persistent draft only", async () => {
    const writes: Array<{ url: string; body: Record<string, unknown> | null; method: string }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      const text = String(url);
      const method = init?.method ?? "GET";
      if (method !== "GET") {
        writes.push({
          url: text,
          method,
          body: init?.body ? JSON.parse(String(init.body)) : null,
        });
      }
      if (text.includes("/api/file/draft")) {
        return {
          ok: true,
          json: async () => method === "GET"
            ? { exists: false }
            : { exists: true, content: "draft", base_identity: { mtime_ns: 1, size: 3 } },
        } as Response;
      }
      if (text.includes("/api/file/metadata")) {
        return {
          ok: true,
          json: async () => ({ path: "/tmp/project/app.ts", mtime_ns: 1, size: 3 }),
        } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          content: "disk",
          language: "typescript",
          path: "/tmp/project/app.ts",
          mtime_ns: 1,
          size: 3,
        }),
      } as Response;
    });

    const { unmount } = render(
      <FileViewer
        filePath="/tmp/project/app.ts"
        onClose={() => {}}
      />,
    );

    fireEvent.change(await screen.findByTestId("mock-editor"), { target: { value: "draft" } });
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 1100));
    });

    await waitFor(() => {
      expect(writes.some((write) => write.url.includes("/api/file/draft") && write.method === "POST" && write.body?.content === "draft")).toBe(true);
    });
    expect(writes.some((write) => write.url.endsWith("/api/file") && write.method === "POST")).toBe(false);
    unmount();
  });
});
