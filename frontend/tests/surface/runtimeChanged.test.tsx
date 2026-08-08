// `model_change` / `harness_change` nodes — RuntimeChanged.tsx. Ported gap
// closure for tests/messagebubble-modelswitchedevent.test.tsx (deleted):
// tests/model-switch-grouping.test.tsx already covers banner PLACEMENT
// (before the affected turn) and the "from -> to" text case; this file
// closes the remaining coverage on the SAME existing components: the
// to-only render (no previous state) and HarnessChangeView, which had
// zero test coverage. NOT ported: reasoning-effort change display and
// provider name/nickname formatting — ModelChangePayloadWire/
// HarnessChangePayloadWire (src/adapter/wire.ts) carry only flat display
// strings with no reasoning-effort field; that composition now happens
// server-side and the client-side formatting logic legacy tested no
// longer exists to test — flagged as a design-change note in the ledger,
// not a fabricated gap-closure test.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HarnessChangeView, ModelChangeView } from "../../src/surface/nodes/RuntimeChanged";
import { node, resetSeq } from "./fixtures";

resetSeq();

describe("ModelChangeView", () => {
  it("renders 'from -> to' when a previous run is known", () => {
    const n = node({
      node_id: "m1",
      turn_id: "t1",
      kind: "model_change",
      payload: { from_run_ref: "claude / sonnet", to_run_ref: "codex / gpt-5-codex", source: "provider" },
    });
    render(<ModelChangeView node={n} />);
    expect(screen.getByTestId("surface-model-change").textContent).toContain("claude / sonnet → codex / gpt-5-codex");
  });

  it("renders only 'to' when there is no previous run (to-only case)", () => {
    const n = node({
      node_id: "m2",
      turn_id: "t1",
      kind: "model_change",
      payload: { from_run_ref: null, to_run_ref: "codex / gpt-5-codex", source: "user" },
    });
    render(<ModelChangeView node={n} />);
    const el = screen.getByTestId("surface-model-change");
    expect(el.textContent).toContain("codex / gpt-5-codex");
    expect(el.textContent).not.toContain("→");
    expect(el.getAttribute("data-source")).toBe("user");
  });

  it("null payload renders nothing", () => {
    const n = node({ node_id: "m3", turn_id: "t1", kind: "model_change", payload: null });
    const { container } = render(<ModelChangeView node={n} />);
    expect(container.firstChild).toBeNull();
  });
});

describe("HarnessChangeView", () => {
  it("renders 'from -> to' harness profile ids", () => {
    const n = node({
      node_id: "h1",
      turn_id: "t1",
      kind: "harness_change",
      payload: { from_harness_profile_id: "cli", to_harness_profile_id: "sdk" },
    });
    render(<HarnessChangeView node={n} />);
    expect(screen.getByTestId("surface-harness-change").textContent).toContain("cli → sdk");
  });

  it("renders only 'to' when there is no previous profile", () => {
    const n = node({
      node_id: "h2",
      turn_id: "t1",
      kind: "harness_change",
      payload: { from_harness_profile_id: null, to_harness_profile_id: "sdk" },
    });
    render(<HarnessChangeView node={n} />);
    const el = screen.getByTestId("surface-harness-change");
    expect(el.textContent).toContain("sdk");
    expect(el.textContent).not.toContain("→");
  });

  it("null payload renders nothing", () => {
    const n = node({ node_id: "h3", turn_id: "t1", kind: "harness_change", payload: null });
    const { container } = render(<HarnessChangeView node={n} />);
    expect(container.firstChild).toBeNull();
  });
});
