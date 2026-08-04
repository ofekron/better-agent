import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import type { editor } from "monaco-editor";
import { FileEditor } from "../src/components/FileEditor";

/**
 * Drives the activeEditor-dependent paths of FileEditor that the disk-diff
 * test never reaches (it stubs Monaco and never sets activeEditor). A
 * MockEditor stand-in is handed to FileEditor via the MarkdownFileEditor
 * onMount prop; the child components are stubbed to surface the callbacks
 * FileEditor wires into them. All mutable shared state lives in vi.hoisted
 * so the (hoisted) vi.mock factories can read it.
 */

type ActionOpts = { id: string; label: string; run: (ed: unknown) => void };

interface MockSelection {
  startLineNumber: number;
  endLineNumber: number;
  startColumn: number;
  endColumn: number;
}

interface MockEditor {
  _position: { lineNumber: number; column: number } | null;
  _selection: MockSelection | null;
  _onContextMenu: ((event: unknown) => void) | null;
  _onMouseUp: (() => void) | null;
  _onKeyUp: ((event: { shiftKey?: boolean }) => void) | null;
  _action: ActionOpts | null;
  getPosition: () => { lineNumber: number; column: number } | null;
  setPosition: (p: { lineNumber: number; column: number }) => void;
  getSelection: () => MockSelection | null;
  setSelection: (sel: MockSelection) => void;
  getTargetAtClientPoint: (x: number, y: number) => { position: { lineNumber: number } } | null;
  onContextMenu: (cb: (event: unknown) => void) => { dispose: () => void };
  onMouseUp: (cb: () => void) => { dispose: () => void };
  onKeyUp: (cb: (event: { shiftKey?: boolean }) => void) => { dispose: () => void };
  addAction: (opts: ActionOpts) => { dispose: () => void };
  changeViewZones: (cb: (accessor: { addZone: (z: unknown) => number; removeZone: (id: number) => void; layoutZone: (id: number) => void }) => void) => void;
}

const shared = vi.hoisted(() => {
  function makeEditor(): MockEditor {
    const ed: MockEditor = {
      _position: { lineNumber: 5, column: 2 },
      _selection: null,
      _onContextMenu: null,
      _onMouseUp: null,
      _onKeyUp: null,
      _action: null,
      getPosition: () => ed._position,
      setPosition: (p) => {
        ed._position = p;
      },
      getSelection: () => ed._selection,
      setSelection: (sel) => {
        ed._selection = sel;
      },
      getTargetAtClientPoint: () => ({ position: { lineNumber: 9 } }),
      onContextMenu: (cb) => {
        ed._onContextMenu = cb;
        return { dispose: () => {} };
      },
      onMouseUp: (cb) => {
        ed._onMouseUp = cb;
        return { dispose: () => {} };
      },
      onKeyUp: (cb) => {
        ed._onKeyUp = cb;
        return { dispose: () => {} };
      },
      addAction: (opts) => {
        ed._action = opts;
        return { dispose: () => {} };
      },
      changeViewZones: (cb) => {
        let next = 1;
        cb({
          addZone: () => next++,
          removeZone: () => {},
          layoutZone: () => {},
        });
      },
    };
    return ed;
  }
  return {
    makeEditor,
    editor: null as MockEditor | null,
    panels: [] as Array<{
      discussionId: string;
      onSend: (discussionId: string, prompt: string, clientId: string) => Promise<void>;
      onToggleCollapsed: (discussionId: string, collapsed: boolean) => Promise<void>;
    }>,
    commentBar: null as {
      onSubmit: (c: { selection: { kind: string }; comment: string }) => Promise<void>;
    } | null,
    // Number of times MarkdownFileEditor has fired its deferred onMount.
    // The mock defers onMount to a setTimeout(0) macrotask (the real Monaco
    // onMount is async too); this counter is the event-driven signal the
    // editor has actually mounted after a view-mode remount.
    onMountGen: 0,
  };
});

vi.mock("../src/api", () => ({ API: "/api" }));

vi.mock("../src/components/FileEditorPrimitives", () => ({
  MarkdownFileEditor: (props: {
    formattedTestId: string;
    editTestId: string;
    editing: boolean;
    onMount: (ed: editor.IStandaloneCodeEditor) => void;
    onRequestEdit?: () => void;
    onChange?: (next: string) => void;
    value: string;
  }) => {
    // Defer onMount to a macrotask: the real Monaco onMount fires async, AFTER
    // FileEditor's own mount effects — notably the view-mode effect that
    // resets activeEditor to null. A synchronous child-effect onMount would
    // run before that reset and get clobbered to null.
    useEffect(() => {
      const id = setTimeout(() => {
        shared.onMountGen++;
        props.onMount(shared.editor as unknown as editor.IStandaloneCodeEditor);
      }, 0);
      return () => clearTimeout(id);
    }, []);
    return (
      <div>
        <div data-testid={props.formattedTestId} />
        <div data-testid={props.editTestId} />
        {props.editing ? null : (
          <button type="button" data-testid="mfe-request-edit" onClick={() => props.onRequestEdit?.()}>
            edit
          </button>
        )}
        <button type="button" data-testid="mfe-change" onClick={() => props.onChange?.("# changed\n")}>
          change
        </button>
        <div data-testid="mfe-value">{props.value}</div>
      </div>
    );
  },
}));

vi.mock("../src/components/FileCommentBar", () => ({
  FileCommentBar: (props: {
    selection: unknown;
    onSubmit: (c: { selection: { kind: string }; comment: string }) => Promise<void>;
    onCancel: () => void;
  }) => {
    useEffect(() => {
      shared.commentBar = { onSubmit: props.onSubmit };
    });
    return (
      <div>
        <button
          type="button"
          data-testid="fcb-submit"
          onClick={() =>
            props.onSubmit({
              selection: { kind: "monaco", startLine: 1, endLine: 2, startCol: 1, endCol: 3 },
              comment: "hi",
            })
          }
        >
          submit
        </button>
        <button type="button" data-testid="fcb-cancel" onClick={() => props.onCancel()}>
          cancel
        </button>
      </div>
    );
  },
}));

vi.mock("../src/components/FileDiscussionPanel", () => ({
  FileDiscussionPanel: (props: {
    discussion: { id: string; collapsed?: boolean };
    onSend?: (discussionId: string, prompt: string, clientId: string) => Promise<void>;
    onToggleCollapsed?: (discussionId: string, collapsed: boolean) => Promise<void>;
  }) => {
    useEffect(() => {
      shared.panels.push({
        discussionId: props.discussion.id,
        onSend: props.onSend ?? (async () => {}),
        onToggleCollapsed: props.onToggleCollapsed ?? (async () => {}),
      });
    }, []);
    return <div data-testid="fdp" />;
  },
}));

beforeEach(() => {
  shared.editor = shared.makeEditor();
  shared.panels.length = 0;
  shared.commentBar = null;
  vi.stubGlobal(
    "ResizeObserver",
    class {
      private cb: () => void;
      constructor(cb: () => void) {
        this.cb = cb;
      }
      // Fire once on observe so the view-zone relayout path runs
      // synchronously (the real observer fires async on layout).
      observe() {
        this.cb();
      }
      unobserve() {}
      disconnect() {}
    },
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method === "GET" && url.includes("/api/file")) {
        return { ok: true, status: 200, json: async () => ({ content: "polled\n" }) } as Response;
      }
      if (method === "POST" && url.includes("/api/file")) {
        return { ok: true, status: 200, json: async () => ({ ok: true }) } as Response;
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderEditor(overrides: Record<string, unknown> = {}) {
  return render(
    <FileEditor
      tempFilePath="/tmp/example.md"
      originalContent={"before\n"}
      onSubmitComment={vi.fn(async () => {})}
      {...overrides}
    />,
  );
}

describe("FileEditor file-mode editing", () => {
  it("toggles edit, flushes save via view button, debounce, and Cmd/Ctrl+S", async () => {
    const { container } = renderEditor({ diskWritable: true });

    // File mode renders the formatted view + an edit affordance.
    await waitFor(() =>
      expect(container.querySelector('[data-testid="mfe-request-edit"]')).not.toBeNull(),
    );

    const editBtn = () => container.querySelector('[data-testid="mfe-request-edit"]');
    const changeBtn = () => container.querySelector('[data-testid="mfe-change"]')!;
    const viewBtn = () => container.querySelector('[data-testid="eng-file-md-view"]');

    // Enter markdown edit mode once.
    fireEvent.click(editBtn()!);
    await waitFor(() => expect(viewBtn()).not.toBeNull());

    const postCount = () =>
      (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.filter(
        ([, init]: [unknown, RequestInit?]) => init?.method === "POST",
      ).length;

    // (a) change → let the 1s debounce fire its own flush (debounce callback).
    fireEvent.click(changeBtn());
    await waitFor(() => expect(postCount()).toBeGreaterThan(0), { timeout: 2000 });

    // (b) change → Cmd/Ctrl+S shortcut clears the pending debounce + flushes.
    fireEvent.click(changeBtn());
    container.querySelector(".file-viewer")?.setAttribute("tabindex", "0");
    (container.querySelector(".file-viewer") as HTMLElement)?.focus();
    fireEvent.keyDown(window, { key: "s", metaKey: true });

    // (b2) two rapid changes: the second clears the first's pending debounce.
    fireEvent.click(changeBtn());
    fireEvent.click(changeBtn());

    // (c) change → "view" button (returnToFormattedView) clears + flushes.
    fireEvent.click(changeBtn());
    fireEvent.click(viewBtn()!);
    await waitFor(() => expect(editBtn()).not.toBeNull());
  });
});

describe("FileEditor discussions", () => {
  it("renders discussion view zones and wires send/collapse callbacks", async () => {
    const onSendDiscussionMessage = vi.fn(async () => {});
    const onPatchDiscussion = vi.fn(async () => {});
    const { rerender, unmount } = renderEditor({
      diskWritable: true,
      onPatchDiscussion,
      onSendDiscussionMessage,
      fileDiscussions: [
        { id: "d1", line: 3, collapsed: false },
        { id: "d2", line: 7, collapsed: true },
      ],
    });

    await waitFor(() => expect(shared.panels.length).toBe(2));
    expect(shared.panels[0]!.discussionId).toBe("d1");
    expect(shared.panels[1]!.discussionId).toBe("d2");

    const panel = shared.panels[0]!;
    await panel.onSend("d1", "hello", "client-1");
    expect(onSendDiscussionMessage).toHaveBeenCalledWith("d1", "hello", "client-1");

    await panel.onToggleCollapsed("d1", true);
    expect(onPatchDiscussion).toHaveBeenCalledWith("d1", { collapsed: true });

    // Re-render with a session message that acks the pending onSend message:
    // the pending-messages projection filter runs and drops the acked entry.
    rerender(
      <FileEditor
        tempFilePath="/tmp/example.md"
        originalContent={"before\n"}
        onSubmitComment={vi.fn(async () => {})}
        diskWritable
        onPatchDiscussion={onPatchDiscussion}
        onSendDiscussionMessage={onSendDiscussionMessage}
        fileDiscussions={[
          { id: "d1", line: 3, collapsed: false },
          { id: "d2", line: 7, collapsed: true },
        ]}
        sessionMessages={[
          {
            id: "m1",
            role: "user",
            content: "hello",
            events: [],
            timestamp: "2026-01-01T00:00:00.000Z",
            isStreaming: false,
            client_id: "client-1",
            file_discussion_id: "d1",
          },
        ]}
      />,
    );

    unmount();
  });
});

describe("FileEditor context menu + comment submission", () => {
  it("starts a discussion at the captured context-menu line and submits a monaco-anchored comment", async () => {
    const onStartDiscussion = vi.fn(async () => ({ id: "d-new", line: 9 }));
    const onSubmitComment = vi.fn(async () => {});
    const { container } = renderEditor({
      diskWritable: true,
      onStartDiscussion,
      onSubmitComment,
    });

    await waitFor(() => expect(shared.editor!._action).not.toBeNull());

    // Toggle diff → file to exercise both view-mode tab handlers. The view-mode
    // effect resets activeEditor to null on each switch; the editor is only
    // repopulated when MarkdownFileEditor's deferred onMount macrotask fires
    // again after the remount. Wait for that onMount before proceeding, else
    // handleSubmit below reads a stale activeEditor=null and skips the collapse.
    const onMountGenBeforeToggle = shared.onMountGen;
    fireEvent.click(container.querySelector('[data-testid="eng-view-diff"]')!);
    fireEvent.click(container.querySelector('[data-testid="eng-view-file"]')!);
    await waitFor(() =>
      expect(shared.onMountGen).toBeGreaterThan(onMountGenBeforeToggle),
    );

    // Context-menu: event without an inline position → getTargetAtClientPoint fallback.
    shared.editor!._onContextMenu!({
      event: { browserEvent: { clientX: 10, clientY: 20 } },
      target: {},
    });
    shared.editor!._action!.run(shared.editor);
    await waitFor(() => expect(onStartDiscussion).toHaveBeenCalledWith("/tmp/example.md", 9));

    // Capture a non-collapsed selection so the comment bar arms.
    shared.editor!._selection = { startLineNumber: 1, endLineNumber: 2, startColumn: 1, endColumn: 3 };
    shared.editor!._onMouseUp!();

    fireEvent.click(container.querySelector('[data-testid="fcb-submit"]')!);
    await waitFor(() =>
      expect(onSubmitComment).toHaveBeenCalledWith(
        expect.objectContaining({ filePath: "/tmp/example.md", startLine: 1, endLine: 2, comment: "hi" }),
      ),
    );
    // handleSubmit collapses the selection only AFTER `await onSubmitComment`
    // resolves — the call assertion above proves the call fired but not the
    // post-await collapse, so wait for the collapse itself rather than racing it.
    await waitFor(() =>
      expect(shared.editor!._selection?.startLineNumber).toBe(shared.editor!._selection?.endLineNumber),
    );
  });

  it("ignores a non-monaco comment submission and cancels a pending selection", async () => {
    const onStartDiscussion = vi.fn(async () => ({ id: "d-new", line: 1 }));
    const onSubmitComment = vi.fn(async () => {});
    const { container } = renderEditor({ diskWritable: true, onStartDiscussion, onSubmitComment });

    await waitFor(() => expect(shared.commentBar).not.toBeNull());
    await shared.commentBar!.onSubmit({ selection: { kind: "text" }, comment: "x" });
    expect(onSubmitComment).not.toHaveBeenCalled();

    // onCancel clears any pending selection.
    fireEvent.click(container.querySelector('[data-testid="fcb-cancel"]')!);
  });
});
