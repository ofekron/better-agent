import { describe, expect, it } from "vitest";
import "../src/i18n";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import { buildOpenFilesPreamble } from "../src/utils/openFilesPreamble";

function sentPrompt(h: Awaited<ReturnType<typeof renderApp>>): string {
  const frame = h.outbound.findLast((f) => f.type === "send_message");
  return typeof frame?.prompt === "string" ? frame.prompt : "";
}

describe("open file reminder", () => {
  it("includes concise caret and selected range context", () => {
    const prompt = buildOpenFilesPreamble([
      {
        path: "/tmp/proj/src/app.ts",
        visible: { startLine: 4, endLine: 12 },
        caret: { line: 8, column: 17 },
        selection: {
          startLine: 6,
          startColumn: 3,
          endLine: 7,
          endColumn: 11,
        },
      },
    ]);

    expect(prompt).toContain("Open files in the user's UI");
    expect(prompt).toContain(
      "- /tmp/proj/src/app.ts (view 4-12, caret 8:17, selection 6:3-7:11)",
    );
    expect(prompt).not.toContain("Treat them as likely-relevant context");
  });

  it("dedupes identical open-file state lines", () => {
    const prompt = buildOpenFilesPreamble([
      {
        path: "/tmp/proj/src/app.ts",
        visible: { startLine: 4, endLine: 12 },
        caret: { line: 8, column: 17 },
        selection: null,
      },
      {
        path: "/tmp/proj/src/app.ts",
        visible: { startLine: 4, endLine: 12 },
        caret: { line: 8, column: 17 },
        selection: null,
      },
    ]);

    expect(prompt.match(/\/tmp\/proj\/src\/app\.ts/g)).toHaveLength(1);
  });

  it("does not send the open-file reminder when the right panel is closed", async () => {
    const session = makeSession({
      open_file_panels: [{ id: "file-1", path: "/tmp/proj/src/app.ts" }],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    await h.typeAndSend("look here");

    expect(sentPrompt(h)).toBe("look here");
    expect(sentPrompt(h)).not.toContain("<system-reminder>");
    h.unmount();
  });

  it("sends the open-file reminder when the right panel is open and a file is open", async () => {
    const session = makeSession({
      open_file_panels: [{ id: "file-1", path: "/tmp/proj/src/app.ts" }],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.click(".chat-toolbar-right-panel-toggle");

    await h.typeAndSend("look here");

    const prompt = sentPrompt(h);
    expect(prompt).toContain("<system-reminder>");
    expect(prompt).toContain("/tmp/proj/src/app.ts");
    expect(prompt).toContain("look here");
    h.unmount();
  });

  it("sends the open-file reminder once until the open-file state changes", async () => {
    const session = makeSession({
      open_file_panels: [{ id: "file-1", path: "/tmp/proj/src/app.ts" }],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.click(".chat-toolbar-right-panel-toggle");

    await h.typeAndSend("first");
    expect(sentPrompt(h)).toContain("<system-reminder>");

    await h.typeAndSend("second");
    expect(sentPrompt(h)).toBe("second");

    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: session.id,
        patch: {
          open_file_panels: [
            { id: "file-1", path: "/tmp/proj/src/app.ts" },
            { id: "file-2", path: "/tmp/proj/src/other.ts" },
          ],
        },
        originated_by: "agent-tool",
      },
    });
    await h.flush();

    await h.typeAndSend("third");
    expect(sentPrompt(h)).toContain("<system-reminder>");
    expect(sentPrompt(h)).toContain("/tmp/proj/src/other.ts");
    h.unmount();
  });

  it("opens the Files right panel when another writer adds a file panel", async () => {
    const session = makeSession({ open_file_panels: [] });
    const h = await renderApp({
      seed: {
        sessions: [session],
        files: { "/tmp/proj/src/app.ts": "export const value = 1;\n" },
      },
    });
    await h.selectSession(session.id);

    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: session.id,
        patch: {
          open_file_panels: [{ id: "file-1", path: "/tmp/proj/src/app.ts" }],
        },
        originated_by: "agent-tool",
      },
    });
    await h.flush();

    expect(h.$(".right-panel:not(.right-panel-collapsed)")).toBeTruthy();
    expect(h.$(".right-panel-tab.active")?.textContent).toContain("Files");
    expect(h.raw.container.textContent).toContain("app.ts");
    h.unmount();
  });
});
