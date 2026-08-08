// `diagnostic`/`unknown` nodes (src/surface/nodes/Misc.tsx's DiagnosticView/
// UnknownView, both dispatching to leaf/Chips.tsx's DiagnosticBox) and
// `fact` nodes (FactView -> FactChip). Ported gap closure for
// tests/messagebubble-diagnosticevent.test.tsx (deleted): DiagnosticBox
// existed with zero test coverage. Native's disclosure box uses plain
// glyph toggles (▸/▾), not legacy's chevron <svg>, and label text is
// "unknown: <kind>" not "unknown event: <kind>" — copied verbatim from
// the component, not invented here.

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DiagnosticView, FactView, UnknownView } from "../../src/surface/nodes/Misc";
import { node, resetSeq } from "./fixtures";

resetSeq();

describe("DiagnosticView", () => {
  it("execution_continuation code renders the continuation text directly, not a disclosure box", () => {
    const n = node({
      node_id: "d1",
      turn_id: "t1",
      kind: "diagnostic",
      payload: { severity: "info", code: "execution_continuation", text: "continuing…", data: null },
    });
    render(<DiagnosticView node={n} />);
    expect(screen.getByTestId("surface-diagnostic-continuation").textContent).toBe("continuing…");
    expect(screen.queryByTestId("surface-diagnostic-box")).toBeNull();
  });

  it("other codes render a closed disclosure box; clicking reveals the JSON-pretty payload", () => {
    const n = node({
      node_id: "d2",
      turn_id: "t1",
      kind: "diagnostic",
      payload: { severity: "info", code: "stream_json" as never, text: "", data: { hello: "world" } },
    });
    render(<DiagnosticView node={n} />);
    const box = screen.getByTestId("surface-diagnostic-box");
    expect(box.textContent).toContain("stream_json");
    expect(box.querySelector("pre")).toBeNull();

    fireEvent.click(box.querySelector("button")!);
    expect(box.querySelector("pre")?.textContent).toBe(JSON.stringify({ hello: "world" }, null, 2));

    fireEvent.click(box.querySelector("button")!);
    expect(box.querySelector("pre")).toBeNull();
  });

  it("null payload renders nothing", () => {
    const n = node({ node_id: "d3", turn_id: "t1", kind: "diagnostic", payload: null });
    const { container } = render(<DiagnosticView node={n} />);
    expect(container.firstChild).toBeNull();
  });
});

describe("UnknownView", () => {
  it("renders a disclosure box keyed on label, with the raw payload behind the toggle", () => {
    const n = node({
      node_id: "u1",
      turn_id: "t1",
      kind: "unknown",
      payload: { label: "mystery_kind", payload: { a: 1 } },
    });
    render(<UnknownView node={n} />);
    const box = screen.getByTestId("surface-diagnostic-box");
    expect(box.textContent).toContain("mystery_kind");
    fireEvent.click(box.querySelector("button")!);
    expect(box.querySelector("pre")?.textContent).toBe(JSON.stringify({ a: 1 }, null, 2));
  });
});

describe("FactView (non pr_link kind)", () => {
  it("renders a fact chip with the kind label and a flattened data summary", () => {
    const n = node({
      node_id: "f1",
      turn_id: "t1",
      kind: "fact",
      payload: { kind: "some_fact", data: { count: 3, note: "x" } },
    });
    render(<FactView node={n} />);
    const chip = screen.getByTestId("surface-fact-chip");
    expect(chip.textContent).toContain("count: 3");
  });
});
