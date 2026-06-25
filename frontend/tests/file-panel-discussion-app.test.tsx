import React from "react";
import { act, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { makeSession } from "./fixtures";

type FilePanelsProbeProps = {
  onStartDiscussion?: (filePath: string, line: number) => Promise<unknown>;
};

let latestFilePanelsProps: FilePanelsProbeProps | null = null;

vi.mock("../src/components/FilePanels", () => ({
  FilePanels: (props: FilePanelsProbeProps) => {
    latestFilePanelsProps = props;
    return <div data-testid="file-panels-probe" />;
  },
}));

const { renderApp } = await import("./harness");

describe("file panel discussion wiring", () => {
  const defaultViewport = {
    width: window.innerWidth,
    height: window.innerHeight,
  };

  function setViewport(width: number, height: number): void {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: width,
    });
    Object.defineProperty(window, "innerHeight", {
      configurable: true,
      value: height,
    });
    window.dispatchEvent(new Event("resize"));
  }

  beforeEach(() => {
    setViewport(1280, 900);
    latestFilePanelsProps = null;
    localStorage.clear();
  });

  afterEach(() => {
    setViewport(defaultViewport.width, defaultViewport.height);
  });

  function openFilesPanelFor(sessionId: string): void {
    localStorage.setItem(
      "better-agent-right-panel-states",
      JSON.stringify({ [sessionId]: { open: true, tab: "files" } }),
    );
  }

  it("passes a file action callback for valid empty file-edit sessions", async () => {
    const session = makeSession({
      id: "empty-file-edit",
      working_mode: "file_editing",
      working_mode_meta: {
        persistent: true,
        project_cwd: "/tmp/proj",
        file_paths: [],
        original_contents: {},
      },
      right_panel_open: true,
      right_panel_active_tab: "files",
      open_file_panels: [{ id: "panel-1", path: "/tmp/proj/a.ts" }],
    });
    openFilesPanelFor(session.id);
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    await waitFor(() =>
      expect(latestFilePanelsProps?.onStartDiscussion).toBeTypeOf("function"),
    );
    h.unmount();
  });

  it("starts the editor session and creates the requested discussion", async () => {
    const session = makeSession({
      id: "empty-file-edit",
      working_mode: "file_editing",
      working_mode_meta: {
        persistent: true,
        project_cwd: "/tmp/proj",
        file_paths: [],
        original_contents: {},
      },
      right_panel_open: true,
      right_panel_active_tab: "files",
      open_file_panels: [{ id: "panel-1", path: "/tmp/proj/a.ts" }],
    });
    openFilesPanelFor(session.id);
    const h = await renderApp({
      seed: {
        sessions: [session],
        files: { "/tmp/proj/a.ts": "one\ntwo\nthree" },
      },
    });
    await h.selectSession(session.id);

    await waitFor(() =>
      expect(latestFilePanelsProps?.onStartDiscussion).toBeTypeOf("function"),
    );
    await act(async () => {
      await latestFilePanelsProps?.onStartDiscussion?.("/tmp/proj/a.ts", 7);
    });

    await waitFor(() =>
      expect(h.restCalls).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            method: "POST",
            path: "/api/file-editor/file-edit-2/discussions",
            body: expect.objectContaining({
              file_path: "/tmp/proj/a.ts",
              line: 7,
            }),
          }),
        ]),
      ),
    );
    const editorStartIndex = h.restCalls.findIndex(
      (call) => call.method === "POST" && call.path === "/api/file-editor",
    );
    const discussionStartIndex = h.restCalls.findIndex(
      (call) =>
        call.method === "POST" &&
        call.path === "/api/file-editor/file-edit-2/discussions",
    );
    expect(editorStartIndex).toBeGreaterThanOrEqual(0);
    expect(discussionStartIndex).toBeGreaterThan(editorStartIndex);
    h.unmount();
  });

  it("does not pass a file action callback for malformed legacy file-edit sessions", async () => {
    const session = makeSession({
      id: "legacy-file-edit",
      working_mode: "file_editing",
      working_mode_meta: {
        persistent: true,
        project_cwd: "/tmp/proj",
        file_path: "/tmp/proj/a.ts",
      },
      right_panel_open: true,
      right_panel_active_tab: "files",
      open_file_panels: [{ id: "panel-1", path: "/tmp/proj/a.ts" }],
    });
    openFilesPanelFor(session.id);
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    await waitFor(() =>
      expect(latestFilePanelsProps?.onStartDiscussion).toBeUndefined(),
    );
    h.unmount();
  });

  it("does not pass a file action callback for normal sessions", async () => {
    const session = makeSession({
      id: "normal",
      right_panel_open: true,
      right_panel_active_tab: "files",
      open_file_panels: [{ id: "panel-1", path: "/tmp/proj/a.ts" }],
    });
    openFilesPanelFor(session.id);
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    await waitFor(() =>
      expect(latestFilePanelsProps?.onStartDiscussion).toBeUndefined(),
    );
    h.unmount();
  });
});
