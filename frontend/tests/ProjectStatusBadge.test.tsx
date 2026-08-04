import { describe, it, expect, beforeEach, vi } from "vitest";
import { render } from "@testing-library/react";
import type { ProjectAggregate } from "../src/lib/sessionRegistry";
import { ProjectStatusBadge } from "../src/components/ProjectStatusBadge";

// useProjectAggregate is the only export this component consumes from the
// registry; route it through a spy so each test controls the aggregate.
const { aggregateMock } = vi.hoisted(() => ({ aggregateMock: vi.fn() }));
vi.mock("../src/lib/sessionRegistry", () => ({
  useProjectAggregate: (path: string, nodeId: string) =>
    aggregateMock(path, nodeId),
}));

const ZERO: ProjectAggregate = {
  running_count: 0,
  unread_session_count: 0,
  waiting_for_user_count: 0,
  errored_count: 0,
};

beforeEach(() => aggregateMock.mockReset());

describe("ProjectStatusBadge", () => {
  it("renders nothing when every counter is zero", () => {
    aggregateMock.mockReturnValue(ZERO);
    const { container } = render(<ProjectStatusBadge path="/repo" />);
    expect(container.textContent).toBe("");
    // nodeId defaults to "primary".
    expect(aggregateMock).toHaveBeenCalledWith("/repo", "primary");
  });

  it("shows a singular title when a count is exactly 1", () => {
    aggregateMock.mockReturnValue({ ...ZERO, errored_count: 1 });
    const { getByTestId } = render(<ProjectStatusBadge path="/repo" />);
    const badge = getByTestId("project-errored-count");
    expect(badge.textContent).toBe("1");
    // Empty-resources i18n returns the key verbatim — singular key resolved.
    expect(badge.getAttribute("title")).toBe("projects.errored_1");
  });

  it("shows a plural title and the raw count for values 2..99", () => {
    aggregateMock.mockReturnValue({
      running_count: 3,
      unread_session_count: 42,
      waiting_for_user_count: 0,
      errored_count: 0,
    });
    const { getByTestId } = render(<ProjectStatusBadge path="/repo" />);
    const running = getByTestId("project-running-count");
    const unread = getByTestId("project-unread-count");
    expect(running.textContent).toBe("3");
    expect(running.getAttribute("title")).toBe("projects.running_other");
    expect(unread.textContent).toBe("42");
    expect(unread.getAttribute("title")).toBe("projects.unread_other");
    // zero counters stay absent
    expect(() => getByTestId("project-waiting-count")).toThrow();
    expect(() => getByTestId("project-errored-count")).toThrow();
  });

  it("caps the visible number at 99+ while keeping the real count in the title", () => {
    aggregateMock.mockReturnValue({ ...ZERO, waiting_for_user_count: 150 });
    const { getByTestId } = render(<ProjectStatusBadge path="/repo" />);
    const badge = getByTestId("project-waiting-count");
    expect(badge.textContent).toBe("99+");
    expect(badge.getAttribute("title")).toBe("projects.waiting_other");
  });

  it("renders all four counters together and stamps data-project-path", () => {
    aggregateMock.mockReturnValue({
      running_count: 2,
      unread_session_count: 5,
      waiting_for_user_count: 7,
      errored_count: 9,
    });
    const { getByTestId } = render(<ProjectStatusBadge path="/deep/repo" />);
    for (const testid of [
      "project-errored-count",
      "project-waiting-count",
      "project-running-count",
      "project-unread-count",
    ]) {
      const badge = getByTestId(testid);
      expect(badge.getAttribute("data-project-path")).toBe("/deep/repo");
    }
  });

  it("passes an explicit nodeId through to the hook", () => {
    aggregateMock.mockReturnValue(ZERO);
    render(<ProjectStatusBadge path="/repo" nodeId="worker-2" />);
    expect(aggregateMock).toHaveBeenCalledWith("/repo", "worker-2");
  });
});
