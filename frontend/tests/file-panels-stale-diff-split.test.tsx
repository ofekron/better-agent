import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect, useRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";

vi.mock("../src/hooks/useViewport", () => ({
  useViewport: () => ({ mode: "desktop" }),
}));

vi.mock("../src/api", () => ({ API: "" }));

// Stub Monaco DiffEditor so the diff view renders without a real worker.
// FileViewer itself is mocked below, so only DiffEditor (imported directly
// by FilePanels) needs stubbing.
vi.mock("@monaco-editor/react", () => ({
  DiffEditor: (props: { original?: string; modified?: string }) => (
    <div
      data-testid="mock-diff-editor"
      data-original={props.original ?? ""}
      data-modified={props.modified ?? ""}
    />
  ),
}));

// Keep the real identity/text helpers (they bottom out at global fetch, which
// routeFetch controls) and only stub the FileViewer component itself.
vi.mock("../src/components/FileViewer", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/components/FileViewer")>();
  return {
    ...actual,
    // The stub exposes the FilePanels→FileViewer wiring (onClose,
    // onEditorReady) through buttons so those callback bodies can be
    // exercised without a real Monaco editor.
    FileViewer: function MockFileViewer(props: {
      filePath: string;
      initialViewState?: {
        monacoViewState?: { mockValue?: string } | null;
      } | null;
      onViewStateChange?: (state: {
        monacoViewState: { mockValue: string };
        renderedScroll: null;
        markdownEditing: boolean;
        video: null;
      }) => void;
      onClose?: () => void;
      onEditorReady?: (h: unknown) => void;
    }) {
      const [viewState, setViewState] = useState(
        () => props.initialViewState?.monacoViewState?.mockValue ?? "",
      );
      const viewStateRef = useRef(viewState);
      viewStateRef.current = viewState;
      const onViewStateChangeRef = useRef(props.onViewStateChange);
      onViewStateChangeRef.current = props.onViewStateChange;
      useEffect(() => () => {
        onViewStateChangeRef.current?.({
          monacoViewState: { mockValue: viewStateRef.current },
          renderedScroll: null,
          markdownEditing: false,
          video: null,
        });
      }, [props.filePath]);
      return (
        <div
          data-testid="mock-file-viewer"
          data-path={props.filePath}
        >
          source:{props.filePath}
          <input
            aria-label={`state-${props.filePath}`}
            value={viewState}
            onChange={(event) => setViewState(event.currentTarget.value)}
          />
          <button data-testid="stub-close" onClick={props.onClose} />
          <button
            data-testid="stub-editor-ready"
            onClick={() => props.onEditorReady?.({ snapshot: true })}
          />
        </div>
      );
    },
  };
});

const { FilePanels } = await import("../src/components/FilePanels");
const { fetchHtmlPreviewUrl } = await import("../src/components/FilePanels");

const SIGNED_PATH = "about:blank";

type Route = { match: RegExp; body: (url: string) => unknown };

function routeFetch(routes: Route[]) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
    const u = String(url);
    for (const r of routes) {
      if (r.match.test(u)) {
        return { ok: true, json: async () => r.body(u) } as Response;
      }
    }
    throw new Error("unrouted fetch: " + u);
  });
}

function viewerFor(path: string): HTMLElement {
  const viewer = screen.getAllByTestId("mock-file-viewer").find(
    (candidate) => candidate.getAttribute("data-path") === path,
  );
  if (!viewer) throw new Error(`Missing viewer for ${path}`);
  return viewer;
}

function visibleViewer(): HTMLElement {
  const viewer = screen.getAllByTestId("mock-file-viewer").find(
    (candidate) => !candidate.closest(".file-panels-pane")?.hasAttribute("hidden"),
  );
  if (!viewer) throw new Error("Missing visible viewer");
  return viewer;
}

// Identity {mtime_ns:1} on the initial text load, {mtime_ns:2} on every disk
// poll → the preview goes stale and the reload/diff actions appear.
function staleRoutes(textContent = "<html>one</html>", latestContent = "<html>two</html>") {
  let textCalls = 0;
  return routeFetch([
    { match: /\/api\/file\/preview-url/, body: () => ({ url: SIGNED_PATH }) },
    {
      match: /\/api\/file\/metadata/,
      body: () => ({ mtime_ns: 2, size: 100 }),
    },
    {
      match: /\/api\/file\?/,
      body: () => {
        textCalls += 1;
        return {
          content: textCalls === 1 ? textContent : latestContent,
          language: "html",
          mtime_ns: textCalls === 1 ? 1 : 2,
          size: 100,
        };
      },
    },
  ]);
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

async function mountStalePreview(textContent = "<html>one</html>") {
  staleRoutes(textContent);
  render(
    <FilePanels
      panels={[{ id: "p1", path: "/repo/x/index.html" }]}
      onClosePanel={() => {}}
      registerEditor={() => {}}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Browser" }));
  await screen.findByTestId("file-browser-preview");
}

describe("FilePanels browser preview staleness + diff", () => {
  it("surfaces a changed badge, reload, and diff actions once disk changes", async () => {
    await mountStalePreview();

    await waitFor(() => {
      expect(screen.getByText("Changed")).toBeTruthy();
    });
    expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Diff" })).toBeTruthy();
    // Iframe still rendered alongside the stale toolbar.
    expect(screen.getByTestId("file-browser-preview").querySelector("iframe")).toBeTruthy();
  });

  it("shows a before/after diff via DiffEditor and returns on Back", async () => {
    await mountStalePreview();
    await waitFor(() => screen.getByRole("button", { name: "Diff" }));

    fireEvent.click(screen.getByRole("button", { name: "Diff" }));

    const diff = await screen.findByTestId("mock-diff-editor");
    expect(diff.getAttribute("data-original")).toBe("<html>one</html>");
    expect(diff.getAttribute("data-modified")).toBe("<html>two</html>");
    expect(screen.getByText("loaded → latest")).toBeTruthy();
    // iframe hidden while diff is shown
    expect(screen.queryByTestId("file-browser-preview").querySelector("iframe")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => {
      expect(screen.getByTestId("file-browser-preview").querySelector("iframe")).toBeTruthy();
    });
    expect(screen.queryByTestId("mock-diff-editor")).toBeNull();
  });

  it("reloads the preview from disk when Reload is clicked", async () => {
    const spy = staleRoutes();
    render(
      <FilePanels
        panels={[{ id: "p1", path: "/repo/x/index.html" }]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Browser" }));
    await screen.findByTestId("file-browser-preview");
    await waitFor(() => screen.getByRole("button", { name: "Reload" }));

    const callsBefore = spy.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Reload" }));

    await waitFor(() => expect(spy.mock.calls.length).toBeGreaterThan(callsBefore));
  });

  it("treats an unreadable disk identity as synced (poll catch)", async () => {
    routeFetch([
      { match: /\/api\/file\/preview-url/, body: () => ({ url: SIGNED_PATH }) },
      { match: /\/api\/file\/metadata/, body: () => Promise.reject(new Error("net")) },
      {
        match: /\/api\/file\?/,
        body: () => ({ content: "<html>x</html>", language: "html", mtime_ns: 1, size: 100 }),
      },
    ]);

    render(
      <FilePanels
        panels={[{ id: "p1", path: "/repo/x/index.html" }]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Browser" }));
    await screen.findByTestId("file-browser-preview");

    await waitFor(() => expect(screen.getByText("Synced")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "Reload" })).toBeNull();
  });
});

describe("FilePanels tab + split container logic", () => {
  const twoPanels = [
    { id: "p1", path: "/repo/src/App.tsx" },
    { id: "p2", path: "/repo/src/other.ts" },
  ];

  function renderTwo() {
    return render(
      <FilePanels panels={twoPanels} onClosePanel={() => {}} registerEditor={() => {}} />,
    );
  }

  it("cycles the active tab with the ‹ › buttons and Ctrl/Cmd+Tab", () => {
    renderTwo();
    // Active defaults to the last panel (p2 → other.ts).
    expect(screen.getAllByTestId("mock-file-viewer").length).toBe(1);
    expect(visibleViewer().textContent).toContain("/repo/src/other.ts");

    fireEvent.click(screen.getByTitle("Previous file (Ctrl/Cmd+Tab)"));
    expect(visibleViewer().textContent).toContain("/repo/src/App.tsx");

    fireEvent.click(screen.getByTitle("Next file (Ctrl/Cmd+Tab)"));
    expect(visibleViewer().textContent).toContain("/repo/src/other.ts");

    // Keyboard: Shift+Ctrl+Tab reverses.
    fireEvent.keyDown(window, {
      key: "Tab",
      ctrlKey: true,
      shiftKey: true,
    });
    expect(visibleViewer().textContent).toContain("/repo/src/App.tsx");

    // Keyboard: Ctrl+Tab advances.
    fireEvent.keyDown(window, { key: "Tab", ctrlKey: true });
    expect(visibleViewer().textContent).toContain("/repo/src/other.ts");
  });

  it("restores each tab's captured viewer state after switching", () => {
    renderTwo();
    const otherViewer = viewerFor("/repo/src/other.ts");
    fireEvent.change(screen.getByRole("textbox", { name: "state-/repo/src/other.ts" }), {
      target: { value: "kept position" },
    });

    fireEvent.click(screen.getByText("App.tsx"));
    expect(screen.getAllByTestId("mock-file-viewer")).toHaveLength(1);
    expect(otherViewer.isConnected).toBe(false);

    fireEvent.click(screen.getByText("other.ts"));
    expect(viewerFor("/repo/src/other.ts")).not.toBe(otherViewer);
    expect(
      (screen.getByRole("textbox", {
        name: "state-/repo/src/other.ts",
      }) as HTMLInputElement).value,
    ).toBe("kept position");
  });

  it("unmounts retained viewer state when the backend closes its panel", () => {
    const { rerender } = renderTwo();
    fireEvent.click(screen.getByText("App.tsx"));
    fireEvent.change(screen.getByRole("textbox", { name: "state-/repo/src/App.tsx" }), {
      target: { value: "discarded position" },
    });
    fireEvent.click(screen.getByText("other.ts"));

    rerender(
      <FilePanels
        panels={[twoPanels[1]]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );

    expect(screen.queryByRole("textbox", { name: "state-/repo/src/App.tsx" })).toBeNull();
    expect(screen.getAllByTestId("mock-file-viewer")).toHaveLength(1);

    rerender(
      <FilePanels
        panels={[twoPanels[1], twoPanels[0]]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );

    expect(
      (screen.getByRole("textbox", {
        name: "state-/repo/src/App.tsx",
      }) as HTMLInputElement).value,
    ).toBe("");
  });

  it("lets a changed explicit focus override cached viewer state", () => {
    const { rerender } = renderTwo();
    fireEvent.change(screen.getByRole("textbox", { name: "state-/repo/src/other.ts" }), {
      target: { value: "stale position" },
    });
    fireEvent.click(screen.getByText("App.tsx"));

    rerender(
      <FilePanels
        panels={[
          twoPanels[0],
          { ...twoPanels[1], focus: { startLine: 7, endLine: 7 } },
        ]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );
    fireEvent.click(screen.getByText("other.ts"));

    expect(
      (screen.getByRole("textbox", {
        name: "state-/repo/src/other.ts",
      }) as HTMLInputElement).value,
    ).toBe("");
  });

  it("calls onClosePanel from a tab close button", () => {
    const onClosePanel = vi.fn();
    render(<FilePanels panels={twoPanels} onClosePanel={onClosePanel} registerEditor={() => {}} />);
    const closes = screen.getAllByTitle("Close file");
    fireEvent.click(closes[0]);
    expect(onClosePanel).toHaveBeenCalledWith("p1");
  });

  it("splits panes side by side, unsets split on tab click, and re-merges", () => {
    renderTwo();
    expect(screen.getAllByTestId("mock-file-viewer").length).toBe(1);
    const otherViewer = viewerFor("/repo/src/other.ts");

    fireEvent.click(screen.getByRole("button", { name: "Split" }));
    expect(screen.getAllByTestId("mock-file-viewer").length).toBe(2);
    expect(screen.getByRole("button", { name: "Unsplit" })).toBeTruthy();
    expect(viewerFor("/repo/src/other.ts")).toBe(otherViewer);
    const appViewer = viewerFor("/repo/src/App.tsx");

    // Clicking a tab while split unsets split and focuses that tab.
    fireEvent.click(screen.getByText("App.tsx"));
    expect(screen.getAllByTestId("mock-file-viewer").length).toBe(1);
    expect(viewerFor("/repo/src/App.tsx")).toBe(appViewer);
    expect(visibleViewer().textContent).toContain("/repo/src/App.tsx");

    // Split again and toggle back via the button.
    fireEvent.click(screen.getByRole("button", { name: "Split" }));
    fireEvent.click(screen.getByRole("button", { name: "Unsplit" }));
    expect(screen.getAllByTestId("mock-file-viewer").length).toBe(1);
  });

  it("renders nothing for an empty panel list and clears active on drain", () => {
    const { container, rerender } = render(
      <FilePanels panels={twoPanels} onClosePanel={() => {}} registerEditor={() => {}} />,
    );
    expect(container.querySelector(".file-panels")).toBeTruthy();

    rerender(
      <FilePanels panels={[]} onClosePanel={() => {}} registerEditor={() => {}} />,
    );
    expect(container.querySelector(".file-panels")).toBeNull();
  });

  it("drops the browser view mode when its panel disappears", async () => {
    routeFetch([
      { match: /\/api\/file\/preview-url/, body: () => ({ url: SIGNED_PATH }) },
      { match: /\/api\/file\/metadata/, body: () => ({ mtime_ns: 1, size: 100 }) },
      {
        match: /\/api\/file\?/,
        body: () => ({ content: "<html>x</html>", language: "html", mtime_ns: 1, size: 100 }),
      },
    ]);

    // p1 (HTML) is last → it is the active panel on mount, so its Browser
    // toggle is the one rendered.
    const { rerender } = render(
      <FilePanels
        panels={[
          { id: "p2", path: "/repo/src/b.ts" },
          { id: "p1", path: "/repo/x/a.html" },
        ]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );
    // Put p1 into browser mode.
    fireEvent.click(screen.getByRole("button", { name: "Browser" }));
    await screen.findByTestId("file-browser-preview");

    // Add another panel while p1 stays in browser mode — the cleanup
    // effect must RETAIN p1's entry (not flag it changed/dropped). The
    // newly-appended p3 becomes active (in source mode), but p1's mode
    // survives in state for when it is re-focused.
    rerender(
      <FilePanels
        panels={[
          { id: "p2", path: "/repo/src/b.ts" },
          { id: "p1", path: "/repo/x/a.html" },
          { id: "p3", path: "/repo/x/c.html" },
        ]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );
    expect(visibleViewer()).toBeTruthy();

    // Remove p1 entirely — the orphaned view-mode entry must be dropped so
    // p1 (if it ever reappears) defaults back to source.
    rerender(
      <FilePanels
        panels={[{ id: "p2", path: "/repo/src/b.ts" }]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );
    expect(screen.queryByTestId("file-browser-preview")).toBeNull();
    expect(visibleViewer()).toBeTruthy();

    // Re-add p1: it should start in source mode (no browser toggle active).
    rerender(
      <FilePanels
        panels={[
          { id: "p2", path: "/repo/src/b.ts" },
          { id: "p1", path: "/repo/x/a.html" },
        ]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );
    expect(visibleViewer()).toBeTruthy();
  });
});

describe("FilePanels view-mode + html helpers", () => {
  it("treats a .htm file as browser-renderable", () => {
    routeFetch([
      { match: /\/api\/file\/preview-url/, body: () => ({ url: SIGNED_PATH }) },
      { match: /\/api\/file\/metadata/, body: () => ({ mtime_ns: 1, size: 100 }) },
      {
        match: /\/api\/file\?/,
        body: () => ({ content: "<html>x</html>", language: "html", mtime_ns: 1, size: 100 }),
      },
    ]);
    render(
      <FilePanels
        panels={[{ id: "p1", path: "/repo/x/legacy.htm" }]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Browser" })).toBeTruthy();
  });

  it("wires the viewer's editor handle and close callback through to the host", () => {
    const registerEditor = vi.fn();
    const onClosePanel = vi.fn();
    render(
      <FilePanels
        panels={[{ id: "p1", path: "/repo/src/App.tsx" }]}
        onClosePanel={onClosePanel}
        registerEditor={registerEditor}
      />,
    );

    fireEvent.click(screen.getByTestId("stub-editor-ready"));
    expect(registerEditor).toHaveBeenCalledWith("/repo/src/App.tsx", { snapshot: true });

    fireEvent.click(screen.getByTestId("stub-close"));
    expect(onClosePanel).toHaveBeenCalledWith("p1");
  });

  it("closes the spawned window when minting the tab preview URL fails", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      throw new Error("net");
    });
    const close = vi.fn();
    vi.spyOn(window, "open").mockReturnValue({ close } as unknown as Window);

    render(
      <FilePanels
        panels={[{ id: "p1", path: "/repo/x/index.html" }]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Open in tab" }));

    await waitFor(() => expect(close).toHaveBeenCalled());
  });

  it("is a no-op to re-click Browser when already in browser mode", async () => {
    routeFetch([
      { match: /\/api\/file\/preview-url/, body: () => ({ url: SIGNED_PATH }) },
      { match: /\/api\/file\/metadata/, body: () => ({ mtime_ns: 1, size: 100 }) },
      {
        match: /\/api\/file\?/,
        body: () => ({ content: "<html>x</html>", language: "html", mtime_ns: 1, size: 100 }),
      },
    ]);
    render(
      <FilePanels
        panels={[{ id: "p1", path: "/repo/x/a.html" }]}
        onClosePanel={() => {}}
        registerEditor={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Browser" }));
    await screen.findByTestId("file-browser-preview");
    // Second Browser click: mode already "browser" — dedup no-op, still in preview.
    fireEvent.click(screen.getByRole("button", { name: "Browser" }));
    expect(screen.getByTestId("file-browser-preview")).toBeTruthy();
  });

  it("fetchHtmlPreviewUrl throws on a non-ok preview-url response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({}),
    } as Response);
    await expect(fetchHtmlPreviewUrl("/x.html", "primary")).rejects.toThrow(
      "preview-url failed: 500",
    );
  });
});
