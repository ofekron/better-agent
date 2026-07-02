import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect, useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";

const editorInstances: MockEditor[] = [];

type Selection = {
  startLineNumber: number;
  startColumn: number;
  endLineNumber: number;
  endColumn: number;
};

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
  getVisibleRanges: ReturnType<typeof vi.fn>;
  layout: ReturnType<typeof vi.fn>;
  onContextMenu: ReturnType<typeof vi.fn>;
  onKeyUp: ReturnType<typeof vi.fn>;
  onMouseUp: ReturnType<typeof vi.fn>;
  revealLinesInCenter: ReturnType<typeof vi.fn>;
  setSelection: ReturnType<typeof vi.fn>;
};

function lineCount(value: string): number {
  return value.length === 0 ? 1 : value.split("\n").length;
}

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
      getLineCount: () => lineCount(valueRef.current),
      getLineMaxColumn: (line: number) => lineMaxColumn(valueRef.current, line),
    })),
    getPosition: vi.fn(() => ({ lineNumber: 1, column: 1 })),
    getSelection: vi.fn(() => null),
    getVisibleRanges: vi.fn(() => [{ startLineNumber: 1, endLineNumber: 2 }]),
    layout: vi.fn(),
    onContextMenu: vi.fn(() => ({ dispose })),
    onKeyUp: vi.fn(() => ({ dispose })),
    onMouseUp: vi.fn(() => ({ dispose })),
    revealLinesInCenter: vi.fn(),
    setSelection: vi.fn(),
  };
}

vi.unmock("../src/components/FileViewer");
vi.doMock("@monaco-editor/react", () => ({
  default: ({
    value,
    onChange,
    onMount,
  }: {
    value?: string;
    onChange?: (value: string) => void;
    onMount?: (editor: MockEditor, monaco: { KeyMod: { CtrlCmd: number }; KeyCode: { KeyS: number } }) => void;
  }) => {
    const valueRef = useRef(value ?? "");
    valueRef.current = value ?? "";
    const editorRef = useRef<MockEditor | null>(null);
    if (!editorRef.current) {
      editorRef.current = createMockEditor(valueRef);
      editorInstances.push(editorRef.current);
    }
    const editor = editorRef.current;
    useEffect(() => {
      onMount?.(editor, { KeyMod: { CtrlCmd: 1 }, KeyCode: { KeyS: 2 } });
    }, [editor, onMount]);
    return (
      <textarea
        data-testid="mock-editor"
        value={value ?? ""}
        onChange={(event) => onChange?.(event.currentTarget.value)}
      />
    );
  },
  Editor: ({
    value,
    onChange,
    onMount,
  }: {
    value?: string;
    onChange?: (value: string) => void;
    onMount?: (editor: MockEditor, monaco: { KeyMod: { CtrlCmd: number }; KeyCode: { KeyS: number } }) => void;
  }) => {
    const valueRef = useRef(value ?? "");
    valueRef.current = value ?? "";
    const editorRef = useRef<MockEditor | null>(null);
    if (!editorRef.current) {
      editorRef.current = createMockEditor(valueRef);
      editorInstances.push(editorRef.current);
    }
    const editor = editorRef.current;
    useEffect(() => {
      onMount?.(editor, { KeyMod: { CtrlCmd: 1 }, KeyCode: { KeyS: 2 } });
    }, [editor, onMount]);
    return (
      <textarea
        data-testid="mock-editor"
        value={value ?? ""}
        onChange={(event) => onChange?.(event.currentTarget.value)}
      />
    );
  },
  DiffEditor: () => <div data-testid="mock-diff-editor" />,
}));

const { FileViewer } = await import("../src/components/FileViewer");

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  editorInstances.length = 0;
});

describe("FileViewer focused editing", () => {
  it("does not re-scroll or reset selection while typing in a focused file panel", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => ({
      ok: true,
      json: async () => String(url).includes("/api/file/draft")
        ? { exists: false }
        : {
            content: "one\ntwo\nthree",
            language: "typescript",
            path: "/tmp/project/app.ts",
            mtime_ns: 1,
            size: 13,
          },
    } as Response));

    render(
      <FileViewer
        filePath="/tmp/project/app.ts"
        focus={{ startLine: 2, endLine: 2 }}
        select={{ startLine: 2, startColumn: 2, endLine: 2, endColumn: 4 }}
        onClose={() => {}}
      />,
    );

    const textarea = await screen.findByTestId("mock-editor");

    await waitFor(() => {
      expect(editorInstances.some((editor) => editor.revealLinesInCenter.mock.calls.length > 0)).toBe(true);
    });
    const revealedEditor = editorInstances.find((editor) => editor.revealLinesInCenter.mock.calls.length > 0)!;
    expect(revealedEditor.revealLinesInCenter).toHaveBeenCalledTimes(1);
    expect(revealedEditor.setSelection).toHaveBeenCalledTimes(1);
    expect(revealedEditor.setSelection).toHaveBeenCalledWith({
      startLineNumber: 2,
      startColumn: 2,
      endLineNumber: 2,
      endColumn: 4,
    });

    fireEvent.change(textarea, { target: { value: "one\ntwo edited\nthree" } });
    await act(async () => {
      await Promise.resolve();
      await new Promise((resolve) => requestAnimationFrame(() => resolve(undefined)));
    });

    expect(revealedEditor.revealLinesInCenter).toHaveBeenCalledTimes(1);
    expect(revealedEditor.setSelection).toHaveBeenCalledTimes(1);
    expect(revealedEditor.createDecorationsCollection.mock.calls.length).toBeGreaterThan(1);
  });

  it("re-applies reveal and selection when the requested file selection changes", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => ({
      ok: true,
      json: async () => String(url).includes("/api/file/draft")
        ? { exists: false }
        : {
            content: "one\ntwo\nthree",
            language: "typescript",
            path: "/tmp/project/app.ts",
            mtime_ns: 1,
            size: 13,
          },
    } as Response));

    const { rerender } = render(
      <FileViewer
        filePath="/tmp/project/app.ts"
        focus={{ startLine: 2, endLine: 2 }}
        select={{ startLine: 2, startColumn: 2, endLine: 2, endColumn: 4 }}
        onClose={() => {}}
      />,
    );

    await screen.findByTestId("mock-editor");
    await waitFor(() => {
      expect(editorInstances.some((editor) => editor.revealLinesInCenter.mock.calls.length > 0)).toBe(true);
    });
    const revealedEditor = editorInstances.find((editor) => editor.revealLinesInCenter.mock.calls.length > 0)!;

    rerender(
      <FileViewer
        filePath="/tmp/project/app.ts"
        focus={{ startLine: 3, endLine: 3 }}
        select={{ startLine: 3, startColumn: 1, endLine: 3, endColumn: 6 }}
        onClose={() => {}}
      />,
    );

    await waitFor(() => {
      expect(revealedEditor.revealLinesInCenter).toHaveBeenCalledTimes(2);
      expect(revealedEditor.setSelection).toHaveBeenCalledTimes(2);
    });
    expect(revealedEditor.setSelection).toHaveBeenLastCalledWith({
      startLineNumber: 3,
      startColumn: 1,
      endLineNumber: 3,
      endColumn: 6,
    });
  });

  it("registers a file discussion action in the Monaco context menu", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => ({
      ok: true,
      json: async () => String(url).includes("/api/file/draft")
        ? { exists: false }
        : {
            content: "one\ntwo\nthree",
            language: "typescript",
            path: "/tmp/project/app.ts",
            mtime_ns: 1,
            size: 13,
          },
    } as Response));
    const onStartDiscussion = vi.fn(async () => {});

    render(
      <FileViewer
        filePath="/tmp/project/app.ts"
        onClose={() => {}}
        onStartDiscussion={onStartDiscussion}
      />,
    );

    await screen.findByTestId("mock-editor");
    await waitFor(() => {
      expect(editorInstances.some((editor) => editor.addAction.mock.calls.length > 0)).toBe(true);
    });
    const editor = editorInstances.find((item) => item.addAction.mock.calls.length > 0)!;
    const action = editor.addAction.mock.calls[0][0];
    expect(action).toMatchObject({
      id: "better-agent.start-file-discussion",
      label: "Start discussion",
      contextMenuGroupId: "navigation",
    });

    await action.run(editor);

    expect(onStartDiscussion).toHaveBeenCalledWith("/tmp/project/app.ts", 1);
  });
});
