import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import React from "react";
import type { ProjectConfigFile } from "../src/types";

// --- hoisted control plane --------------------------------------------------

const store = vi.hoisted(() => ({
  trackedFetch: vi.fn(),
  useOpProgress: vi.fn(),
}));

// trackedFetch + useOpProgress are the only exports ProjectSettings consumes
// from the progress store; reducing them keeps the WS/op machinery out.
vi.mock("../src/progress/store", () => ({
  trackedFetch: (...a: unknown[]) => store.trackedFetch(...a),
  useOpProgress: (...a: unknown[]) => store.useOpProgress(...a),
}));

vi.mock("../src/api", () => ({ API: "http://test" }));

vi.mock("../src/hooks/useBackButtonDismiss", () => ({
  useBackButtonDismiss: vi.fn(),
}));

vi.mock("../src/components/Icon", () => ({
  default: ({
    name,
    size,
    style,
  }: {
    name?: string;
    size?: number;
    style?: unknown;
  }) =>
    React.createElement("span", {
      "data-icon": name ?? "",
      "data-size": size ?? "",
      style: (style as object | undefined) ?? undefined,
    }),
}));

import { ProjectSettings } from "../src/components/ProjectSettings";

// --- helpers ----------------------------------------------------------------

type JsonRes = { json: () => Promise<unknown> };
function res(body: unknown): JsonRes {
  return { json: () => Promise.resolve(body) };
}

function file(p: Partial<ProjectConfigFile> & { path: string }): ProjectConfigFile {
  return {
    name: p.path.split("/").pop() ?? p.path,
    category: p.category ?? "instructions",
    description: p.description ?? "",
    exists: p.exists ?? true,
    size: p.size ?? 0,
    modified: p.modified ?? null,
    path: p.path,
  };
}

const BASE_PROPS = {
  cwd: "/proj/better-agent/",
  onFileClick: vi.fn(),
  onEngineerFile: vi.fn(),
  onClose: vi.fn(),
};

function setLoad(body: unknown): void {
  store.trackedFetch.mockImplementation((_opId: string, _url: string, init?: RequestInit) => {
    if (init?.method === "POST") return Promise.resolve(res({}));
    return Promise.resolve(res(body));
  });
}

beforeEach(() => {
  store.trackedFetch.mockReset();
  store.useOpProgress.mockReset();
  store.useOpProgress.mockReturnValue({ inflight: false, error: null });
  BASE_PROPS.onFileClick.mockReset();
  BASE_PROPS.onEngineerFile.mockReset();
  BASE_PROPS.onClose.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// --- tests ------------------------------------------------------------------

describe("ProjectSettings", () => {
  it("renders project name, cwd, and grouped files sorted by category", async () => {
    setLoad({
      files: [
        file({ path: "h", category: "hook", exists: true }),
        file({ path: "i", category: "instructions", exists: false }),
        file({ path: "s", category: "settings", exists: true }),
        file({ path: "k", category: "skill", exists: true }),
      ],
    });

    const { container } = render(<ProjectSettings {...BASE_PROPS} />);

    await waitFor(() =>
      expect(container.querySelectorAll(".project-settings-group")).toHaveLength(4),
    );

    // projectName strips trailing slashes and takes the last segment.
    const title = container.querySelector(".project-settings-title")!;
    expect(title.textContent).toContain("better-agent");
    expect(container.querySelector(".project-settings-subtitle")!.textContent).toBe(
      "/proj/better-agent/",
    );

    const groups = Array.from(container.querySelectorAll(".project-settings-group")).map(
      (g) => g.querySelector(".project-settings-group-count")!.textContent,
    );
    expect(groups).toStrictEqual(["1", "1", "1", "1"]);

    // Category order: instructions, settings, skill, hook.
    const headerIcons = Array.from(
      container.querySelectorAll(".project-settings-group-header [data-icon]"),
    ).map((n) => n.getAttribute("data-icon"));
    expect(headerIcons).toStrictEqual(["memo", "settings", "target", "sliders"]);

    // exists → Edit + AI Edit; missing → Create.
    expect(container.querySelectorAll(".btn-small").length).toBeGreaterThanOrEqual(4);
    const missingRow = container.querySelector(".project-settings-file.missing")!;
    expect(missingRow.querySelector("button")!.textContent).toContain("Create");
  });

  it("shows the loading indicator while the op is inflight", () => {
    store.useOpProgress.mockReturnValue({ inflight: true, error: null });
    setLoad({ files: [] });

    const { container } = render(<ProjectSettings {...BASE_PROPS} />);

    expect(container.querySelector(".project-settings-loading")!).toBeTruthy();
    expect(container.querySelector(".project-settings-group")).toBeNull();
  });

  it("shows the error message when config load fails with an Error", async () => {
    store.trackedFetch.mockRejectedValue(new Error("boom"));
    const { container } = render(<ProjectSettings {...BASE_PROPS} />);

    await waitFor(() =>
      expect(container.querySelector(".project-settings-error")!.textContent).toBe("boom"),
    );
  });

  it("shows the fallback error when config load fails with a non-Error", async () => {
    store.trackedFetch.mockRejectedValue("oops");
    const { container } = render(<ProjectSettings {...BASE_PROPS} />);

    await waitFor(() =>
      expect(container.querySelector(".project-settings-error")!.textContent).toBe(
        "projectSettings.failedToLoadConfig",
      ),
    );
  });

  it("treats a missing files field as an empty list", async () => {
    setLoad({ files: undefined });
    const { container } = render(<ProjectSettings {...BASE_PROPS} />);

    await waitFor(() =>
      expect(container.querySelector(".project-settings-group")).toBeNull(),
    );
    expect(container.querySelector(".project-settings-loading")).toBeNull();
    expect(container.querySelector(".project-settings-error")).toBeNull();
  });

  // Sequenced GET: first call returns the initial list (must contain a
  // missing file so the Create button renders); the refresh call after
  // POST returns `refreshBody`.
  function sequencedCreate(initial: ProjectConfigFile[], refreshBody: unknown): void {
    let gets = 0;
    store.trackedFetch.mockImplementation((_opId: string, _url: string, init?: RequestInit) => {
      if (init?.method === "POST") return Promise.resolve(res({}));
      gets += 1;
      return Promise.resolve(res(gets === 1 ? { files: initial } : refreshBody));
    });
  }

  it("creates a missing file, refreshes the list, and opens it", async () => {
    const created = file({ path: "CLAUDE.md", category: "instructions", exists: true });
    const missing = file({ path: "CLAUDE.md", category: "instructions", exists: false });
    sequencedCreate([missing], { files: [created] });

    const { container } = render(<ProjectSettings {...BASE_PROPS} />);
    await waitFor(() =>
      expect(container.querySelector(".project-settings-file.missing")).not.toBeNull(),
    );

    await act(async () => {
      fireEvent.click(container.querySelector(".project-settings-file.missing button")!);
    });

    expect(BASE_PROPS.onFileClick).toHaveBeenCalledWith("CLAUDE.md");
    await waitFor(() =>
      expect(container.querySelector(".project-settings-file-name")!.textContent).toBe(
        "CLAUDE.md",
      ),
    );
  });

  it("create success tolerates an undefined files field on refresh", async () => {
    const missing = file({ path: "CLAUDE.md", category: "instructions", exists: false });
    sequencedCreate([missing], { files: undefined });

    const { container } = render(<ProjectSettings {...BASE_PROPS} />);
    await waitFor(() =>
      expect(container.querySelector(".project-settings-file.missing")).not.toBeNull(),
    );

    await act(async () => {
      fireEvent.click(container.querySelector(".project-settings-file.missing button")!);
    });

    expect(BASE_PROPS.onFileClick).toHaveBeenCalledTimes(1);
  });

  it("alerts when file creation fails with an Error", async () => {
    const alertSpy = vi.fn();
    vi.stubGlobal("alert", alertSpy);
    const missing = file({ path: "CLAUDE.md", category: "instructions", exists: false });
    store.trackedFetch.mockImplementation((_opId: string, _url: string, init?: RequestInit) => {
      if (init?.method === "POST") return Promise.reject(new Error("nope"));
      return Promise.resolve(res({ files: [missing] }));
    });

    const { container } = render(<ProjectSettings {...BASE_PROPS} />);
    await waitFor(() =>
      expect(container.querySelector(".project-settings-file.missing")).not.toBeNull(),
    );

    await act(async () => {
      fireEvent.click(container.querySelector(".project-settings-file.missing button")!);
    });

    expect(alertSpy).toHaveBeenCalledWith(
      expect.stringContaining("nope"),
    );
  });

  it("alerts with the raw value when file creation fails with a non-Error", async () => {
    const alertSpy = vi.fn();
    vi.stubGlobal("alert", alertSpy);
    const missing = file({ path: "CLAUDE.md", category: "instructions", exists: false });
    store.trackedFetch.mockImplementation((_opId: string, _url: string, init?: RequestInit) => {
      if (init?.method === "POST") return Promise.reject("kaboom");
      return Promise.resolve(res({ files: [missing] }));
    });

    const { container } = render(<ProjectSettings {...BASE_PROPS} />);
    await waitFor(() =>
      expect(container.querySelector(".project-settings-file.missing")).not.toBeNull(),
    );

    await act(async () => {
      fireEvent.click(container.querySelector(".project-settings-file.missing button")!);
    });

    expect(alertSpy).toHaveBeenCalledWith(expect.stringContaining("kaboom"));
  });

  it("renders an unknown category with no icon, a raw label, sorted last", async () => {
    setLoad({
      files: [
        file({ path: "known", category: "instructions", exists: true }),
        file({ path: "weird", category: "weird" as ProjectConfigFile["category"], exists: true }),
      ],
    });

    const { container } = render(<ProjectSettings {...BASE_PROPS} />);

    await waitFor(() =>
      expect(container.querySelectorAll(".project-settings-group")).toHaveLength(2),
    );

    const groups = Array.from(container.querySelectorAll(".project-settings-group"));
    // Known category first.
    expect(groups[0]!.querySelector("[data-icon]")!.getAttribute("data-icon")).toBe("memo");
    // Unknown category: no icon, raw label text.
    const unknownHeader = groups[1]!.querySelector(".project-settings-group-header")!;
    expect(unknownHeader.querySelector("[data-icon]")).toBeNull();
    expect(unknownHeader.textContent).toContain("weird");
  });

  it("Edit and AI Edit buttons invoke their callbacks", async () => {
    setLoad({ files: [file({ path: "CLAUDE.md", category: "instructions", exists: true })] });

    const { container, getByText } = render(<ProjectSettings {...BASE_PROPS} />);
    await waitFor(() =>
      expect(container.querySelector(".project-settings-file")).not.toBeNull(),
    );

    fireEvent.click(getByText("Edit"));
    expect(BASE_PROPS.onFileClick).toHaveBeenCalledWith("CLAUDE.md");

    fireEvent.click(getByText("AI Edit"));
    expect(BASE_PROPS.onEngineerFile).toHaveBeenCalledWith("CLAUDE.md", "");
  });

  it("close button invokes onClose", async () => {
    setLoad({ files: [] });
    const { container } = render(<ProjectSettings {...BASE_PROPS} />);
    await waitFor(() =>
      expect(container.querySelector(".project-settings-close")).not.toBeNull(),
    );
    fireEvent.click(container.querySelector(".project-settings-close")!);
    expect(BASE_PROPS.onClose).toHaveBeenCalledTimes(1);
  });

  it("falls back to cwd for projectName when the path has no name segment", async () => {
    setLoad({ files: [] });
    const { container } = render(
      <ProjectSettings {...BASE_PROPS} cwd="/" />,
    );
    await waitFor(() =>
      expect(container.querySelector(".project-settings-group")).toBeNull(),
    );
    expect(container.querySelector(".project-settings-title")!.textContent).toContain("/");
  });

  it("cancels the pending load (success) on unmount before resolve", async () => {
    let resolveLoad: (v: JsonRes) => void = () => {};
    store.trackedFetch.mockImplementation(
      () => new Promise<JsonRes>((r) => { resolveLoad = r; }),
    );

    const { unmount } = render(<ProjectSettings {...BASE_PROPS} />);
    unmount();

    // Resolving after unmount must not throw (cancelled guard holds).
    await act(async () => {
      resolveLoad(res({ files: [file({ path: "x" })] }));
      await Promise.resolve();
    });
  });

  it("cancels the pending load (error) on unmount before reject", async () => {
    let rejectLoad: (e: unknown) => void = () => {};
    store.trackedFetch.mockImplementation(
      () => new Promise<JsonRes>((_, r) => { rejectLoad = r; }),
    );

    const { unmount } = render(<ProjectSettings {...BASE_PROPS} />);
    unmount();

    await act(async () => {
      rejectLoad(new Error("late"));
      await Promise.resolve();
    });
  });
});
