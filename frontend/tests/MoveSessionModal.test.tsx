import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";

// Surface i18next interpolation args so the `moveSession.description`
// interpolation ({ session: sessionName }) is assertable. With empty
// resources the real t() returns only the key, hiding the propagated
// session name and making that coverage a line-toucher.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) => {
      if (!opts) return key;
      const vals = Object.values(opts);
      return vals.length ? `${key}:${vals.join(",")}` : key;
    },
  }),
}));

// History-manipulating back-button hook — its own behavior belongs to its
// own slice. Neutralize so this test exercises MoveSessionModal's branching.
vi.mock("../src/hooks/useBackButtonDismiss", () => ({
  useBackButtonDismiss: () => {},
}));

import { MoveSessionModal } from "../src/components/MoveSessionModal";
import type { Project } from "../src/types";

function mkProject(path: string, name = path): Project {
  return {
    path,
    name,
    created_at: "2025-01-01T00:00:00Z",
    last_used: "2025-01-01T00:00:00Z",
  };
}

const defaultProps = () => ({
  sessionName: "My Session",
  currentCwd: "/current",
  projects: [
    mkProject("/current", "current"),
    mkProject("/alpha", "Alpha"),
    mkProject("/beta", "Beta"),
  ],
  busy: false,
  error: null as string | null,
  onConfirm: vi.fn(),
  onCancel: vi.fn(),
});

function renderModal(overrides: Partial<ReturnType<typeof defaultProps>> = {}) {
  const props = { ...defaultProps(), ...overrides };
  const utils = render(<MoveSessionModal {...props} />);
  return { ...utils, props };
}

describe("MoveSessionModal", () => {
  it("filters out the current cwd from the target list", () => {
    const { queryByText, getByText } = renderModal();
    expect(getByText("Alpha")).toBeTruthy();
    expect(getByText("Beta")).toBeTruthy();
    // current cwd project is excluded from targets
    expect(queryByText("current")).toBeNull();
  });

  it("renders the title and the session-name-interpolated description", () => {
    const { getByText } = renderModal({ sessionName: "Payroll Bot" });
    expect(getByText("moveSession.title")).toBeTruthy();
    expect(getByText("moveSession.description:Payroll Bot")).toBeTruthy();
  });

  it("renders the moving message and disables close when busy", () => {
    const { getByText, getByRole, props } = renderModal({ busy: true });
    expect(getByText("moveSession.moving")).toBeTruthy();
    const close = getByRole("button", { name: "×" });
    expect(close).toHaveProperty("disabled", true);
    fireEvent.click(close);
    expect(props.onCancel).not.toHaveBeenCalled();
  });

  it("does not invoke onCancel on overlay click while busy (onClick undefined)", () => {
    const { props, container } = renderModal({ busy: true });
    const overlay = container.firstChild as HTMLElement;
    expect(overlay.className).toContain("modal-overlay");
    fireEvent.click(overlay);
    expect(props.onCancel).not.toHaveBeenCalled();
  });

  it("invokes onCancel on overlay click when not busy", () => {
    const { props, container } = renderModal();
    const overlay = container.firstChild as HTMLElement;
    fireEvent.click(overlay);
    expect(props.onCancel).toHaveBeenCalledTimes(1);
  });

  it("stops propagation on content click so overlay onCancel does not fire", () => {
    const { props, container } = renderModal();
    const content = container.querySelector(".modal-content") as HTMLElement;
    fireEvent.click(content);
    expect(props.onCancel).not.toHaveBeenCalled();
  });

  it("close button invokes onCancel when not busy", () => {
    const { getByRole, props } = renderModal();
    fireEvent.click(getByRole("button", { name: "×" }));
    expect(props.onCancel).toHaveBeenCalledTimes(1);
  });

  it("renders the error message when error is provided", () => {
    const { getByText } = renderModal({ error: "boom-failed" });
    expect(getByText("boom-failed")).toBeTruthy();
  });

  it("renders noProjects message when no targets remain", () => {
    const { getByText, queryByRole } = renderModal({
      projects: [mkProject("/current", "current")],
    });
    expect(getByText("moveSession.noProjects")).toBeTruthy();
    // no list items rendered
    expect(queryByRole("listitem")).toBeNull();
  });

  it("renders a list item per target with name and path", () => {
    const { getAllByRole } = renderModal();
    const items = getAllByRole("listitem");
    expect(items).toHaveLength(2);
  });

  it("invokes onConfirm with the clicked target's path", () => {
    const { props, getByText } = renderModal();
    fireEvent.click(getByText("Beta"));
    expect(props.onConfirm).toHaveBeenCalledTimes(1);
    expect(props.onConfirm).toHaveBeenCalledWith("/beta");
  });

  it("invokes onConfirm for each distinct target independently", () => {
    const { props, getByText } = renderModal();
    fireEvent.click(getByText("Alpha"));
    fireEvent.click(getByText("Beta"));
    expect(props.onConfirm).toHaveBeenCalledWith("/alpha");
    expect(props.onConfirm).toHaveBeenCalledWith("/beta");
    expect(props.onConfirm).toHaveBeenCalledTimes(2);
  });
});
