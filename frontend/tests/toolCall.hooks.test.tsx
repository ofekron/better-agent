import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { ToolCall } from "../src/components/ToolCall";
import { sessionLinkMarker } from "../src/utils/linkifyFilePaths";

// Bootstrap i18n so `useTranslation` inside ToolCall has something to
// resolve against. Side-effecting import is intentional.
import "../src/i18n";

/** Mount `node` into a fresh container and return a same-container
 * `rerender` so consecutive renders hit the SAME React fiber position
 * (which is what triggers Rules-of-Hooks violations when a component's
 * hook count changes across renders). */
async function mount(node: React.ReactNode) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  let root: Root | null = null;
  await act(async () => {
    root = createRoot(container);
    root.render(node);
  });
  return {
    container,
    unmount: () => {
      act(() => root?.unmount());
      container.remove();
    },
    rerender: async (next: React.ReactNode) => {
      await act(async () => root?.render(next));
    },
  };
}

/** Regression suite for the "page goes blank" bug. ToolCall used to
 * call hooks AFTER conditional early returns, so the same React-keyed
 * instance switching between tools in different "hook-count buckets"
 * threw "Rendered fewer/more hooks than expected" and — with no error
 * boundary directly around it — unmounted the entire chat tree.
 *
 * The buckets in the buggy version were:
 *   - 0 hooks: `if (inline)` (open_file_panel MCP, inline mode)
 *   - 5 hooks: Agent / Bash / Skill / Canvas (returns after the
 *              `useState`/`useRef`/`useTranslation` block)
 *   - 6 hooks: Read / Write / Grep / Edit / generic MCP (falls through
 *              to a trailing `useEffect`)
 *
 * Each test rerenders the SAME container with two tools drawn from
 * different buckets and asserts that React did NOT log the hooks-count
 * error. Pre-fix, the 5↔6 and 0↔6 transitions throw and console.error
 * is called with the diagnostic string; post-fix, none of these
 * transitions changes the hook count, so the error never fires. */
describe("ToolCall — hook stability across tool prop changes", () => {
  let errorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    errorSpy.mockRestore();
  });

  function hadHooksError(): boolean {
    return errorSpy.mock.calls.some((call) =>
      call.some(
        (a) =>
          (typeof a === "string" && /Rendered (fewer|more) hooks/.test(a)) ||
          (a instanceof Error && /Rendered (fewer|more) hooks/.test(a.message)),
      ),
    );
  }

  it("does not throw 'Rendered fewer hooks' on Read → Bash rerender", async () => {
    const h = await mount(
      <ToolCall tool="Read" args={JSON.stringify({ file_path: "/x.ts" })} />,
    );
    await h.rerender(
      <ToolCall tool="Bash" args={JSON.stringify({ command: "ls -la" })} />,
    );
    expect(hadHooksError()).toBe(false);
    h.unmount();
  });

  it("does not throw 'Rendered more hooks' on Bash → Read rerender", async () => {
    const h = await mount(
      <ToolCall tool="Bash" args={JSON.stringify({ command: "echo hi" })} />,
    );
    await h.rerender(
      <ToolCall tool="Read" args={JSON.stringify({ file_path: "/y.ts" })} />,
    );
    expect(hadHooksError()).toBe(false);
    h.unmount();
  });

  it("does not throw hooks errors across Skill ↔ Edit ↔ Agent transitions", async () => {
    const h = await mount(
      <ToolCall
        tool="Skill"
        args={JSON.stringify({ skill: "demo", args: "hello" })}
      />,
    );
    await h.rerender(
      <ToolCall
        tool="Edit"
        args={JSON.stringify({
          file_path: "/a.ts",
          old_string: "foo",
          new_string: "bar",
        })}
      />,
    );
    await h.rerender(
      <ToolCall
        tool="Agent"
        args={JSON.stringify({ description: "go", prompt: "do it" })}
      />,
    );
    await h.rerender(
      <ToolCall tool="Grep" args={JSON.stringify({ pattern: "x" })} />,
    );
    expect(hadHooksError()).toBe(false);
    h.unmount();
  });

  it("does not throw hooks errors when the open_file_panel inline branch flips", async () => {
    // First render takes the `if (inline)` early-return path (which
    // used to be BEFORE any hook calls — 0 hooks bucket).
    const h = await mount(
      <ToolCall
        tool="mcp__open_file_panel__open_file_panel"
        args={JSON.stringify({ mode: "inline", path: "/some/file.ts" })}
      />,
    );
    // Second render switches to a tool that runs through the trailing
    // useEffect path (6 hooks bucket pre-fix).
    await h.rerender(
      <ToolCall tool="Read" args={JSON.stringify({ file_path: "/q.ts" })} />,
    );
    expect(hadHooksError()).toBe(false);
    h.unmount();
  });

  it("does not throw hooks errors when the view_image inline branch flips", async () => {
    const h = await mount(
      <ToolCall
        tool="view_image"
        args={JSON.stringify({ path: "/tmp/demo.png" })}
      />,
    );
    await h.rerender(
      <ToolCall tool="Read" args={JSON.stringify({ file_path: "/q.ts" })} />,
    );
    expect(hadHooksError()).toBe(false);
    h.unmount();
  });

  it("renders view_image as an inline image sourced from the raw file endpoint", async () => {
    const h = await mount(
      <ToolCall
        tool="view_image"
        args={JSON.stringify({ path: "/tmp/demo image.png" })}
      />,
    );
    const img = h.container.querySelector(".inline-image-frame img");
    expect(img).not.toBeNull();
    expect(img?.getAttribute("src")).toBe(
      "/api/file/raw?path=%2Ftmp%2Fdemo%20image.png&node_id=primary",
    );
    expect(img?.getAttribute("alt")).toBe("demo image.png");
    h.unmount();
  });
});

describe("ToolCall — apply_patch rendering", () => {
  const patch = [
    "*** Begin Patch",
    "*** Update File: frontend/src/App.tsx",
    "@@",
    "-old line",
    "+new line",
    "*** Add File: frontend/src/new.ts",
    "+export const value = 1;",
    "*** End Patch",
  ].join("\n");

  it("summarizes raw apply_patch calls", async () => {
    const h = await mount(<ToolCall tool="apply_patch" args={patch} />);

    expect(h.container.textContent).toContain("apply_patch");
    expect(h.container.textContent).toContain("2 files");
    expect(h.container.textContent).toContain("+2");
    expect(h.container.textContent).toContain("-1");
    expect(h.container.textContent).toContain("frontend/src/App.tsx, frontend/src/new.ts");
    expect(h.container.textContent).toContain("-old line");
    expect(h.container.textContent).toContain("+new line");

    h.unmount();
  });

  it("summarizes JSON-wrapped apply_patch calls", async () => {
    const h = await mount(
      <ToolCall tool="apply_patch" args={JSON.stringify({ value: patch })} />,
    );

    expect(h.container.textContent).toContain("2 files");
    expect(h.container.textContent).not.toContain("*** Begin Patch");

    h.unmount();
  });

  it("keeps long apply_patch summaries and diffs inside the card", async () => {
    const longPatch = [
      "*** Begin Patch",
      "*** Update File: /workspace/better-agent/.agents/skills/project-structure/sections/requirements/constraints.md",
      "@@",
      "-".padEnd(260, "x"),
      "+".padEnd(260, "y"),
      "*** Update File: /workspace/better-agent/.claude/skills/project-structure/sections/requirements/integration-contracts.md",
      "@@",
      "+`get-requirements` is supplied by the private requirements runtime MCP, which replaces the reserved server name.",
      "*** End Patch",
    ].join("\n");
    const h = await mount(<ToolCall tool="apply_patch" args={longPatch} />);

    expect(h.container.querySelector(".apply-patch-tool-call")).not.toBeNull();
    expect(h.container.querySelector(".apply-patch-summary-toggle")).not.toBeNull();
    expect(h.container.querySelector(".apply-patch-summary-paths")).not.toBeNull();
    expect(h.container.querySelectorAll(".apply-patch-diff")).toHaveLength(2);

    h.unmount();
  });
});

describe("ToolCall — edit diff rendering", () => {
  it("shows Edit diffs expanded by default", async () => {
    const h = await mount(
      <ToolCall
        tool="Edit"
        args={JSON.stringify({
          file_path: "/tmp/demo.ts",
          old_string: "old line",
          new_string: "new line",
        })}
      />,
    );

    expect(h.container.textContent).toContain("diff");
    expect(h.container.textContent).toContain("- old line");
    expect(h.container.textContent).toContain("+ new line");

    h.unmount();
  });

  it("shows Claude MultiEdit diffs expanded by default", async () => {
    const h = await mount(
      <ToolCall
        tool="MultiEdit"
        args={JSON.stringify({
          file_path: "/tmp/demo.ts",
          edits: [
            { old_string: "first old", new_string: "first new" },
            { old_string: "second old", new_string: "second new" },
          ],
        })}
      />,
    );

    expect(h.container.textContent).toContain("MultiEdit");
    expect(h.container.textContent).toContain("- first old");
    expect(h.container.textContent).toContain("+ first new");
    expect(h.container.textContent).toContain("- second old");
    expect(h.container.textContent).toContain("+ second new");

    h.unmount();
  });

  it("shows Gemini-normalized Edit diffs expanded by default", async () => {
    const h = await mount(
      <ToolCall
        tool="Edit"
        args={JSON.stringify({
          file_path: "/tmp/gemini.ts",
          old_string: "gemini old",
          new_string: "gemini new",
        })}
      />,
    );

    expect(h.container.textContent).toContain("- gemini old");
    expect(h.container.textContent).toContain("+ gemini new");

    h.unmount();
  });
});

describe("ToolCall — Bash result rendering", () => {
  it("moves Codex metadata below command output and drops the Output label", async () => {
    const result = [
      "Chunk ID: 1e7ac9",
      "Wall time: 0.0000 seconds",
      "Process exited with code 0",
      "Original token count: 1939",
      "Output:",
      "matched",
      "next line",
    ].join("\n");

    const h = await mount(
      <ToolCall
        tool="Bash"
        args={JSON.stringify({ command: "rg matched" })}
        result={result}
      />,
    );

    const rendered = h.container.querySelector(".bash-result-content")?.textContent;
    expect(rendered).toBe([
      "matched",
      "next line",
      "",
      "Chunk ID: 1e7ac9",
      "Wall time: 0.0000 seconds",
      "Process exited with code 0",
      "Original token count: 1939",
    ].join("\n"));
    expect(rendered).not.toContain("Output:");

    h.unmount();
  });
});

describe("ToolCall — get_requirements result rendering", () => {
  it("renders wrapped requirement JSON as compact match cards", async () => {
    const payload = {
      success: true,
      count: 1,
      matches: [
        {
          text: "Use requirement units instead of raw prompts.",
          kind: "explicit",
          source: "user",
          polarity: "positive",
          strength: "high",
          cwd: "/workspace/better-agent",
          edited_files: ["backend/requirement_context.py"],
          source_text: "get requirements mcp should use requirement units",
        },
      ],
      freshness: {
        fresh: false,
        unhandled_prompts: 3,
        unit_sync: { error: "requirement unit extraction already running" },
      },
    };
    const result = `Wall time: 22.0757 seconds\nOutput:\n${JSON.stringify(payload)}`;

    const h = await mount(
      <ToolCall
        tool="mcp__get_requirements__get_requirements"
        args={JSON.stringify({ rg_args: ["-i", "requirements"] })}
        result={result}
      />,
    );

    expect(h.container.textContent).toContain("1 matches");
    expect(h.container.textContent).toContain("stale by 3");
    expect(h.container.textContent).not.toContain('"matches"');

    const button = h.container.querySelector(".tool-result-toggle");
    await act(async () => {
      button?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(h.container.textContent).toContain("Use requirement units instead of raw prompts.");
    expect(h.container.textContent).toContain("backend/requirement_context.py");
    expect(h.container.textContent).toContain("requirement unit extraction already running");

    h.unmount();
  });
});

describe("ToolCall — session marker nesting", () => {
  it("keeps the collapsed toggle static and renders a native link after expansion", async () => {
    const marker = sessionLinkMarker("sid-123", "Linked Session");
    const h = await mount(
      <ToolCall tool="example_tool" args="{}" result={`${marker} ${"details ".repeat(20)}`} />,
    );
    const button = h.container.querySelector(".tool-result-toggle");

    expect(button?.textContent).toContain("Linked Session · sid-");
    expect(button?.querySelector("a")).toBeNull();

    await act(async () => {
      button?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(h.container.querySelector('.tool-result-content a[href="/s/sid-123"]')).not.toBeNull();
    h.unmount();
  });
});
