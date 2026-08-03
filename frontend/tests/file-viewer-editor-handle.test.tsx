import { cleanup, render, waitFor } from "@testing-library/react";
import { useEffect, useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { editor } from "monaco-editor";
import "../src/i18n";
import type { FileEditorHandle } from "../src/components/FileViewer";

// A Monaco-editor stand-in rich enough to drive FileViewer's editor-dependent
// effects (onEditorReady handle getters, context-menu discussion action). It
// records registered callbacks on vi.fn mocks so the test can invoke them.
type MockEditor = {
  addAction: ReturnType<typeof vi.fn>;
  addCommand: ReturnType<typeof vi.fn>;
  createDecorationsCollection: ReturnType<typeof vi.fn>;
  focus: ReturnType<typeof vi.fn>;
  getDomNode: ReturnType<typeof vi.fn>;
  getLayoutInfo: ReturnType<typeof vi.fn>;
  getLineMaxColumn: ReturnType<typeof vi.fn>;
  getModel: ReturnType<typeof vi.fn>;
  getPosition: ReturnType<typeof vi.fn>;
  getSelection: ReturnType<typeof vi.fn>;
  getTargetAtClientPoint: ReturnType<typeof vi.fn>;
  getVisibleRanges: ReturnType<typeof vi.fn>;
  layout: ReturnType<typeof vi.fn>;
  onContextMenu: ReturnType<typeof vi.fn>;
  onDidChangeCursorSelection: ReturnType<typeof vi.fn>;
  revealLinesInCenter: ReturnType<typeof vi.fn>;
  restoreViewState: ReturnType<typeof vi.fn>;
  saveViewState: ReturnType<typeof vi.fn>;
  setSelection: ReturnType<typeof vi.fn>;
};

const CAPTURED_VIEW_STATE = {
  cursorState: [],
  viewState: { scrollTop: 240, scrollLeft: 12 },
  contributionsState: {},
} as unknown as editor.ICodeEditorViewState;

function lineMaxColumn(value: string, line: number): number {
  return (value.split("\n")[line - 1] ?? "").length + 1;
}

function createMockEditor(valueRef: { current: string }): MockEditor {
  const parent = document.createElement("div");
  Object.defineProperty(parent, "offsetHeight", { configurable: true, value: 400 });
  Object.defineProperty(parent, "offsetWidth", { configurable: true, value: 600 });
  const domNode = document.createElement("div");
  parent.appendChild(domNode);
  const dispose = vi.fn();
  return {
    addAction: vi.fn(() => ({ dispose })),
    addCommand: vi.fn(),
    createDecorationsCollection: vi.fn(() => ({ clear: vi.fn() })),
    focus: vi.fn(),
    getDomNode: vi.fn(() => domNode),
    getLayoutInfo: vi.fn(() => ({ height: 400, width: 600 })),
    getLineMaxColumn: vi.fn((line: number) => lineMaxColumn(valueRef.current, line)),
    getModel: vi.fn(() => ({
      getLineCount: () => (valueRef.current === "" ? 1 : valueRef.current.split("\n").length),
      getLineMaxColumn: (line: number) => lineMaxColumn(valueRef.current, line),
      getValueInRange: () => "sel-text",
    })),
    getPosition: vi.fn(() => ({ lineNumber: 1, column: 1 })),
    // Non-collapsed selection so the handle's getSelection() returns a range.
    getSelection: vi.fn(() => ({
      startLineNumber: 1,
      startColumn: 1,
      endLineNumber: 1,
      endColumn: 4,
      isEmpty: () => false,
    })),
    getTargetAtClientPoint: vi.fn(() => ({ position: { lineNumber: 3 } })),
    getVisibleRanges: vi.fn(() => [{ startLineNumber: 1, endLineNumber: 2 }]),
    layout: vi.fn(),
    onContextMenu: vi.fn(() => ({ dispose })),
    onDidChangeCursorSelection: vi.fn(() => ({ dispose })),
    revealLinesInCenter: vi.fn(),
    restoreViewState: vi.fn(),
    saveViewState: vi.fn(() => CAPTURED_VIEW_STATE),
    setSelection: vi.fn(),
  };
}

const editorInstances: MockEditor[] = [];

vi.unmock("../src/components/FileViewer");
vi.doMock("@monaco-editor/react", () => ({
  default: ({ value }: { value?: string }) => (
    <textarea data-testid="mock-editor" value={value ?? ""} readOnly />
  ),
  Editor: ({
    value,
    onMount,
  }: {
    value?: string;
    onMount?: (editor: MockEditor, monaco: unknown) => void;
  }) => {
    const valueRef = useRef(value ?? "");
    valueRef.current = value ?? "";
    const ref = useRef<MockEditor | null>(null);
    if (!ref.current) {
      ref.current = createMockEditor(valueRef);
      editorInstances.push(ref.current);
    }
    useEffect(() => {
      onMount?.(ref.current!, { KeyMod: { CtrlCmd: 1 }, KeyCode: { KeyS: 2 } });
    }, [ref.current, onMount]);
    return <textarea data-testid="mock-editor" value={value ?? ""} readOnly />;
  },
  DiffEditor: () => <div data-testid="mock-diff-editor" />,
}));

const { FileViewer } = await import("../src/components/FileViewer");

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  editorInstances.length = 0;
});

function mockFetch({
  content = "one\ntwo\nthree",
  language = "typescript",
  path = "/p/a.ts",
}: {
  content?: string;
  language?: string;
  path?: string;
} = {}) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => ({
    ok: true,
    json: async () =>
      String(url).includes("/api/file/draft")
        ? { exists: false }
        : {
            content,
            language,
            path,
            mtime_ns: 1,
            size: content.length,
          },
  }) as Response);
}

describe("FileViewer onEditorReady handle", () => {
  it("registers a live handle whose getters read the mounted editor", async () => {
    mockFetch();
    const handles: FileEditorHandle[] = [];
    const onEditorReady = (h: FileEditorHandle | null) => {
      if (h) handles.push(h);
    };

    render(
      <FileViewer filePath="/p/a.ts" onClose={() => {}} onEditorReady={onEditorReady} />,
    );

    await waitFor(() => expect(handles.length).toBeGreaterThan(0));
    const handle = handles[0];
    expect(handle.path).toBe("/p/a.ts");
    expect(handle.getVisibleRange()).toEqual({ startLine: 1, endLine: 2 });
    expect(handle.getCaretPosition()).toEqual({ line: 1, column: 1 });
    expect(handle.getSelection()).toEqual({
      startLine: 1,
      startColumn: 1,
      endLine: 1,
      endColumn: 4,
    });
  });

  it("restores Monaco view state and captures the latest state on unmount", async () => {
    mockFetch();
    const restoredState = {
      cursorState: [],
      viewState: { scrollTop: 120, scrollLeft: 4 },
      contributionsState: {},
    } as unknown as editor.ICodeEditorViewState;
    const onViewStateChange = vi.fn();
    const { unmount } = render(
      <FileViewer
        filePath="/p/a.ts"
        onClose={() => {}}
        initialViewState={{
          monacoViewState: restoredState,
          renderedScroll: null,
          markdownEditing: false,
          video: null,
        }}
        onViewStateChange={onViewStateChange}
      />,
    );

    await waitFor(() => expect(editorInstances).toHaveLength(1));
    const mountedEditor = editorInstances[0];
    await waitFor(() => {
      expect(mountedEditor.restoreViewState).toHaveBeenCalledWith(restoredState);
    });

    unmount();

    expect(onViewStateChange).toHaveBeenCalledWith({
      monacoViewState: CAPTURED_VIEW_STATE,
      renderedScroll: null,
      markdownEditing: false,
      video: null,
    });
    expect(editorInstances).toHaveLength(1);
  });

  it("restores and captures rendered-file scroll position", async () => {
    mockFetch({
      content: "name,count\none,1\ntwo,2",
      language: "csv",
      path: "/p/data.csv",
    });
    const onViewStateChange = vi.fn();
    const { container, unmount } = render(
      <FileViewer
        filePath="/p/data.csv"
        onClose={() => {}}
        initialViewState={{
          monacoViewState: null,
          renderedScroll: { top: 90, left: 30 },
          markdownEditing: false,
          video: null,
        }}
        onViewStateChange={onViewStateChange}
      />,
    );

    const rendered = container.querySelector<HTMLDivElement>(".file-viewer-rendered-wrap");
    expect(rendered).toBeTruthy();
    Object.defineProperties(rendered!, {
      scrollTop: { configurable: true, value: 0, writable: true },
      scrollLeft: { configurable: true, value: 0, writable: true },
    });
    await waitFor(() => {
      expect(rendered?.scrollTop).toBe(90);
      expect(rendered?.scrollLeft).toBe(30);
    });
    rendered!.scrollTop = 210;
    rendered!.scrollLeft = 17;

    unmount();

    expect(onViewStateChange).toHaveBeenCalledWith({
      monacoViewState: null,
      renderedScroll: { top: 210, left: 17 },
      markdownEditing: false,
      video: null,
    });
  });
});

describe("FileViewer context-menu discussion action", () => {
  it("starts a discussion from the editor context menu", async () => {
    mockFetch();
    const started: { path: string; line: number }[] = [];
    const onStartDiscussion = async (filePath: string, line: number) => {
      started.push({ path: filePath, line });
    };

    render(
      <FileViewer
        filePath="/p/a.ts"
        onClose={() => {}}
        onStartDiscussion={onStartDiscussion}
      />,
    );

    // Wait for the editor to mount so the context-menu effect registered its
    // handlers.
    await waitFor(() => expect(editorInstances.length).toBeGreaterThan(0));
    const editor = editorInstances[0];
    await waitFor(() => expect(editor.onContextMenu).toHaveBeenCalled());

    const menuHandler = editor.onContextMenu.mock.calls[0][0];
    // Event with a browserEvent but NO event.target.position — the handler
    // falls back to getTargetAtClientPoint (line 752-759).
    menuHandler({
      event: { browserEvent: { clientX: 10, clientY: 20 } },
      target: {},
    });
    expect(editor.getTargetAtClientPoint).toHaveBeenCalledWith(10, 20);

    // The registered action's run() uses the captured context-menu line.
    await waitFor(() => expect(editor.addAction).toHaveBeenCalled());
    const action = editor.addAction.mock.calls[0][0];
    action.run({ getPosition: () => ({ lineNumber: 9, column: 1 }) });
    expect(started).toContainEqual({ path: "/p/a.ts", line: 3 });
  });

  it("also fires when the menu event carries its own position", async () => {
    mockFetch();
    const started: { path: string; line: number }[] = [];
    render(
      <FileViewer
        filePath="/p/a.ts"
        onClose={() => {}}
        onStartDiscussion={(filePath, line) => {
          started.push({ path: filePath, line });
          return Promise.resolve();
        }}
      />,
    );

    await waitFor(() => expect(editorInstances[0]?.onContextMenu).toHaveBeenCalled());
    const editor = editorInstances[0];
    editor.onContextMenu.mock.calls[0][0]({
      event: { browserEvent: { clientX: 0, clientY: 0 } },
      target: { position: { lineNumber: 7 } },
    });
    editor.addAction.mock.calls[0][0].run({ getPosition: () => null });
    expect(started).toContainEqual({ path: "/p/a.ts", line: 7 });
  });
});
