import { describe, it, expect, vi } from "vitest";
import { type RefObject } from "react";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { ErrorBoundary } from "../src/components/ErrorBoundary";

/** A child that always throws while rendering. */
function Boom({ message }: { message: string }) {
  throw new Error(message);
}

describe("ErrorBoundary", () => {
  it("renders its children when nothing throws", () => {
    render(
      <ErrorBoundary>
        <span>all good</span>
      </ErrorBoundary>,
    );
    expect(screen.getByText("all good")).toBeTruthy();
  });

  it("catches a child render error and shows the fallback UI, stack, and reload action", () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const reloadSpy = vi.spyOn(window.location, "reload").mockImplementation(() => {});

    render(
      <ErrorBoundary>
        <Boom message="kaboom" />
      </ErrorBoundary>,
    );

    expect(screen.getByText("Something went wrong.")).toBeTruthy();
    expect(screen.getByText("kaboom")).toBeTruthy();
    // componentDidCatch logged the error and populated componentStack → details shown.
    expect(errSpy).toHaveBeenCalled();
    expect(screen.getByText("Component stack")).toBeTruthy();

    fireEvent.click(screen.getByText("Reload App"));
    expect(reloadSpy).toHaveBeenCalledOnce();
  });

  it("omits the component-stack details when componentStack is null", () => {
    const ref: RefObject<ErrorBoundary | null> = { current: null };
    render(
      <ErrorBoundary ref={ref}>
        <span>ok</span>
      </ErrorBoundary>,
    );
    act(() => {
      ref.current!.setState({ hasError: true, error: new Error("no stack"), componentStack: null });
    });
    expect(screen.getByText("no stack")).toBeTruthy();
    expect(screen.queryByText("Component stack")).toBeNull();
  });

  it("tolerates a missing componentStack in componentDidCatch (nullish-coalesce to null)", () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const ref: RefObject<ErrorBoundary | null> = { current: null };
    render(
      <ErrorBoundary ref={ref}>
        <span>ok</span>
      </ErrorBoundary>,
    );
    act(() => {
      ref.current!.setState({ hasError: true, error: new Error("edge"), componentStack: null });
      ref.current!.componentDidCatch(new Error("edge"), { componentStack: undefined as unknown as string });
    });
    // componentStack stayed null (no details) and the error was still logged.
    expect(errSpy).toHaveBeenCalled();
    expect(screen.queryByText("Component stack")).toBeNull();
  });
});
