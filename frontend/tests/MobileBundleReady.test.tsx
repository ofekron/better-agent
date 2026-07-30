// @vitest-environment happy-dom

import { StrictMode } from "react";
import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "../src/components/ErrorBoundary";
import { MobileBundleReady } from "../src/components/MobileBundleReady";

function BrokenShell(): never {
  throw new Error("shell render failed");
}

describe("MobileBundleReady", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("signals readiness once after its React subtree commits under StrictMode", () => {
    const onReady = vi.fn();

    render(
      <StrictMode>
        <ErrorBoundary>
          <MobileBundleReady onReady={onReady} />
          <div>working shell</div>
        </ErrorBoundary>
      </StrictMode>,
    );

    expect(onReady).toHaveBeenCalledOnce();
  });

  it("does not signal readiness when the shell fails into the error boundary", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const onReady = vi.fn();

    render(
      <ErrorBoundary>
        <MobileBundleReady onReady={onReady} />
        <BrokenShell />
      </ErrorBoundary>,
    );

    expect(onReady).not.toHaveBeenCalled();
  });
});
