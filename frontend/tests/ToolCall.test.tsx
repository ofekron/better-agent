import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";

// Heavy children are independently covered; stub them so this slice
// isolates ToolCall's own branching.
vi.mock("@monaco-editor/react", () => ({
  default: ({ value, options }: { value?: string; options?: { lineNumbers?: (n: number) => string } }) => (
    <div data-testid="monaco">
      {options?.lineNumbers ? options.lineNumbers(1) : null}
      {value}
    </div>
  ),
}));
vi.mock("../src/components/FileViewer", () => ({
  FileViewer: ({ onClose }: { onClose?: () => void }) => (
    <div data-testid="fileviewer">
      <button data-testid="fv-close" onClick={() => onClose?.()}>
        close
      </button>
    </div>
  ),
}));
vi.mock("../src/components/JsonNode", () => ({
  JsonNode: ({ value }: { value?: unknown }) => (
    <div data-testid="jsonnode">{JSON.stringify(value)}</div>
  ),
}));
const { openExternalLinkMock } = vi.hoisted(() => ({ openExternalLinkMock: vi.fn() }));
vi.mock("../src/utils/externalLink", () => ({ openExternalLink: openExternalLinkMock }));
vi.mock("../src/utils/typography", () => ({ useScaledMonacoFontSize: () => 12 }));

import { ToolCall } from "../src/components/ToolCall";
import { ConfigPanelContext } from "../src/components/configPanelContext";

beforeEach(() => {
  openExternalLinkMock.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

/** Force CSS-overflow so the header truncation effect flips `argsTruncated`
 *  true (happy-dom reports 0 for scrollWidth/clientWidth). */
function withOverflow<T>(fn: () => T): T {
  const sw = Object.getOwnPropertyDescriptor(Element.prototype, "scrollWidth");
  const cw = Object.getOwnPropertyDescriptor(Element.prototype, "clientWidth");
  Object.defineProperty(Element.prototype, "scrollWidth", { configurable: true, get: () => 200 });
  Object.defineProperty(Element.prototype, "clientWidth", { configurable: true, get: () => 50 });
  try {
    return fn();
  } finally {
    if (sw) Object.defineProperty(Element.prototype, "scrollWidth", sw);
    else delete (Element.prototype as Record<string, unknown>).scrollWidth;
    if (cw) Object.defineProperty(Element.prototype, "clientWidth", cw);
    else delete (Element.prototype as Record<string, unknown>).clientWidth;
  }
}

describe("ToolCall — normalizeArgs coercion", () => {
  it("renders null args without crashing", () => {
    const { container } = render(<ToolCall tool="Grep" args={null} />);
    expect(container.querySelector(".tool-call")).toBeTruthy();
  });

  it("stringifies a raw object args shape", () => {
    const { container } = render(
      <ToolCall tool="Grep" args={{ pattern: "x" } as unknown as string} />,
    );
    // object args → JSON-stringified → parsed back → summarized
    expect(container.querySelector(".tool-args")?.textContent).toContain("pattern");
  });

  it("falls back to String() when args is a circular object", () => {
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    const { container } = render(
      <ToolCall tool="Grep" args={cyclic as unknown as string} />,
    );
    expect(container.querySelector(".tool-call")).toBeTruthy();
  });
});

describe("ToolCall — generic header summary + file path extraction", () => {
  it("summarizes an array args as [n] and offers an expandable args tree", () => {
    const { container } = render(<ToolCall tool="Grep" args={JSON.stringify([1, 2, 3])} />);
    expect(container.querySelector(".tool-args")?.textContent).toContain("[3]");
    const argsToggle = container.querySelector(".tool-args-toggle") as HTMLButtonElement;
    expect(argsToggle).toBeTruthy();
    fireEvent.click(argsToggle);
    expect(container.querySelector(".tool-args-tree")).toBeTruthy();
  });

  it("summarizes an empty object as {}", () => {
    const { container } = render(<ToolCall tool="Grep" args="{}" />);
    expect(container.querySelector(".tool-args")?.textContent).toBe("{}");
    // empty object → not expandable
    expect(container.querySelector(".tool-args-toggle")).toBeNull();
  });

  it("renders a numeric value via String() in the summary", () => {
    const { container } = render(<ToolCall tool="Grep" args={JSON.stringify({ count: 5 })} />);
    expect(container.querySelector(".tool-args")?.textContent).toContain("count: 5");
  });

  it("offers expandable args for a multi-key file tool (parsedFilePath ? 1 : 0 arm)", () => {
    const { container } = render(
      <ToolCall tool="Read" args={JSON.stringify({ file_path: "/x.ts", foo: "bar" })} />,
    );
    const toggleBtn = container.querySelector(".tool-args-toggle") as HTMLButtonElement;
    fireEvent.click(toggleBtn);
    expect(container.querySelector('[data-testid="jsonnode"]')?.textContent).toContain("foo");
    // clicking the args section itself (not the button) exercises its stopPropagation handler
    fireEvent.click(container.querySelector(".tool-args-section") as HTMLElement);
  });

  it("renders nested object / null / number / long-string values in the summary", () => {
    const { container } = render(
      <ToolCall
        tool="Grep"
        args={JSON.stringify({ a: { x: 1 }, b: null, c: 5, d: "y".repeat(60), e: "short", f: "x" })}
      />,
    );
    const summary = container.querySelector(".tool-args")?.textContent ?? "";
    expect(summary).toContain("{…"); // nested object → {…}
    expect(summary).toContain("null");
    // only top-2 preferred entries render + rest count
    expect(summary).toContain("+4");
  });

  it("extracts file_path / notebook_path / AbsolutePath into the header", () => {
    const { container } = render(
      <ToolCall tool="Read" args={JSON.stringify({ notebook_path: "/nb.ipynb" })} />,
    );
    expect(container.querySelector(".tool-args")?.textContent).toContain("/nb.ipynb");
  });

  it("strips agy internal toolAction/toolSummary keys from rendered args", () => {
    const { container } = render(
      <ToolCall tool="Grep" args={JSON.stringify({ pattern: "x", toolAction: "run", toolSummary: "s" })} />,
    );
    const tree = container.querySelector(".tool-args-toggle") as HTMLButtonElement;
    fireEvent.click(tree);
    const node = container.querySelector('[data-testid="jsonnode"]')?.textContent ?? "";
    expect(node).toContain("pattern");
    expect(node).not.toContain("toolAction");
    expect(node).not.toContain("toolSummary");
  });

  it("is clickable for a raw-string file-path args and fires onFileClick", () => {
    const onFileClick = vi.fn();
    const { container } = render(<ToolCall tool="Read" args="/raw/path.ts" onFileClick={onFileClick} />);
    expect(container.querySelector(".tool-call.clickable")).toBeTruthy();
    fireEvent.click(container.querySelector(".tool-call") as HTMLElement);
    expect(onFileClick).toHaveBeenCalledWith("/raw/path.ts", undefined);
  });
});

describe("ToolCall — header expand button (overflow)", () => {
  it("reveals the expand toggle when args overflow and toggles expansion", () => {
    withOverflow(() => {
      const { container } = render(<ToolCall tool="Read" args={JSON.stringify({ file_path: "/x.ts" })} />);
      const btn = container.querySelector(".tool-args-expand-btn") as HTMLButtonElement;
      expect(btn).toBeTruthy();
      fireEvent.click(btn); // headerExpanded → true
      expect(container.querySelector(".tool-args-expanded")).toBeTruthy();
      fireEvent.click(btn); // back to false
      expect(container.querySelector(".tool-args-expanded")).toBeNull();
    });
  });
});

describe("ToolCall — Edit / MultiEdit diffs", () => {
  it("renders single-edit diff and fires onViewDiff from the panel button", () => {
    const onViewDiff = vi.fn();
    const { container } = render(
      <ToolCall
        tool="Edit"
        args={JSON.stringify({ file_path: "/a.ts", old_string: "foo", new_string: "bar" })}
        onViewDiff={onViewDiff}
      />,
    );
    expect(container.textContent).toContain("- foo");
    expect(container.textContent).toContain("+ bar");
    const panelBtn = container.querySelector(".diff-panel-btn") as HTMLButtonElement;
    fireEvent.click(panelBtn);
    expect(onViewDiff).toHaveBeenCalledWith("/a.ts", "foo", "bar");
  });

  it("toggles an InlineDiff closed and open", () => {
    const { container } = render(
      <ToolCall tool="Edit" args={JSON.stringify({ file_path: "/a.ts", old_string: "foo", new_string: "bar" })} />,
    );
    const diffToggle = container.querySelector(".diff-toggle") as HTMLButtonElement;
    expect(container.querySelector(".diff-content")).toBeTruthy(); // open by default
    fireEvent.click(diffToggle);
    expect(container.querySelector(".diff-content")).toBeNull();
    fireEvent.click(diffToggle);
    expect(container.querySelector(".diff-content")).toBeTruthy();
  });

  it("renders MultiEdit with multiple edits and filters malformed edit entries", () => {
    const { container } = render(
      <ToolCall
        tool="MultiEdit"
        args={JSON.stringify({
          file_path: "/m.ts",
          edits: [
            { old_string: "o1", new_string: "n1" },
            { bad: true } as unknown as { old_string: string; new_string: string },
            { old_string: "o2", new_string: "n2" },
          ],
        })}
      />,
    );
    expect(container.querySelectorAll(".diff-content")).toHaveLength(2);
  });

  it("renders Edit with non-JSON args without crashing (parseEditArgs null)", () => {
    const { container } = render(<ToolCall tool="Edit" args="not json" />);
    expect(container.querySelector(".tool-call")).toBeTruthy();
    expect(container.querySelector(".edit-diff-list")).toBeNull();
  });

  it("treats Edit args missing file_path as non-edit (parseEditArgs null)", () => {
    const { container } = render(
      <ToolCall tool="Edit" args={JSON.stringify({ old_string: "a", new_string: "b" })} />,
    );
    expect(container.querySelector(".edit-diff-list")).toBeNull();
  });

  it("filters non-object edit entries in MultiEdit", () => {
    const { container } = render(
      <ToolCall
        tool="MultiEdit"
        args={JSON.stringify({
          file_path: "/m.ts",
          edits: [
            42 as unknown as { old_string: string; new_string: string },
            { old_string: "o", new_string: "n" },
          ],
        })}
      />,
    );
    expect(container.querySelectorAll(".diff-content")).toHaveLength(1);
  });

  it("treats MultiEdit whose edits all filter out as non-edit", () => {
    const { container } = render(
      <ToolCall
        tool="MultiEdit"
        args={JSON.stringify({ file_path: "/m.ts", edits: [{ bad: true }] })}
      />,
    );
    expect(container.querySelector(".edit-diff-list")).toBeNull();
  });

  it("renders an InlineDiff with an empty old_string", () => {
    const { container } = render(
      <ToolCall tool="Edit" args={JSON.stringify({ file_path: "/a.ts", old_string: "", new_string: "bar" })} />,
    );
    expect(container.textContent).toContain("+ bar");
  });

  it("renders an InlineDiff with an empty new_string", () => {
    const { container } = render(
      <ToolCall tool="Edit" args={JSON.stringify({ file_path: "/a.ts", old_string: "foo", new_string: "" })} />,
    );
    expect(container.textContent).toContain("- foo");
  });
});

describe("ToolCall — Bash tool", () => {
  it("parses agy CommandLine/Cwd shape and shows description", () => {
    const { container } = render(
      <ToolCall tool="run_command" args={JSON.stringify({ CommandLine: "ls", Cwd: "/tmp" })} />,
    );
    expect(container.querySelector(".bash-command")?.textContent).toBe("ls");
    expect(container.querySelector(".bash-description")?.textContent).toBe("/tmp");
    expect(container.querySelector(".tool-name")?.textContent).toBe("run_command");
  });

  it("renames execute_command to Bash in the header", () => {
    const { container } = render(
      <ToolCall tool="execute_command" args={JSON.stringify({ command: "echo hi" })} />,
    );
    expect(container.querySelector(".tool-name")?.textContent).toBe("Bash");
  });

  it("parses python-dict bash args", () => {
    const { container } = render(<ToolCall tool="Bash" args="{'command': 'git status'}" />);
    expect(container.querySelector(".bash-command")?.textContent).toBe("git status");
  });

  it("collapses long bash output and expands on click", () => {
    const long = Array.from({ length: 10 }, (_, i) => `line${i}`).join("\n");
    const { container } = render(
      <ToolCall tool="Bash" args={JSON.stringify({ command: "ls" })} result={long} />,
    );
    expect(container.querySelector(".bash-result-collapsed")).toBeTruthy();
    const expand = container.querySelector(".bash-result-expand") as HTMLButtonElement;
    fireEvent.click(expand);
    expect(container.querySelector(".bash-result-collapsed")).toBeNull();
  });

  it("moves Codex metadata below output and drops a bare result through unchanged", () => {
    const { container } = render(
      <ToolCall tool="Bash" args={JSON.stringify({ command: "ls" })} result="plain output" />,
    );
    expect(container.querySelector(".bash-result-content")?.textContent).toContain("plain output");
  });

  it("returns just the metadata when a Codex result has no output after the label", () => {
    const result = [
      "Chunk ID: 1e7ac9",
      "Wall time: 0.0000 seconds",
      "Process exited with code 0",
      "Original token count: 1939",
      "Output:",
    ].join("\n");
    const { container } = render(
      <ToolCall tool="Bash" args={JSON.stringify({ command: "ls" })} result={result} />,
    );
    const text = container.querySelector(".bash-result-content")?.textContent ?? "";
    expect(text).toContain("Chunk ID");
    expect(text).not.toContain("Output:");
  });

  it("falls back to the raw args when bash args have an unquoted-key dict (both parses fail)", () => {
    const { container } = render(<ToolCall tool="Bash" args="{command: 'ls'}" />);
    expect(container.querySelector(".bash-command")?.textContent).toBe("{command: 'ls'}");
  });

  it("ignores a non-string command field and falls back to raw args", () => {
    const { container } = render(<ToolCall tool="Bash" args={JSON.stringify({ command: 123 })} />);
    expect(container.querySelector(".bash-command")?.textContent).toBe(JSON.stringify({ command: 123 }));
  });

  it("falls back to raw args when bash args parse to a non-object (JSON string)", () => {
    const { container } = render(<ToolCall tool="Bash" args={JSON.stringify("just a string")} />);
    expect(container.querySelector(".bash-command")?.textContent).toBe(JSON.stringify("just a string"));
  });
});

describe("ToolCall — Agent / Skill tools", () => {
  it("parses python-dict and regex-fallback agent args, toggles the prompt", () => {
    const { container } = render(
      <ToolCall tool="Agent" args={"{'description': 'go', 'subagent_type': 'claude', 'prompt': 'do it'}"} />,
    );
    expect(container.querySelector(".tool-name")?.textContent).toBe("claude");
    expect(container.querySelector(".agent-description")?.textContent).toBe("go");
    const promptToggle = container.querySelector(".agent-prompt-toggle") as HTMLButtonElement;
    fireEvent.click(promptToggle);
    expect(container.querySelector(".agent-prompt-content")?.textContent).toBe("do it");
  });

  it("falls back to regex extraction for unparseable agent args", () => {
    const { container } = render(
      <ToolCall tool="Task" args={'weird blob "description": "scan" rest "subagent_type": "x"'} />,
    );
    expect(container.querySelector(".agent-description")?.textContent).toBe("scan");
  });

  it("renders the default sub-agent label when agent args are wholly unparseable", () => {
    const { container } = render(<ToolCall tool="Agent" args="totally unstructured no quotes here" />);
    expect(container.querySelector(".agent-description")?.textContent).toBe("toolCall.subAgent");
    expect(container.querySelector(".agent-prompt-section")).toBeNull();
  });

  it("shows the spawn-failed banner on full-history fork error", () => {
    const { container } = render(
      <ToolCall
        tool="Agent"
        args={JSON.stringify({ description: "x" })}
        result="Full-history forked agents inherit the parent agent type, model, and reasoning effort"
      />,
    );
    expect(container.querySelector(".agent-tool-call-failed")).toBeTruthy();
  });

  it("clicks the skill name to open SKILL.md and toggles the prompt", () => {
    const onFileClick = vi.fn();
    const { container } = render(
      <ToolCall tool="Skill" args={JSON.stringify({ skill: "deploy", prompt: "run it", description: "d" })} onFileClick={onFileClick} />,
    );
    const nameBtn = container.querySelector(".skill-name-btn") as HTMLButtonElement;
    fireEvent.click(nameBtn);
    expect(onFileClick).toHaveBeenCalledWith(".claude/skills/deploy/SKILL.md");
    const promptToggle = container.querySelector(".agent-prompt-toggle") as HTMLButtonElement;
    fireEvent.click(promptToggle);
    expect(container.querySelector(".agent-prompt-content")?.textContent).toBe("run it");
  });

  it("falls back to regex for unparseable skill args", () => {
    const { container } = render(<ToolCall tool="Skill" args={'blob "skill": "deploy" "args": "x"'} />);
    expect(container.querySelector(".skill-name-btn")?.textContent).toBe("deploy");
  });

  it("renders the raw tool name when skill args have no skill field", () => {
    const { container } = render(<ToolCall tool="Skill" args="{}" />);
    expect(container.querySelector(".tool-name")?.textContent).toBe("Skill");
  });

  it("renders a Skill tool result", () => {
    const { container } = render(
      <ToolCall tool="Skill" args={JSON.stringify({ skill: "deploy" })} result="done" />,
    );
    expect(container.querySelector(".tool-result-inline")).toBeTruthy();
  });
});

describe("ToolCall — apply_patch", () => {
  const patch = [
    "*** Begin Patch",
    "*** Update File: frontend/src/App.tsx",
    "@@",
    "-old line",
    "+new line",
    "context line",
    "*** Delete File: gone.ts",
    "-removed",
    "*** End Patch",
  ].join("\n");

  it("renders add/update/delete actions, stats, and per-file click", () => {
    const onFileClick = vi.fn();
    const { container } = render(<ToolCall tool="apply_patch" args={patch} onFileClick={onFileClick} />);
    expect(container.textContent).toContain("2 files");
    expect(container.textContent).toContain("delete gone.ts");
    const pathBtn = container.querySelectorAll(".apply-patch-path")[1] as HTMLButtonElement;
    fireEvent.click(pathBtn);
    expect(onFileClick).toHaveBeenCalledWith("gone.ts");
  });

  it("collapses and re-expands the summary", () => {
    const { container } = render(<ToolCall tool="apply_patch" args={patch} />);
    const summaryToggle = container.querySelector(".apply-patch-summary-toggle") as HTMLButtonElement;
    fireEvent.click(summaryToggle);
    expect(container.querySelector(".apply-patch-files")).toBeNull();
    fireEvent.click(summaryToggle);
    expect(container.querySelector(".apply-patch-files")).toBeTruthy();
  });

  it("renders raw text when args has no patch markers", () => {
    const { container } = render(<ToolCall tool="apply_patch" args="just some text" />);
    expect(container.querySelector(".tool-result-content")?.textContent).toBe("just some text");
  });

  it("renders raw text when the patch body has markers but no files", () => {
    const { container } = render(<ToolCall tool="apply_patch" args="*** Begin Patch\n*** End Patch" />);
    expect(container.querySelector(".tool-result-content")?.textContent).toContain("*** Begin Patch");
  });

  it("renders a single-file patch with the path as the label", () => {
    const patch = "*** Begin Patch\n*** Add File: lone.ts\n+line\n*** End Patch";
    const { container } = render(<ToolCall tool="apply_patch" args={patch} result="ok" />);
    expect(container.querySelector(".tool-args")?.textContent).toBe("lone.ts");
    expect(container.querySelector(".tool-result-inline, .tool-result-block")).toBeTruthy();
  });

  it("renders an empty patch line as a space and a no-patch result block", () => {
    // the blank line between markers becomes a context line with empty text
    const patch = "*** Begin Patch\n*** Add File: lone.ts\n+line\n\n*** End Patch";
    const { container } = render(<ToolCall tool="apply_patch" args={patch} />);
    const lines = [...container.querySelectorAll(".apply-patch-line")].map((l) => l.textContent);
    expect(lines).toContain(" ");
  });

  it("renders a result under a no-patch apply_patch card", () => {
    const { container } = render(<ToolCall tool="apply_patch" args="not a patch" result="ok" />);
    expect(container.querySelector(".tool-result-inline")?.textContent).toBe("ok");
  });

  it("falls back to the raw args when apply_patch value is a JSON object without a string value", () => {
    const { container } = render(<ToolCall tool="apply_patch" args={JSON.stringify({ foo: "bar" })} />);
    expect(container.querySelector(".tool-result-content")?.textContent).toContain("foo");
  });
});

describe("ToolCall — Canvas tool", () => {
  it("renders the iframe when a board id is present and collapses on click", () => {
    const { container } = render(
      <ToolCall tool="canvas" args={JSON.stringify({ action: "create" })} result={JSON.stringify({ id: "board-1" })} />,
    );
    expect(container.querySelector(".canvas-inline-iframe")?.getAttribute("src")).toContain("board=board-1");
    expect(container.textContent).toContain("create");
    const btn = container.querySelector(".tool-args-expand-btn") as HTMLButtonElement;
    fireEvent.click(btn);
    expect(container.querySelector(".canvas-inline-iframe")).toBeNull();
    fireEvent.click(btn);
    expect(container.querySelector(".canvas-inline-iframe")).toBeTruthy();
  });

  it("renders no iframe when the result is not a JSON object", () => {
    const { container } = render(<ToolCall tool="canvas" args={JSON.stringify({})} result="notjson" />);
    expect(container.querySelector(".canvas-inline-iframe")).toBeNull();
  });

  it("renders no iframe when the result object has no id", () => {
    const { container } = render(<ToolCall tool="canvas" args={JSON.stringify({})} result={JSON.stringify({ foo: 1 })} />);
    expect(container.querySelector(".canvas-inline-iframe")).toBeNull();
    expect(container.textContent).toContain("boardId=null");
  });

  it("renders without iframe when there is no board id", () => {
    const { container } = render(<ToolCall tool="mcp__canvas__canvas" args={JSON.stringify({})} />);
    expect(container.querySelector(".canvas-inline-iframe")).toBeNull();
    expect(container.textContent).toContain("boardId=null");
  });
});

describe("ToolCall — inline open_file_panel", () => {
  it("resolves the path from the result JSON and renders the inline panel", () => {
    const { container } = render(
      <ToolCall
        tool="mcp__files__open_file_panel"
        args={JSON.stringify({ mode: "inline", path: "/raw.ts", start_line: "10", end_line: 20, selected_start: 12, selected_end: "bad" })}
        result={JSON.stringify({ panel: { path: "/abs/raw.ts" } })}
      />,
    );
    expect(container.querySelector(".inline-file-panel")).toBeTruthy();
    expect(container.querySelector('[data-testid="fileviewer"]')).toBeTruthy();
  });

  it("builds a select range when both selected_start and selected_end are valid numbers", () => {
    const { container } = render(
      <ToolCall
        tool="mcp__files__open_file_panel"
        args={JSON.stringify({ mode: "inline", path: "/raw.ts", selected_start: 5, selected_end: 8 })}
      />,
    );
    expect(container.querySelector(".inline-file-panel")).toBeTruthy();
  });

  it("falls back to the raw arg path when the result isn't JSON", () => {
    const { container } = render(
      <ToolCall
        tool="mcp__files__open_file_panel"
        args={JSON.stringify({ mode: "inline", path: "/raw2.ts" })}
        result="not json"
      />,
    );
    expect(container.querySelector(".inline-file-panel")).toBeTruthy();
  });

  it("collapses via the embedded FileViewer onClose", () => {
    const { container } = render(
      <ToolCall tool="mcp__files__open_file_panel" args={JSON.stringify({ mode: "inline", path: "/raw3.ts" })} />,
    );
    fireEvent.click(container.querySelector('[data-testid="fv-close"]') as HTMLElement);
    expect(container.querySelector(".inline-file-collapsed")).toBeTruthy();
  });

  it("falls back to the full path when collapsing a trailing-slash path", () => {
    const { container } = render(
      <ToolCall tool="mcp__files__open_file_panel" args={JSON.stringify({ mode: "inline", path: "/dir/" })} />,
    );
    fireEvent.click(container.querySelector('[data-testid="fv-close"]') as HTMLElement);
    expect(container.querySelector(".inline-file-collapsed .btn-small")?.textContent).toBe("/dir/");
  });

  it("falls through when args is not a JSON object", () => {
    const { container } = render(
      <ToolCall tool="mcp__files__open_file_panel" args="notjson" />,
    );
    expect(container.querySelector(".inline-file-panel")).toBeNull();
    expect(container.querySelector(".tool-call")).toBeTruthy();
  });

  it("falls through to a normal card when mode is not inline", () => {
    const { container } = render(
      <ToolCall tool="mcp__files__open_file_panel" args={JSON.stringify({ mode: "panel", path: "/x.ts" })} />,
    );
    expect(container.querySelector(".inline-file-panel")).toBeNull();
    expect(container.querySelector(".tool-call")).toBeTruthy();
  });

  it("falls through when there is no path at all", () => {
    const { container } = render(
      <ToolCall tool="mcp__files__open_file_panel" args={JSON.stringify({ mode: "inline" })} />,
    );
    expect(container.querySelector(".inline-file-panel")).toBeNull();
  });
});

describe("ToolCall — inline view_image", () => {
  it("resolves the image path from the result panel and renders the frame", () => {
    const { container } = render(
      <ToolCall
        tool="view_image"
        args={JSON.stringify({ path: "/raw.png" })}
        result={JSON.stringify({ panel: { path: "/abs/raw.png" } })}
      />,
    );
    const img = container.querySelector(".inline-image-frame img") as HTMLImageElement;
    expect(img.getAttribute("src")).toContain(encodeURIComponent("/abs/raw.png"));
  });

  it("collapses and shows an error when the image fails to load", () => {
    const { container } = render(
      <ToolCall tool="view_image" args={JSON.stringify({ path: "/raw.png" })} />,
    );
    fireEvent.error(container.querySelector(".inline-image-frame img") as HTMLImageElement);
    expect(container.querySelector(".inline-image-error")).toBeTruthy();
    // collapse path
    const { container: c2 } = render(
      <ToolCall tool="view_image" args={JSON.stringify({ path: "/raw.png" })} />,
    );
    fireEvent.click(c2.querySelector(".btn-small") as HTMLButtonElement);
    expect(c2.querySelector(".inline-image-collapsed")).toBeTruthy();
  });

  it("falls through when there is no image path", () => {
    const { container } = render(<ToolCall tool="view_image" args={JSON.stringify({})} />);
    expect(container.querySelector(".inline-image-viewer")).toBeNull();
    expect(container.querySelector(".tool-call")).toBeTruthy();
  });
});

describe("ToolCall — inline open_config_panel", () => {
  function ctxOf(overrides: Record<string, unknown> = {}) {
    return {
      open: vi.fn(),
      activeInlineId: null as string | null,
      claimInline: vi.fn(),
      releaseInline: vi.fn(),
      ...overrides,
    };
  }

  it("renders the active panel when no context is present (open disabled)", () => {
    const { container } = render(
      <ToolCall tool="mcp__cfg__open_config_panel" args={JSON.stringify({ capability_id: "cap:1" })} />,
    );
    expect(container.querySelector(".inline-config-panel")).toBeTruthy();
    expect((container.querySelector(".inline-config-panel-actions .btn-small") as HTMLButtonElement).disabled).toBe(true);
  });

  it("renders the closed marker when superseded by a newer inline panel", () => {
    const ctx = ctxOf({ activeInlineId: "other-id" });
    const { container } = render(
      <ConfigPanelContext.Provider value={ctx}>
        <ToolCall tool="mcp__cfg__open_config_panel" args={JSON.stringify({ capability_id: "cap:1" })} />
      </ConfigPanelContext.Provider>,
    );
    expect(container.querySelector(".inline-config-closed")).toBeTruthy();
    expect(container.querySelector(".inline-config-panel")).toBeNull();
  });

  it("opens in panel and collapses when it is the active inline", () => {
    const claimed = { id: "" };
    const ctx = ctxOf({
      claimInline: (id: string) => {
        claimed.id = id;
      },
    });
    const { rerender, container } = render(
      <ConfigPanelContext.Provider value={ctx}>
        <ToolCall tool="mcp__cfg__open_config_panel" args={JSON.stringify({ capability_id: "cap:1", scope: "global", cwd: "/p" })} />
      </ConfigPanelContext.Provider>,
    );
    // promote to active using the id claimed on mount
    const active = ctxOf({ activeInlineId: claimed.id, open: ctx.open });
    rerender(
      <ConfigPanelContext.Provider value={active}>
        <ToolCall tool="mcp__cfg__open_config_panel" args={JSON.stringify({ capability_id: "cap:1", scope: "global", cwd: "/p" })} />
      </ConfigPanelContext.Provider>,
    );
    const buttons = container.querySelectorAll(".inline-config-panel-actions .btn-small");
    fireEvent.click(buttons[0]); // Open in panel
    expect(active.open).toHaveBeenCalledWith({ capability_id: "cap:1", scope: "global", cwd: "/p" });
    expect(container.textContent).toContain("Opened");
    fireEvent.click(buttons[1]); // Collapse
    expect(container.querySelector(".inline-file-collapsed")).toBeTruthy();
    // re-expand from the collapsed button
    fireEvent.click(container.querySelector(".inline-file-collapsed .btn-small") as HTMLButtonElement);
    expect(container.querySelector(".inline-config-panel")).toBeTruthy();
  });

  it("resolves scope/cwd from the result JSON", () => {
    const { container } = render(
      <ToolCall
        tool="mcp__cfg__open_config_panel"
        args={JSON.stringify({ capability_id: "cap:1" })}
        result={JSON.stringify({ panel: { scope: "project", cwd: "/resolved" } })}
      />,
    );
    expect(container.textContent).toContain("project");
  });

  it("resolves a global scope from the result JSON", () => {
    const { container } = render(
      <ToolCall
        tool="mcp__cfg__open_config_panel"
        args={JSON.stringify({ capability_id: "cap:1", scope: "project" })}
        result={JSON.stringify({ panel: { scope: "global", cwd: "/g" } })}
      />,
    );
    expect(container.textContent).toContain("global");
  });

  it("falls through when capability_id is missing", () => {
    const { container } = render(
      <ToolCall tool="mcp__cfg__open_config_panel" args={JSON.stringify({})} />,
    );
    expect(container.querySelector(".inline-config-panel")).toBeNull();
  });
});

describe("ToolResult — rendering modes", () => {
  it("renders a short inline result", () => {
    const { container } = render(<ToolCall tool="Grep" args={JSON.stringify({ pattern: "x" })} result="ok" />);
    expect(container.querySelector(".tool-result-inline")).toBeTruthy();
    expect(container.querySelector(".tool-result-text")?.textContent).toBe("ok");
  });

  it("renders nothing when the cleaned result is empty", () => {
    const { container } = render(<ToolCall tool="Grep" args={JSON.stringify({ pattern: "x" })} result="📎" />);
    expect(container.querySelector(".tool-result-inline, .tool-result-block")).toBeNull();
  });

  it("renders a long text result in a collapsible block with a char count", () => {
    const long = "x".repeat(1100);
    const { container } = render(<ToolCall tool="Grep" args={JSON.stringify({ pattern: "x" })} result={long} />);
    const toggleBtn = container.querySelector(".tool-result-toggle") as HTMLButtonElement;
    expect(toggleBtn.textContent).toContain("1.1k chars");
    fireEvent.click(toggleBtn);
    expect(container.querySelector(".tool-result-content")).toBeTruthy();
  });

  it("renders a JSON result with the json tree tag and viewer", () => {
    const { container } = render(
      <ToolCall tool="Grep" args={JSON.stringify({ pattern: "x" })} result={JSON.stringify({ hello: "world" })} />,
    );
    const toggleBtn = container.querySelector(".tool-result-toggle") as HTMLButtonElement;
    fireEvent.click(toggleBtn);
    expect(container.querySelector('[data-testid="jsonnode"]')?.textContent).toContain("hello");
  });

  it("renders Read file content through the Monaco viewer when expanded", () => {
    // Leading newline (trimmed during cleaning) keeps the first line number
    // intact — the emoji-strip regex otherwise eats leading digits (\p{Emoji}).
    const fileContent = "\n1\thello\n2\tworld\n3\tmore";
    const { container } = render(
      <ToolCall tool="Read" args={JSON.stringify({ file_path: "/a.ts" })} result={fileContent} />,
    );
    // line-range chip is rendered from the result
    expect(container.querySelector(".tool-line-range")?.textContent).toContain("L1");
    const toggleBtn = container.querySelector(".tool-result-toggle") as HTMLButtonElement;
    fireEvent.click(toggleBtn);
    expect(container.querySelector('[data-testid="monaco"]')).toBeTruthy();
  });

  it("renders a Read result that isn't line-numbered as a <pre> fallback", () => {
    const long = "y".repeat(200);
    const { container } = render(
      <ToolCall tool="Read" args={JSON.stringify({ file_path: "/b.ts" })} result={long} />,
    );
    const toggleBtn = container.querySelector(".tool-result-toggle") as HTMLButtonElement;
    fireEvent.click(toggleBtn);
    expect(container.querySelector(".tool-result-content.file-content")).toBeTruthy();
  });

  it("parses mixed numbered + note lines and resolves language from the path", () => {
    const fileContent = "\n1\thello\n2\tworld\nFile has more lines.";
    const argsVariants = [
      JSON.stringify({ file_path: "/x/Dockerfile" }), // dockerfile
      JSON.stringify({ file_path: "/a.unknownext" }), // unknown ext → plaintext
      "/raw-string-path", // raw args → filePath undefined → plaintext
    ];
    for (const args of argsVariants) {
      const { container } = render(<ToolCall tool="Read" args={args} result={fileContent} />);
      const toggleBtn = container.querySelector(".tool-result-toggle") as HTMLButtonElement;
      fireEvent.click(toggleBtn);
      // Monaco stub renders regardless of language; just confirm the viewer mounts
      expect(container.querySelector('[data-testid="monaco"]')).toBeTruthy();
    }
  });
});

describe("ToolResult — webReader cards", () => {
  const entries = [
    {
      text: {
        title: "Example",
        description: "desc",
        url: "https://www.example.com/page",
        content: "x".repeat(250),
      },
    },
    { text: "plain string content" },
  ];

  it("parses webReader_result_summary into cards, toggles, expands content, opens url", () => {
    const { container } = render(
      <ToolCall
        tool="webReader"
        args={JSON.stringify({ url: "https://x" })}
        result={`webReader_result_summary: ${JSON.stringify(entries)}`}
      />,
    );
    const toggleBtn = container.querySelector(".tool-result-toggle") as HTMLButtonElement;
    expect(toggleBtn.textContent).toContain("2 results");
    fireEvent.click(toggleBtn);
    expect(container.querySelector(".webreader-results")).toBeTruthy();
    expect(container.querySelector(".webreader-card-preview")?.textContent).toContain("…"); // truncated preview

    // expand the first card to reveal full content
    const header = container.querySelectorAll(".webreader-card-header")[0] as HTMLElement;
    fireEvent.click(header);
    expect(container.querySelector(".webreader-card-content")).toBeTruthy();

    // url click → external link
    const link = container.querySelector(".webreader-card-url") as HTMLAnchorElement;
    fireEvent.click(link);
    expect(openExternalLinkMock).toHaveBeenCalledWith("https://www.example.com/page");
  });

  it("renders an empty result list when webReader JSON is malformed", () => {
    const { container } = render(
      <ToolCall tool="webReader" args={JSON.stringify({ url: "https://x" })} result="webReader_result_summary: notjson" />,
    );
    // entries null/empty → falls through to normal text result rendering
    expect(container.querySelector(".webreader-results")).toBeNull();
  });

  it("exercises the deep webReader parse branches (non-array, primitive item, stringified-json, raw string, empty)", () => {
    // non-array JSON object → null → falls through
    let r = render(<ToolCall tool="webReader" args={JSON.stringify({ url: "https://x" })} result={`webReader_result_summary: {}`} />);
    expect(r.container.querySelector(".webreader-results")).toBeNull();
    r.unmount();

    // empty array → entries length 0 → falls through
    r = render(<ToolCall tool="webReader" args={JSON.stringify({ url: "https://x" })} result={`webReader_result_summary: []`} />);
    expect(r.container.querySelector(".webreader-results")).toBeNull();
    r.unmount();

    // primitive item → {} entry; non-object/non-string text → {} entry;
    // stringified-json text → structured entry; raw non-json string → content entry;
    // structured text with non-string fields → undefined-coerced entry
    const entries = [
      123 as unknown as Record<string, unknown>,
      { text: 456 },
      { text: JSON.stringify({ title: "Str", url: "https://e.com", content: "c" }) },
      { text: "not json string" },
      { text: { title: 9, description: null, url: {}, content: [] as unknown as string } },
    ];
    r = render(<ToolCall tool="webReader" args={JSON.stringify({ url: "https://x" })} result={`webReader_result_summary: ${JSON.stringify(entries)}`} />);
    const toggleBtn = r.container.querySelector(".tool-result-toggle") as HTMLButtonElement;
    fireEvent.click(toggleBtn);
    expect(r.container.querySelectorAll(".webreader-card")).toHaveLength(5);
    expect(r.container.textContent).toContain("Str");
    r.unmount();

    // prefix + malformed array JSON → outer catch → null → falls through
    r = render(<ToolCall tool="webReader" args={JSON.stringify({ url: "https://x" })} result={`webReader_result_summary: [invalid`} />);
    expect(r.container.querySelector(".webreader-results")).toBeNull();
    r.unmount();

    // no prefix at all → match null → falls through
    r = render(<ToolCall tool="webReader" args={JSON.stringify({ url: "https://x" })} result="plain text no prefix" />);
    expect(r.container.querySelector(".webreader-results")).toBeNull();
    r.unmount();
  });

  it("parses an MCP-namespaced webReader tool", () => {
    const { container } = render(
      <ToolCall
        tool="mcp__web__webReader"
        args={JSON.stringify({ url: "https://x" })}
        result={`webReader_result_summary: ${JSON.stringify([{ text: { content: "c" } }])}`}
      />,
    );
    const toggleBtn = container.querySelector(".tool-result-toggle") as HTMLButtonElement;
    fireEvent.click(toggleBtn);
    expect(container.querySelector(".webreader-card")).toBeTruthy();
  });
});

describe("ToolResult — get_requirements cards", () => {
  it("renders requirement matches, source prompt, and file links; toggles open", () => {
    const onFileClick = vi.fn();
    const payload = {
      success: false,
      count: 2,
      matches: [
        {
          text: "do the thing",
          kind: "explicit",
          source: "user",
          polarity: "positive",
          strength: "high",
          cwd: "/p",
          edited_files: ["backend/a.py"],
          source_text: "original prompt",
        },
      ],
      freshness: { fresh: true },
    };
    const { container } = render(
      <ToolCall
        tool="mcp__get_requirements__get_requirements"
        args={JSON.stringify({ q: "x" })}
        result={JSON.stringify(payload)}
        onFileClick={onFileClick}
      />,
    );
    const toggleBtn = container.querySelector(".tool-result-toggle") as HTMLButtonElement;
    fireEvent.click(toggleBtn);
    expect(container.querySelector(".requirements-status-bad")?.textContent).toBe("failed");
    expect(container.querySelector(".requirements-match-text")?.textContent).toContain("do the thing");

    // file link
    const fileLink = container.querySelector(".requirements-file-link") as HTMLButtonElement;
    fireEvent.click(fileLink);
    expect(onFileClick).toHaveBeenCalledWith("backend/a.py");

    // source prompt details
    expect(container.querySelector(".requirements-source summary")?.textContent).toBe("source prompt");
  });

  it("renders the freshness label from unhandled_prompts when stale", () => {
    const payload = { matches: [{ text: "a" }], freshness: { fresh: false, unhandled_prompts: 5 } };
    const { container } = render(
      <ToolCall tool="get_requirements" args={JSON.stringify({ q: "x" })} result={JSON.stringify(payload)} />,
    );
    expect(container.querySelector(".tool-result-toggle")?.textContent).toContain("stale by 5");
  });

  it("renders stale with no 'by N' when unhandled_prompts is absent", () => {
    const payload = { matches: [{ text: "a" }], freshness: { fresh: false } };
    const { container } = render(
      <ToolCall tool="get_requirements" args={JSON.stringify({ q: "x" })} result={JSON.stringify(payload)} />,
    );
    expect(container.querySelector(".tool-result-toggle")?.textContent).toContain("stale");
    expect(container.querySelector(".tool-result-toggle")?.textContent).not.toContain("by");
  });

  it("falls back to matches.length when count is absent", () => {
    const payload = { matches: [{ text: "a" }, { text: "b" }] };
    const { container } = render(
      <ToolCall tool="get_requirements" args={JSON.stringify({ q: "x" })} result={JSON.stringify(payload)} />,
    );
    expect(container.querySelector(".tool-result-toggle")?.textContent).toContain("2 matches");
  });

  it("falls through when the result is not a requirements object", () => {
    const { container } = render(
      <ToolCall tool="get_requirements" args={JSON.stringify({ q: "x" })} result="short ok" />,
    );
    expect(container.querySelector(".tool-result-inline")).toBeTruthy();
  });

  it("renders matches with no freshness label when freshness is absent", () => {
    const payload = { count: 1, matches: [{ text: "t" }] };
    const { container } = render(
      <ToolCall tool="get_requirements" args={JSON.stringify({ q: "x" })} result={JSON.stringify(payload)} />,
    );
    const toggleBtn = container.querySelector(".tool-result-toggle") as HTMLButtonElement;
    expect(toggleBtn.textContent).toContain("1 matches");
    expect(toggleBtn.textContent).not.toContain("fresh");
  });

  it("falls through when a requirements result has no matches array", () => {
    const { container } = render(
      <ToolCall tool="get_requirements" args={JSON.stringify({ q: "x" })} result={JSON.stringify({ success: true })} />,
    );
    expect(container.querySelector(".requirements-results")).toBeNull();
    expect(container.querySelector(".tool-result-block")).toBeTruthy();
  });
});

describe("ToolCall — MCP display name + generic focus", () => {
  it("renders a server/toolName display name for MCP tools", () => {
    const { container } = render(
      <ToolCall tool="mcp__github__create_pr" args={JSON.stringify({ title: "t" })} />,
    );
    expect(container.querySelector(".tool-name")?.textContent).toBe("github/create_pr");
  });

  it("renders a non-paired mcp__ name as the raw tool (parseMcpName null)", () => {
    const { container } = render(<ToolCall tool="mcp__nopair" args="{}" />);
    expect(container.querySelector(".tool-name")?.textContent).toBe("mcp__nopair");
  });

  it("does not compute a Read focus for a non-Read tool", () => {
    const { container } = render(<ToolCall tool="Grep" args="{}" result="1\tx" />);
    expect(container.querySelector(".tool-line-range")).toBeNull();
  });

  it("view_image with non-object args falls through (inlineViewImage null)", () => {
    const { container } = render(<ToolCall tool="view_image" args="notjson" />);
    expect(container.querySelector(".inline-image-viewer")).toBeNull();
    expect(container.querySelector(".tool-call")).toBeTruthy();
  });

  it("open_config_panel with non-object args falls through (inlineOpenConfigPanel null)", () => {
    const { container } = render(<ToolCall tool="mcp__cfg__open_config_panel" args="notjson" />);
    expect(container.querySelector(".inline-config-panel")).toBeNull();
    expect(container.querySelector(".tool-call")).toBeTruthy();
  });

  it("collapses an inline file panel and re-expands it", () => {
    const { container } = render(
      <ToolCall tool="mcp__files__open_file_panel" args={JSON.stringify({ mode: "inline", path: "/dir/" })} />,
    );
    fireEvent.click(container.querySelector('[data-testid="fv-close"]') as HTMLElement);
    const collapsed = container.querySelector(".inline-file-collapsed") as HTMLElement;
    expect(collapsed).toBeTruthy();
    expect(collapsed.querySelector(".btn-small")?.textContent).toBe("/dir/");
  });

  it("collapses an inline image viewer via its Collapse button", () => {
    const { container } = render(<ToolCall tool="view_image" args={JSON.stringify({ path: "/x/y.png" })} />);
    fireEvent.click(container.querySelector(".btn-small") as HTMLButtonElement);
    const collapsed = container.querySelector(".inline-image-collapsed") as HTMLElement;
    expect(collapsed).toBeTruthy();
    expect(collapsed.querySelector(".btn-small")?.textContent).toBe("y.png");
  });
});
