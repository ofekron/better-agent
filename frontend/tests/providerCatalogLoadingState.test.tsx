// @vitest-environment happy-dom
//
// A9 (parity audit): `TemplateGrid` must render an explicit loading/error
// state instead of a blank grid while the installable-provider catalog is
// in flight or failed — verified directly against the component (not via
// a full page render, `providerFormFields.test.ts` already covers the
// pure-helper half of this closure).

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import "../src/i18n";
import { TemplateGrid, type Template } from "../src/components/ProviderForm";

function template(id: string): Template {
  return {
    id,
    label: id,
    blurb: "",
    defaults: { name: id, kind: id, mode: "api_key", base_url: "", config_dir: "", default_model: "", default_reasoning_effort: "" },
    formSchema: [],
  };
}

describe("TemplateGrid — A9 loading/error states", () => {
  it("renders a loading placeholder, not a blank grid, while templates is empty and loading", () => {
    render(<TemplateGrid templates={[]} loading={true} error={null} onPick={() => {}} />);
    expect(screen.getByText("Loading…")).toBeTruthy();
    expect(document.querySelector(".provider-templates")).toBeNull();
  });

  it("renders an error + retry affordance, not a blank grid, when the fetch failed", () => {
    const onRetry = vi.fn();
    render(<TemplateGrid templates={[]} loading={false} error="HTTP 500: boom" onRetry={onRetry} onPick={() => {}} />);
    expect(screen.getByText("HTTP 500: boom")).toBeTruthy();
    expect(document.querySelector(".provider-templates")).toBeNull();

    fireEvent.click(screen.getByTestId("provider-catalog-error").querySelector("button")!);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("renders the real grid once templates arrive, even if loading is still true (e.g. a background retry)", () => {
    render(<TemplateGrid templates={[template("claude")]} loading={true} error={null} onPick={() => {}} />);
    expect(document.querySelector(".provider-templates")).not.toBeNull();
    expect(screen.queryByText("Loading…")).toBeNull();
  });

  it("prefers the loaded templates over a stale error once data arrives", () => {
    render(<TemplateGrid templates={[template("claude")]} loading={false} error="stale" onPick={() => {}} />);
    expect(document.querySelector(".provider-templates")).not.toBeNull();
    expect(screen.queryByTestId("provider-catalog-error")).toBeNull();
  });
});
