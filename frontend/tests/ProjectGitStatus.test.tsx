import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, waitFor } from "@testing-library/react";

// --- hoisted control plane --------------------------------------------------

const fetchFn = vi.hoisted(() => ({ current: vi.fn() }));

vi.mock("react-i18next", () => {
  // Surface count interpolation so conflicted-count assertions can read it.
  const t = (k: string, opts?: Record<string, unknown>) =>
    opts && typeof opts.count === "number" ? `${k}:${opts.count}` : k;
  return { useTranslation: () => ({ t }) };
});

vi.mock("../src/api", () => ({ API: "http://test" }));

import { ProjectGitStatus } from "../src/components/ProjectGitStatus";

// --- fetch stub -------------------------------------------------------------

type FetchResp = { ok: boolean; status: number; json: () => Promise<unknown> };

function jsonRes(body: unknown, ok = true): FetchResp {
  return { ok, status: ok ? 200 : 500, json: () => Promise.resolve(body) };
}

// Default router: clean repo status, successful commit / commit-and-push.
function defaultFetch(url: string): Promise<FetchResp> {
  const u = String(url);
  if (u.includes("/api/git-status")) {
    return Promise.resolve(jsonRes({ is_git: true, branch: "main" }));
  }
  // commit-and-push must be checked before git-commit (substring overlap).
  if (u.includes("/api/git-commit-and-push")) {
    return Promise.resolve(jsonRes({ ok: true, committed: true, output: "pushed" }));
  }
  if (u.includes("/api/git-commit")) {
    return Promise.resolve(jsonRes({ ok: true, committed: true, output: "committed" }));
  }
  return Promise.resolve(jsonRes({}, false));
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn((url: string) => defaultFetch(url));
  fetchFn.current = fetchMock;
  (globalThis as { fetch: typeof fetch }).fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  vi.restoreAllMocks();
});

// --- fixtures ---------------------------------------------------------------

const DIRTY_ALL = {
  is_git: true,
  branch: "dev",
  modified: ["a.ts"],
  added: ["b.ts"],
  deleted: ["c.ts"],
  untracked: ["d.ts"],
  conflicted: ["e.ts"],
}; // dirtyCount = 5, conflicted = 1

const DIRTY_ONE = {
  is_git: true,
  branch: "dev",
  modified: ["only.ts"],
}; // dirtyCount = 1 (singular), no conflicted

async function dirtyStatus(body: Record<string, unknown>) {
  fetchMock.mockImplementation(async (url: string) => {
    if (String(url).includes("/api/git-status")) return jsonRes(body);
    return defaultFetch(url);
  });
}

function openCommitInput(container: HTMLElement) {
  const openBtn = container.querySelector(".project-git-btn-open") as HTMLElement;
  fireEvent.click(openBtn);
  return container.querySelector(".project-git-commit-input") as HTMLInputElement;
}

describe("ProjectGitStatus — rendering", () => {
  it("renders nothing while status is null (fetch not ok)", async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (String(url).includes("/api/git-status")) return jsonRes({}, false);
      return defaultFetch(url);
    });
    const { container } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container.querySelector(".project-git-status")).toBeNull();
  });

  it("renders nothing when status fetch rejects (network)", async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (String(url).includes("/api/git-status")) throw new Error("down");
      return defaultFetch(url);
    });
    const { container } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container.querySelector(".project-git-status")).toBeNull();
  });

  it("renders nothing for a non-git project", async () => {
    await dirtyStatus({ is_git: false });
    const { container } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />,
    );
    await waitFor(() => expect(container.querySelector(".project-git-branch")).toBeNull());
  });

  it("renders a clean repo with empty/absent file arrays", async () => {
    // All `|| []` fallbacks exercised by omitting every file array.
    await dirtyStatus({ is_git: true, branch: "main" });
    const { container } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />,
    );
    await waitFor(() => expect(container.querySelector(".project-git-branch")).not.toBeNull());
    expect(container.querySelector(".project-git-dirty.clean")?.textContent).toBe("clean");
    expect(container.querySelector(".project-git-conflict")).toBeNull();
    expect(container.querySelector(".project-git-actions")).toBeNull();
    expect(container.querySelector(".project-git-result-btn")).toBeNull();
    // branch title set
    expect(container.querySelector(".project-git-branch")?.getAttribute("title")).toBe("main");
  });

  it("renders dirty count, conflicted badge, and commit action when all arrays present", async () => {
    await dirtyStatus(DIRTY_ALL);
    const { container } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />,
    );
    await waitFor(() => expect(container.querySelector(".project-git-btn-open")).not.toBeNull());
    // dirtyCount = 5 (plural)
    expect(container.querySelector(".project-git-dirty")?.textContent).toBe("5 changed");
    const conflict = container.querySelector(".project-git-conflict");
    expect(conflict?.textContent).toBe("projectGitStatus.conflicted:1");
    expect(conflict?.getAttribute("title")).toBe("projectGitStatus.conflictedTitle:1");
    expect(container.querySelector(".project-git-btn-open")?.textContent).toBe("Commit…");
  });

  it("invokes onOpenTree when the info button is clicked", async () => {
    await dirtyStatus({ is_git: true, branch: "main" });
    const onOpenTree = vi.fn();
    const { container } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={onOpenTree} />,
    );
    await waitFor(() => expect(container.querySelector(".project-git-info")).not.toBeNull());
    fireEvent.click(container.querySelector(".project-git-info") as HTMLElement);
    expect(onOpenTree).toHaveBeenCalledTimes(1);
  });
});

describe("ProjectGitStatus — commit input", () => {
  it("focuses the input when opened and closes on Escape", async () => {
    await dirtyStatus(DIRTY_ONE);
    const { container } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />,
    );
    await waitFor(() => expect(container.querySelector(".project-git-btn-open")).not.toBeNull());
    const input = openCommitInput(container);
    expect(input).not.toBeNull();
    expect(document.activeElement).toBe(input);
    // Escape closes → back to the open button.
    fireEvent.keyDown(input, { key: "Escape" });
    await waitFor(() =>
      expect(container.querySelector(".project-git-commit-input")).toBeNull(),
    );
    expect(container.querySelector(".project-git-btn-open")).not.toBeNull();
  });

  it("does not commit on non-Enter keys", async () => {
    await dirtyStatus(DIRTY_ONE);
    const { container } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />,
    );
    await waitFor(() => expect(container.querySelector(".project-git-btn-open")).not.toBeNull());
    const input = openCommitInput(container);
    fireEvent.keyDown(input, { key: "a" });
    expect(fetchMock.mock.calls.find((c) => String(c[0]).includes("/api/git-commit"))).toBeUndefined();
  });
});

describe("ProjectGitStatus — commit / push", () => {
  it("commits with the default singular message when input is empty", async () => {
    let committed = false;
    fetchMock.mockImplementation(async (url: string) => {
      const u = String(url);
      if (u.includes("/api/git-status")) {
        return jsonRes(committed ? { is_git: true, branch: "dev" } : DIRTY_ONE);
      }
      if (u.includes("/api/git-commit") && !u.includes("and-push")) {
        committed = true;
        return jsonRes({ ok: true, committed: true, output: "ok" });
      }
      return defaultFetch(url);
    });
    const { container } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />,
    );
    await waitFor(() => expect(container.querySelector(".project-git-btn-open")).not.toBeNull());
    openCommitInput(container);
    // leave message empty → default; dirtyCount=1 → singular "file"
    fireEvent.click(container.querySelector(".project-git-btn") as HTMLElement);
    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => String(c[0]).includes("/api/git-commit"));
      expect(call).toBeDefined();
    });
    const call = fetchMock.mock.calls.find(
      (c) =>
        String(c[0]).includes("/api/git-commit") &&
        !String(c[0]).includes("and-push"),
    )!;
    const body = JSON.parse((call[1] as RequestInit).body as string);
    expect(body.message).toBe("chore: 1 file changed");
    expect(body.cwd).toBe("/r");
    expect(body.node_id).toBe("n");
    // success → message cleared, input hidden, status refreshed to clean
    await waitFor(() =>
      expect(container.querySelector(".project-git-commit-input")).toBeNull(),
    );
    expect(container.querySelector(".project-git-dirty.clean")?.textContent).toBe("clean");
  });

  it("commits with a custom message on Enter", async () => {
    await dirtyStatus(DIRTY_ALL);
    const { container } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />,
    );
    await waitFor(() => expect(container.querySelector(".project-git-btn-open")).not.toBeNull());
    const input = openCommitInput(container);
    fireEvent.change(input, { target: { value: "my message" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => String(c[0]).includes("/api/git-commit"));
      expect(call).toBeDefined();
    });
    const call = fetchMock.mock.calls.find(
      (c) =>
        String(c[0]).includes("/api/git-commit") &&
        !String(c[0]).includes("and-push"),
    )!;
    const body = JSON.parse((call[1] as RequestInit).body as string);
    expect(body.message).toBe("my message");
  });

  it("shows busy state on commit & push, then clears after success", async () => {
    await dirtyStatus(DIRTY_ALL);
    let releasePush!: (v: FetchResp) => void;
    fetchMock.mockImplementation(async (url: string) => {
      const u = String(url);
      if (u.includes("/api/git-commit-and-push")) {
        return new Promise<FetchResp>((r) => {
          releasePush = r;
        });
      }
      if (u.includes("/api/git-status")) return jsonRes(DIRTY_ALL);
      return defaultFetch(url);
    });
    const { container } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />,
    );
    await waitFor(() => expect(container.querySelector(".project-git-btn-open")).not.toBeNull());
    const input = openCommitInput(container);
    const pushBtn = container.querySelector(".project-git-btn.push") as HTMLElement;
    const commitBtn = container.querySelector(".project-git-btn") as HTMLElement;
    fireEvent.click(pushBtn);
    // busy: push shows "…", input + commit disabled
    await waitFor(() => expect(pushBtn.textContent).toBe("…"));
    expect((input as HTMLInputElement).disabled).toBe(true);
    expect((commitBtn as HTMLButtonElement).disabled).toBe(true);
    // resolve → success path: pushing cleared and commit row unmounts.
    // (On success setShowCommitInput(false) removes the whole commit row, so
    // the push button node detaches — assert row removal, not the node's text.)
    await act(async () => {
      releasePush(jsonRes({ ok: true, committed: true, output: "pushed" }));
    });
    await waitFor(() =>
      expect(container.querySelector(".project-git-commit-row")).toBeNull(),
    );
    const call = fetchMock.mock.calls.find((c) =>
      String(c[0]).includes("/api/git-commit-and-push"),
    );
    expect(call).toBeDefined();
  });

  it("records a failed commit (ok=false) with error output", async () => {
    await dirtyStatus(DIRTY_ALL);
    fetchMock.mockImplementation(async (url: string) => {
      const u = String(url);
      if (u.includes("/api/git-commit") && !u.includes("and-push")) {
        return jsonRes({ ok: false, committed: false, error: "boom" }, true);
      }
      if (u.includes("/api/git-status")) return jsonRes(DIRTY_ALL);
      return defaultFetch(url);
    });
    const { container } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />,
    );
    await waitFor(() => expect(container.querySelector(".project-git-btn-open")).not.toBeNull());
    openCommitInput(container);
    fireEvent.click(container.querySelector(".project-git-btn") as HTMLElement);
    await waitFor(() =>
      expect(container.querySelector(".project-git-result-btn.error")?.textContent).toBe("Failed"),
    );
    // failure does not clear the commit input
    expect(container.querySelector(".project-git-commit-input")).not.toBeNull();
  });

  it("records a partial commit-push (committed=true, ok=false)", async () => {
    await dirtyStatus(DIRTY_ALL);
    fetchMock.mockImplementation(async (url: string) => {
      const u = String(url);
      if (u.includes("/api/git-commit-and-push")) {
        return jsonRes({ ok: false, committed: true, error: "push failed" }, true);
      }
      if (u.includes("/api/git-status")) return jsonRes(DIRTY_ALL);
      return defaultFetch(url);
    });
    const { container } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />,
    );
    await waitFor(() => expect(container.querySelector(".project-git-btn-open")).not.toBeNull());
    openCommitInput(container);
    fireEvent.click(container.querySelector(".project-git-btn.push") as HTMLElement);
    await waitFor(() =>
      expect(container.querySelector(".project-git-result-btn.error")).not.toBeNull(),
    );
  });

  it("records a network error when the commit POST rejects", async () => {
    await dirtyStatus(DIRTY_ALL);
    fetchMock.mockImplementation(async (url: string) => {
      const u = String(url);
      if (u.includes("/api/git-commit") && !u.includes("and-push")) {
        throw new Error("network down");
      }
      if (u.includes("/api/git-status")) return jsonRes(DIRTY_ALL);
      return defaultFetch(url);
    });
    const { container } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />,
    );
    await waitFor(() => expect(container.querySelector(".project-git-btn-open")).not.toBeNull());
    openCommitInput(container);
    fireEvent.click(container.querySelector(".project-git-btn") as HTMLElement);
    await waitFor(() =>
      expect(container.querySelector(".project-git-result-btn.error")?.textContent).toBe("Failed"),
    );
  });

  it("records a network error on commit-and-push (catch action = commit-push)", async () => {
    await dirtyStatus(DIRTY_ALL);
    fetchMock.mockImplementation(async (url: string) => {
      const u = String(url);
      if (u.includes("/api/git-commit-and-push")) throw new Error("network down");
      if (u.includes("/api/git-status")) return jsonRes(DIRTY_ALL);
      return defaultFetch(url);
    });
    const { container } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />,
    );
    await waitFor(() => expect(container.querySelector(".project-git-btn-open")).not.toBeNull());
    openCommitInput(container);
    fireEvent.click(container.querySelector(".project-git-btn.push") as HTMLElement);
    await waitFor(() =>
      expect(container.querySelector(".project-git-result-btn.error")?.textContent).toBe("Failed"),
    );
    // open modal: action label is "Commit & Push", error is the network message
    fireEvent.click(container.querySelector(".project-git-result-btn") as HTMLElement);
    await waitFor(() =>
      expect(container.querySelector(".project-git-result-modal")).not.toBeNull(),
    );
    const dds = container.querySelectorAll(".project-git-result-meta dd");
    expect(dds[0].textContent).toBe("Commit & Push");
    expect(container.querySelector(".project-git-result-log")?.textContent).toBe("Network error");
  });

  it("skips status update after success when refresh returns null", async () => {
    await dirtyStatus(DIRTY_ONE);
    let commitDone = false;
    fetchMock.mockImplementation(async (url: string) => {
      const u = String(url);
      if (u.includes("/api/git-commit") && !u.includes("and-push")) {
        commitDone = true;
        return jsonRes({ ok: true, committed: true, output: "ok" });
      }
      // After commit, status refresh fails → requestStatus returns null.
      if (u.includes("/api/git-status") && commitDone) return jsonRes({}, false);
      if (u.includes("/api/git-status")) return jsonRes(DIRTY_ONE);
      return defaultFetch(url);
    });
    const { container } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />,
    );
    await waitFor(() => expect(container.querySelector(".project-git-btn-open")).not.toBeNull());
    openCommitInput(container);
    fireEvent.click(container.querySelector(".project-git-btn") as HTMLElement);
    // commit input hidden on success even though status refresh returned null
    await waitFor(() =>
      expect(container.querySelector(".project-git-commit-input")).toBeNull(),
    );
  });
});

describe("ProjectGitStatus — result modal", () => {
  async function renderWithResult(
    resultBody: Record<string, unknown>,
    andPush: boolean,
  ) {
    await dirtyStatus(DIRTY_ALL);
    fetchMock.mockImplementation(async (url: string) => {
      const u = String(url);
      if (u.includes("/api/git-commit-and-push")) return jsonRes(resultBody, true);
      if (u.includes("/api/git-commit") && !u.includes("and-push")) return jsonRes(resultBody, true);
      if (u.includes("/api/git-status")) return jsonRes(DIRTY_ALL);
      return defaultFetch(url);
    });
    const r = render(<ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />);
    await waitFor(() => expect(r.container.querySelector(".project-git-btn-open")).not.toBeNull());
    openCommitInput(r.container);
    fireEvent.click(
      andPush
        ? (r.container.querySelector(".project-git-btn.push") as HTMLElement)
        : (r.container.querySelector(".project-git-btn") as HTMLElement),
    );
    await waitFor(() =>
      expect(r.container.querySelector(".project-git-result-btn")).not.toBeNull(),
    );
    fireEvent.click(r.container.querySelector(".project-git-result-btn") as HTMLElement);
    await waitFor(() =>
      expect(r.container.querySelector(".project-git-result-modal")).not.toBeNull(),
    );
    return r;
  }

  it("shows success modal with output and Commit action label", async () => {
    const { container } = await renderWithResult(
      { ok: true, committed: true, output: "all good" },
      false,
    );
    expect(container.querySelector(".project-git-result-summary.ok")?.textContent).toBe("Success");
    expect(container.querySelector(".project-git-result-log")?.textContent).toBe("all good");
    const dds = container.querySelectorAll(".project-git-result-meta dd");
    expect(dds[0].textContent).toBe("Commit"); // action label
  });

  it("shows 'No output.' when neither output nor error present", async () => {
    const { container } = await renderWithResult(
      { ok: true, committed: true },
      false,
    );
    expect(container.querySelector(".project-git-result-log")?.textContent).toBe("No output.");
  });

  it("shows Commit & Push action label for commit-push", async () => {
    const { container } = await renderWithResult(
      { ok: true, committed: true, output: "p" },
      true,
    );
    const dds = container.querySelectorAll(".project-git-result-meta dd");
    expect(dds[0].textContent).toBe("Commit & Push");
    // message dd reflects the committed message
    expect(dds[1].textContent).toContain("chore:");
  });

  it("shows 'Commit succeeded, push failed' for partial result", async () => {
    const { container } = await renderWithResult(
      { ok: false, committed: true, error: "push err" },
      true,
    );
    expect(container.querySelector(".project-git-result-summary.error")?.textContent).toBe(
      "Commit succeeded, push failed",
    );
    expect(container.querySelector(".project-git-result-log")?.textContent).toBe("push err");
  });

  it("shows 'Failed' summary for a fully-failed result", async () => {
    const { container } = await renderWithResult(
      { ok: false, committed: false, error: "nope" },
      false,
    );
    expect(container.querySelector(".project-git-result-summary.error")?.textContent).toBe("Failed");
    expect(container.querySelector(".project-git-result-log")?.textContent).toBe("nope");
  });

  it("closes on overlay click and on close button, but not on content click", async () => {
    const { container } = await renderWithResult(
      { ok: true, committed: true, output: "x" },
      false,
    );
    // click inside content must not close (stopPropagation)
    fireEvent.click(container.querySelector(".modal-content") as HTMLElement);
    expect(container.querySelector(".project-git-result-modal")).not.toBeNull();
    // overlay click closes
    fireEvent.click(container.querySelector(".modal-overlay") as HTMLElement);
    await waitFor(() =>
      expect(container.querySelector(".project-git-result-modal")).toBeNull(),
    );
    // reopen and close via close button
    fireEvent.click(container.querySelector(".project-git-result-btn") as HTMLElement);
    await waitFor(() =>
      expect(container.querySelector(".project-git-result-modal")).not.toBeNull(),
    );
    fireEvent.click(container.querySelector(".modal-close") as HTMLElement);
    await waitFor(() =>
      expect(container.querySelector(".project-git-result-modal")).toBeNull(),
    );
  });
});

describe("ProjectGitStatus — lifecycle", () => {
  it("does not set state from a status refresh that resolves after unmount", async () => {
    let releaseStatus!: (v: FetchResp) => void;
    fetchMock.mockImplementation(
      () =>
        new Promise<FetchResp>((r) => {
          releaseStatus = r;
        }),
    );
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const { unmount } = render(
      <ProjectGitStatus cwd="/r" nodeId="n" onOpenTree={() => {}} />,
    );
    unmount();
    // Resolving now hits the `active=false` guard branch; must not setState / warn.
    await act(async () => {
      releaseStatus(jsonRes({ is_git: true, branch: "x" }));
    });
    // No React "setState on unmounted" warning was logged.
    const warned = spy.mock.calls.some((c) =>
      String(c[0] ?? "").includes("unmounted"),
    );
    expect(warned).toBe(false);
    spy.mockRestore();
  });
});
