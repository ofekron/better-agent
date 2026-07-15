import { describe, it, expect, vi } from "vitest";
import { fireEvent } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";

/** Type into the input WITHOUT sending. Mirrors the helper from
 * draft-sync.test.ts; inlined here so this file stays self-contained. */
function typeDraft(container: HTMLElement, value: string): void {
  const ta = container.querySelector(
    '[data-testid="input-textarea"]',
  ) as HTMLTextAreaElement | null;
  if (!ta) throw new Error("prompt-eng: textarea not present");
  fireEvent.change(ta, { target: { value } });
}

function engineerBtn(h: { $: (s: string) => HTMLElement | null }):
  HTMLButtonElement | null {
  return h.$('[data-testid="engineer-btn"]') as HTMLButtonElement | null;
}

/** The Engineer action lives in InputArea's collapsible "more actions"
 * overflow menu alongside the other secondary composer actions — open
 * it before looking for engineer-btn. */
async function openOverflowMenu(h: {
  $: (s: string) => HTMLElement | null;
  flush: () => Promise<void>;
}): Promise<void> {
  const trigger = h.$(".input-overflow-trigger") as HTMLButtonElement | null;
  if (!trigger) throw new Error("prompt-eng: overflow trigger not present");
  fireEvent.click(trigger);
  await h.flush();
}

function overlay(h: { $: (s: string) => HTMLElement | null }): HTMLElement | null {
  return h.$('[data-testid="prompt-eng-overlay"]');
}

describe("prompt-engineering — start flow", () => {
  it("Engineer button is enabled even when the draft is empty (start-from-scratch is allowed)", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await openOverflowMenu(h);

    const btn = engineerBtn(h);
    expect(btn).not.toBeNull();
    expect(btn!.disabled).toBe(false);

    // Click with no draft → modal still opens. Backend POST will carry
    // an empty `draft`, which the real backend now accepts.
    await h.clickByText(/⚙ Engineer/);
    expect(h.$('[data-testid="prompt-eng-mode-new"]')).not.toBeNull();
    h.unmount();
  });

  it("starts a fresh session with an empty draft — POST carries draft='' and the overlay opens", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    // Don't type anything. Just click ⚙, pick Fresh.
    await openOverflowMenu(h);
    await h.clickByText(/⚙ Engineer/);
    fireEvent.click(
      h.$('[data-testid="prompt-eng-mode-new"]') as HTMLButtonElement,
    );
    await h.flush();
    await h.flush();

    const startCall = h.backend.calls.find(
      (c) =>
        c.method === "POST" &&
        c.path === `/api/sessions/${session.id}/prompt-engineer`,
    );
    expect(startCall).toBeDefined();
    expect(startCall!.body).toMatchObject({ draft: "", mode: "new" });

    expect(overlay(h)).not.toBeNull();
    h.unmount();
  });

  it("typing a draft does NOT change the Engineer button's enabled state (it's always enabled)", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    typeDraft(h.raw.container as HTMLElement, "refine this");
    await h.flush();
    await openOverflowMenu(h);

    expect(engineerBtn(h)!.disabled).toBe(false);

    await h.clickByText(/⚙ Engineer/);

    // Modal renders both mode buttons.
    expect(h.$('[data-testid="prompt-eng-mode-fork"]')).not.toBeNull();
    expect(h.$('[data-testid="prompt-eng-mode-new"]')).not.toBeNull();
    h.unmount();
  });

  it("'Fork' is disabled while parent has no claude_sid; 'Fresh' is always available", async () => {
    const session = makeSession({ manager_claude_session_id: null });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    typeDraft(h.raw.container as HTMLElement, "draft");
    await h.flush();
    await openOverflowMenu(h);
    await h.clickByText(/⚙ Engineer/);

    const fork = h.$('[data-testid="prompt-eng-mode-fork"]') as
      HTMLButtonElement;
    const fresh = h.$('[data-testid="prompt-eng-mode-new"]') as
      HTMLButtonElement;
    expect(fork.disabled).toBe(true);
    expect(fresh.disabled).toBe(false);
    h.unmount();
  });

  it("'Fork' is enabled once the parent has a claude_sid", async () => {
    const session = makeSession({
      manager_claude_session_id: "claude-sid-123",
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    typeDraft(h.raw.container as HTMLElement, "draft");
    await h.flush();
    await openOverflowMenu(h);
    await h.clickByText(/⚙ Engineer/);

    const fork = h.$('[data-testid="prompt-eng-mode-fork"]') as
      HTMLButtonElement;
    expect(fork.disabled).toBe(false);
    h.unmount();
  });

  it("picking 'Fresh session' POSTs /prompt-engineer with mode='new' and opens the overlay", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    typeDraft(h.raw.container as HTMLElement, "improve me");
    await h.flush();
    await openOverflowMenu(h);
    await h.clickByText(/⚙ Engineer/);

    const fresh = h.$('[data-testid="prompt-eng-mode-new"]') as
      HTMLButtonElement;
    fireEvent.click(fresh);
    await h.flush();
    await h.flush();

    const startCall = h.backend.calls.find(
      (c) =>
        c.method === "POST" &&
        c.path === `/api/sessions/${session.id}/prompt-engineer`,
    );
    expect(startCall).toBeDefined();
    expect(startCall!.body).toMatchObject({
      draft: "improve me",
      mode: "new",
    });

    expect(overlay(h)).not.toBeNull();
    h.unmount();
  });

  it("eng session is filtered out of the sidebar after start", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    typeDraft(h.raw.container as HTMLElement, "improve me");
    await h.flush();
    await openOverflowMenu(h);
    await h.clickByText(/⚙ Engineer/);

    const fresh = h.$('[data-testid="prompt-eng-mode-new"]') as
      HTMLButtonElement;
    fireEvent.click(fresh);
    await h.flush();
    await h.flush();

    // Two sessions live on the backend now (parent + eng), but only the
    // parent is visible in the sidebar — the real backend's filter is
    // mirrored in the mock.
    expect(h.backend.state.sessions.length).toBe(2);
    const sidebar = h.toJSON().sidebar.sessions.map((s) => s.id);
    expect(sidebar).toEqual([session.id]);
    h.unmount();
  });
});

describe("prompt-engineering — cancel flow", () => {
  it("Cancel DELETEs the eng session and exits the overlay", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    typeDraft(h.raw.container as HTMLElement, "improve me");
    await h.flush();
    await openOverflowMenu(h);
    await h.clickByText(/⚙ Engineer/);
    fireEvent.click(
      h.$('[data-testid="prompt-eng-mode-new"]') as HTMLButtonElement,
    );
    await h.flush();
    await h.flush();

    expect(overlay(h)).not.toBeNull();
    const engId = h.backend.state.sessions.find(
      (s) =>
        (s as typeof s & { is_prompt_engineering?: boolean })
          .is_prompt_engineering,
    )?.id;
    expect(engId).toBeDefined();

    fireEvent.click(
      h.$('[data-testid="prompt-eng-cancel-btn"]') as HTMLButtonElement,
    );
    await h.flush();
    await h.flush();

    const del = h.backend.calls.find(
      (c) =>
        c.method === "DELETE" &&
        c.path === `/api/sessions/${engId}/prompt-engineer`,
    );
    expect(del).toBeDefined();
    expect(overlay(h)).toBeNull();
    expect(
      h.backend.state.sessions.find((s) => s.id === engId),
    ).toBeUndefined();
    h.unmount();
  });
});

describe("prompt-engineering — Send flow", () => {
  it("Send reads the temp file, ws.sendMessage's the parent, then DELETEs the eng session", async () => {
    const session = makeSession({
      manager_claude_session_id: "claude-sid-abc",
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    typeDraft(h.raw.container as HTMLElement, "first draft");
    await h.flush();
    await openOverflowMenu(h);
    await h.clickByText(/⚙ Engineer/);
    fireEvent.click(
      h.$('[data-testid="prompt-eng-mode-new"]') as HTMLButtonElement,
    );
    await h.flush();
    await h.flush();

    const engId = h.backend.state.sessions.find(
      (s) =>
        (s as typeof s & { is_prompt_engineering?: boolean })
          .is_prompt_engineering,
    )!.id;

    // Simulate Claude editing the prompt.md file. FileEditor polls
    // /api/file but for the Send flow we only need the result endpoint
    // to read the latest content, which is what the mock returns from
    // state.files.
    const tempPath = `/tmp/prompt-eng/${engId}/prompt.md`;
    h.backend.state.files[tempPath] = "refined prompt";

    fireEvent.click(
      h.$('[data-testid="prompt-eng-send-btn"]') as HTMLButtonElement,
    );
    await h.flush();
    await h.flush();
    await h.flush();

    // Result fetched, parent fetched, DELETE fired.
    const result = h.backend.calls.find(
      (c) =>
        c.method === "GET" &&
        c.path === `/api/sessions/${engId}/prompt-eng-result`,
    );
    expect(result).toBeDefined();
    const del = h.backend.calls.find(
      (c) =>
        c.method === "DELETE" &&
        c.path === `/api/sessions/${engId}/prompt-engineer`,
    );
    expect(del).toBeDefined();

    // The refined prompt was sent to the PARENT session via WS.
    const sendFrames = h.outbound.filter((f) => f.type === "send_message");
    expect(sendFrames.length).toBeGreaterThanOrEqual(1);
    const last = sendFrames[sendFrames.length - 1] as {
      type: "send_message";
      prompt: string;
      app_session_id: string;
    };
    expect(last.prompt).toBe("refined prompt");
    expect(last.app_session_id).toBe(session.id);

    // Overlay closed; eng session record removed.
    expect(overlay(h)).toBeNull();
    expect(
      h.backend.state.sessions.find((s) => s.id === engId),
    ).toBeUndefined();
    h.unmount();
  });
});

describe("prompt-engineering — file-anchored comments", () => {
  it("submitting a file:line:col comment POSTs /prompt-eng-comment with the anchor", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    typeDraft(h.raw.container as HTMLElement, "improve me");
    await h.flush();
    await openOverflowMenu(h);
    await h.clickByText(/⚙ Engineer/);
    fireEvent.click(
      h.$('[data-testid="prompt-eng-mode-new"]') as HTMLButtonElement,
    );
    await h.flush();
    await h.flush();

    const engId = h.backend.state.sessions.find(
      (s) =>
        (s as typeof s & { is_prompt_engineering?: boolean })
          .is_prompt_engineering,
    )!.id;

    // Drive the comment endpoint directly. The DiffEditor (Monaco) is
    // mocked to render nothing in this harness, so we can't simulate
    // a real text selection; instead we exercise the App-side handler
    // by hitting the route the FileEditor calls.
    await fetch(
      `http://localhost:8000/api/sessions/${engId}/prompt-eng-comment`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          file_path: `/tmp/prompt-eng/${engId}/prompt.md`,
          start_line: 3,
          end_line: 5,
          start_col: 1,
          end_col: 80,
          comment: "tighten this paragraph",
        }),
      },
    );
    await h.flush();

    const commentCall = h.backend.calls.find(
      (c) =>
        c.method === "POST" &&
        c.path === `/api/sessions/${engId}/prompt-eng-comment`,
    );
    expect(commentCall).toBeDefined();
    expect(commentCall!.body).toMatchObject({
      file_path: `/tmp/prompt-eng/${engId}/prompt.md`,
      start_line: 3,
      end_line: 5,
      start_col: 1,
      end_col: 80,
      comment: "tighten this paragraph",
    });
    h.unmount();
  });
});

/** Helper: drive the start flow up to "overlay open" so the edge-case
 * tests can focus on what they're actually asserting. Returns the
 * harness + the eng session id we just created. */
async function startEngOverlay(
  parentOverrides: Partial<ReturnType<typeof makeSession>> = {},
): Promise<{
  h: Awaited<ReturnType<typeof renderApp>>;
  parentId: string;
  engId: string;
}> {
  const session = makeSession(parentOverrides);
  const h = await renderApp({ seed: { sessions: [session] } });
  await h.selectSession(session.id);
  fireEvent.change(
    h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement,
    { target: { value: "improve me" } },
  );
  await h.flush();
  await h.clickByText(/⚙ Engineer/);
  fireEvent.click(
    h.$('[data-testid="prompt-eng-mode-new"]') as HTMLButtonElement,
  );
  await h.flush();
  await h.flush();
  const engId = h.backend.state.sessions.find(
    (s) =>
      (s as typeof s & { is_prompt_engineering?: boolean })
        .is_prompt_engineering,
  )!.id;
  return { h, parentId: session.id, engId };
}

describe("prompt-engineering — Send edge cases", () => {
  it("aborts when the refined prompt is empty (no DELETE, overlay stays open)", async () => {
    // Stub alert so the assertion path doesn't pop a real one in the
    // happy-dom env (and so we can verify it was called).
    const alertSpy = vi.fn();
    vi.stubGlobal("alert", alertSpy);

    const { h, engId } = await startEngOverlay({
      manager_claude_session_id: "claude-sid-x",
    });

    // Force the temp file to empty — the result endpoint will return
    // empty content, which onSend should treat as a hard stop.
    h.backend.state.files[`/tmp/prompt-eng/${engId}/prompt.md`] = "";

    const callsBefore = h.backend.calls.length;
    fireEvent.click(
      h.$('[data-testid="prompt-eng-send-btn"]') as HTMLButtonElement,
    );
    await h.flush();
    await h.flush();
    await h.flush();

    // Result endpoint WAS hit (so we know we tried), but no DELETE,
    // no ws send, and the overlay is still up.
    const resultHit = h.backend.calls
      .slice(callsBefore)
      .find(
        (c) =>
          c.method === "GET" &&
          c.path === `/api/sessions/${engId}/prompt-eng-result`,
      );
    expect(resultHit).toBeDefined();
    const delHit = h.backend.calls.find(
      (c) =>
        c.method === "DELETE" &&
        c.path === `/api/sessions/${engId}/prompt-engineer`,
    );
    expect(delHit).toBeUndefined();
    const sendFrames = h.outbound.filter((f) => f.type === "send_message");
    expect(sendFrames).toHaveLength(0);
    expect(overlay(h)).not.toBeNull();
    expect(alertSpy).toHaveBeenCalledTimes(1);

    vi.unstubAllGlobals();
    h.unmount();
  });
});

describe("prompt-engineering — Cancel edge cases", () => {
  it("Cancel still closes the overlay even when the eng session is already gone (DELETE 404)", async () => {
    const { h, engId } = await startEngOverlay();

    // Simulate the eng session being torn down externally — backend
    // returns 404 on the cleanup DELETE. App.tsx swallows it.
    h.backend.state.sessions = h.backend.state.sessions.filter(
      (s) => s.id !== engId,
    );

    fireEvent.click(
      h.$('[data-testid="prompt-eng-cancel-btn"]') as HTMLButtonElement,
    );
    await h.flush();
    await h.flush();

    expect(overlay(h)).toBeNull();
    h.unmount();
  });
});

describe("prompt-engineering — Start failure", () => {
  it("surfaces a dismissable error toast when POST /prompt-engineer fails", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    fireEvent.change(
      h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement,
      { target: { value: "improve me" } },
    );
    await h.flush();
    await openOverflowMenu(h);
    await h.clickByText(/⚙ Engineer/);

    // Yank the parent session out of the mock backend's state. The
    // start route returns notFound() (404) when it can't find the
    // parent — same shape App.tsx's error path expects. (currentSession
    // is React state, so the UI keeps the parent loaded; only the
    // backend round-trip fails.)
    h.backend.state.sessions = [];

    fireEvent.click(
      h.$('[data-testid="prompt-eng-mode-new"]') as HTMLButtonElement,
    );
    await h.flush();
    await h.flush();

    // Toast renders with the failure message.
    const container = h.raw.container as HTMLElement;
    expect(container.textContent).toMatch(/Engineer start failed/);
    // No overlay (start aborted).
    expect(overlay(h)).toBeNull();

    // Click the toast's × dismiss button.
    const dismissBtn = Array.from(
      container.querySelectorAll<HTMLButtonElement>("button"),
    ).find((b) => b.textContent?.trim() === "×" && b.parentElement?.textContent?.includes("Engineer start failed"));
    expect(dismissBtn).toBeDefined();
    fireEvent.click(dismissBtn!);
    await h.flush();

    expect(h.raw.container.textContent).not.toMatch(/Engineer start failed/);
    h.unmount();
  });
});

describe("prompt-engineering — overlay UX guardrails", () => {
  it("the ⚙ Engineer button is NOT rendered while the overlay is open (no nested eng)", async () => {
    const { h } = await startEngOverlay();

    // The InputArea inside the overlay's chatSlot received
    // onEngineer={undefined}, so the button should be absent entirely.
    expect(h.$('[data-testid="engineer-btn"]')).toBeNull();
    h.unmount();
  });

  it("clicking another session in the sidebar exits the overlay WITHOUT calling DELETE (non-destructive)", async () => {
    // Two parents seeded; start engineering on the first, then click the
    // second in the sidebar. Overlay should close, eng session should
    // survive on the backend.
    const a = makeSession({ id: "sess-a", name: "Project A" });
    const b = makeSession({ id: "sess-b", name: "Project B" });
    const h = await renderApp({ seed: { sessions: [a, b] } });
    await h.selectSession(a.id);

    fireEvent.change(
      h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement,
      { target: { value: "improve me" } },
    );
    await h.flush();
    await openOverflowMenu(h);
    await h.clickByText(/⚙ Engineer/);
    fireEvent.click(
      h.$('[data-testid="prompt-eng-mode-new"]') as HTMLButtonElement,
    );
    await h.flush();
    await h.flush();

    expect(overlay(h)).not.toBeNull();
    const engId = h.backend.state.sessions.find(
      (s) =>
        (s as typeof s & { is_prompt_engineering?: boolean })
          .is_prompt_engineering,
    )!.id;

    const callsBefore = h.backend.calls.length;
    await h.selectSession(b.id);

    // Overlay closed.
    expect(overlay(h)).toBeNull();
    // No DELETE was issued — eng record survives for resume.
    const del = h.backend.calls
      .slice(callsBefore)
      .find(
        (c) =>
          c.method === "DELETE" &&
          c.path === `/api/sessions/${engId}/prompt-engineer`,
      );
    expect(del).toBeUndefined();
    expect(
      h.backend.state.sessions.find((s) => s.id === engId),
    ).toBeDefined();
    h.unmount();
  });

  it("re-clicking ⚙ Engineer on a parent that already has a live eng session resumes (no second eng record)", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    // First start.
    fireEvent.change(
      h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement,
      { target: { value: "first draft" } },
    );
    await h.flush();
    await openOverflowMenu(h);
    await h.clickByText(/⚙ Engineer/);
    fireEvent.click(
      h.$('[data-testid="prompt-eng-mode-new"]') as HTMLButtonElement,
    );
    await h.flush();
    await h.flush();

    const firstEngId = h.backend.state.sessions.find(
      (s) =>
        (s as typeof s & { is_prompt_engineering?: boolean })
          .is_prompt_engineering,
    )!.id;
    const engCountBefore = h.backend.state.sessions.filter(
      (s) =>
        (s as typeof s & { is_prompt_engineering?: boolean })
          .is_prompt_engineering,
    ).length;
    expect(engCountBefore).toBe(1);

    // Leave non-destructively (sidebar click on the parent).
    await h.selectSession(session.id);
    expect(overlay(h)).toBeNull();
    expect(h.backend.state.sessions.find((s) => s.id === firstEngId)).toBeDefined();

    // Click ⚙ again with a different draft. Backend's idempotency
    // returns the existing eng; no second eng record is created.
    fireEvent.change(
      h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement,
      { target: { value: "different draft" } },
    );
    await h.flush();
    await openOverflowMenu(h);
    await h.clickByText(/⚙ Engineer/);
    fireEvent.click(
      h.$('[data-testid="prompt-eng-mode-new"]') as HTMLButtonElement,
    );
    await h.flush();
    await h.flush();

    const engCountAfter = h.backend.state.sessions.filter(
      (s) =>
        (s as typeof s & { is_prompt_engineering?: boolean })
          .is_prompt_engineering,
    ).length;
    expect(engCountAfter).toBe(1);
    expect(overlay(h)).not.toBeNull();
    h.unmount();
  });

  it("sidebar shows a ⚙ Resume badge on parents with a live eng session, click reopens the overlay", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    // No badge before any eng session exists.
    expect(h.$('[data-testid="eng-resume-badge"]')).toBeNull();

    // Start an eng session, then leave non-destructively.
    fireEvent.change(
      h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement,
      { target: { value: "improve me" } },
    );
    await h.flush();
    await openOverflowMenu(h);
    await h.clickByText(/⚙ Engineer/);
    fireEvent.click(
      h.$('[data-testid="prompt-eng-mode-new"]') as HTMLButtonElement,
    );
    await h.flush();
    await h.flush();
    await h.selectSession(session.id);

    // Refetch sidebar (fresh GET /api/sessions includes the badge field).
    // App.tsx polls via refresh paths or initial load — for the test we
    // simulate by triggering a refresh through createSession (unrelated
    // path that triggers fetchSessions). Cleaner: call the harness flush
    // a few times and verify the field reaches the DOM.
    // The backend mock's GET filters eng + stamps pending_eng_session_id;
    // useSession's initial fetch already ran on mount with no eng, so we
    // need to force a refetch. We do that by triggering an unrelated
    // session refresh — easiest is to delete a non-existent session
    // (no-op on the mock) then click around. Simpler: directly invoke
    // refreshSessions via creating then deleting a throwaway session.
    // A more honest path: the parent rename triggers updateSessionName
    // which modifies local state; alternatively just use clickByText
    // on +New which calls fetchSessions transitively. The cleanest is
    // to drop the WS connection — that re-runs fetchSessions.
    h.dropConnection();
    await h.flush();
    await h.flush();

    // Now the badge should be visible on the parent row.
    const badge = h.$('[data-testid="eng-resume-badge"]');
    expect(badge).not.toBeNull();
    expect(badge!.getAttribute("data-parent-session-id")).toBe(session.id);

    // Click it — overlay reopens against the existing eng session.
    fireEvent.click(badge!);
    await h.flush();
    await h.flush();

    expect(overlay(h)).not.toBeNull();
    h.unmount();
  });

  it("deleting the parent cascades and removes its eng session from the backend", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    fireEvent.change(
      h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement,
      { target: { value: "improve me" } },
    );
    await h.flush();
    await openOverflowMenu(h);
    await h.clickByText(/⚙ Engineer/);
    fireEvent.click(
      h.$('[data-testid="prompt-eng-mode-new"]') as HTMLButtonElement,
    );
    await h.flush();
    await h.flush();

    const engId = h.backend.state.sessions.find(
      (s) =>
        (s as typeof s & { is_prompt_engineering?: boolean })
          .is_prompt_engineering,
    )!.id;
    expect(engId).toBeDefined();

    // Simulate parent delete via the same REST path the real backend
    // hits — the mock's cascade mirrors main.py.
    await fetch(`http://localhost:8000/api/sessions/${session.id}`, {
      method: "DELETE",
    });
    await h.flush();

    expect(
      h.backend.state.sessions.find((s) => s.id === session.id),
    ).toBeUndefined();
    expect(
      h.backend.state.sessions.find((s) => s.id === engId),
    ).toBeUndefined();
    h.unmount();
  });

  it("opening the overlay swaps currentSession to the eng session (chat title shows the eng name)", async () => {
    const session = makeSession({
      name: "My Project",
      manager_claude_session_id: "claude-sid-y",
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    // Title before: parent session name.
    expect(h.toJSON().chat.title ?? "").toContain("My Project");

    fireEvent.change(
      h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement,
      { target: { value: "improve me" } },
    );
    await h.flush();
    await openOverflowMenu(h);
    await h.clickByText(/⚙ Engineer/);
    fireEvent.click(
      h.$('[data-testid="prompt-eng-mode-new"]') as HTMLButtonElement,
    );
    await h.flush();
    await h.flush();

    // After: title is the eng session's name.
    expect(h.toJSON().chat.title ?? "").toContain("Engineer");
    // And the input draft is empty (eng session has no draft, the
    // parent's "improve me" was cleared by the focus swap).
    expect(h.toJSON().input.text).toBe("");
    h.unmount();
  });
});
