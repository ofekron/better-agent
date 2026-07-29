/**
 * Regression test — "New-task" modal stall on large session lists.
 *
 * Invariant: an unrelated parent re-render (opening the New Session modal
 * flips a boolean in App) must NOT re-render <SessionList> or its rows.
 * Before the fix SessionList was not React.memo'd, so every parent
 * re-render fully re-rendered it and re-ran its O(N) useMemos over hundreds
 * of sessions — the multi-second stall. After the fix SessionList is
 * React.memo'd and App passes stable prop identities, so memo bails and no
 * SessionList/SessionNode body re-runs.
 *
 * Detection: SessionList and every SessionNode call useTranslation() at the
 * top of their bodies, so the call count is a direct measure of how many of
 * those bodies executed. A memo bailout runs zero of them. (React's Profiler
 * onRender is NOT usable here: it fires for the parent's commit even when a
 * memoized descendant bails out, so it cannot distinguish a bailout.)
 */
import { useCallback, useMemo, useState } from "react";
import { act, fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useTranslation } from "react-i18next";

import { SessionList } from "../src/components/SessionList";
import { makeSession } from "./fixtures";
import type { Provider, Session } from "../src/types";

vi.mock("react-i18next", () => ({
  useTranslation: vi.fn(() => ({ t: (key: string) => key })),
}));

const providers: Provider[] = [
  {
    id: "claude",
    name: "Claude",
    kind: "claude",
    mode: "subscription",
    base_url: "",
    config_dir: "",
    custom_models: [],
    default_model: "claude-sonnet-4-6",
    reasoning_effort_options: [],
    default_reasoning_effort: "",
    has_api_key: false,
    supports_fork: true,
    supports_manager_mode: true,
    supports_rewind: true,
    supports_steering: false,
    supports_native_subagents: false,
    supports_reasoning_effort: false,
    capability_overrides: {},
  },
];

function makeSessions(count: number): Session[] {
  return Array.from({ length: count }, (_, i) =>
    makeSession({ id: `s-${i}`, name: `session ${i}` }),
  );
}

/** Parent that holds an unrelated boolean (the modal-open analogue) and
 * passes ONLY stable prop identities into <SessionList>. */
function Parent() {
  const [, setToggle] = useState(false);
  const sessions = useMemo(() => makeSessions(300), []);
  const onSelect = useCallback(() => {}, []);
  const onDelete = useCallback(() => {}, []);
  const onDeleteSelected = useCallback(() => {}, []);
  const onRename = useCallback(() => {}, []);
  const onPin = useCallback(() => {}, []);
  const onArchive = useCallback(() => {}, []);
  const onMoveToProject = useCallback(() => {}, []);
  const onWorkerEligible = useCallback(() => {}, []);
  const onAgentRenameAllowed = useCallback(() => {}, []);
  const onDetails = useCallback(() => {}, []);

  return (
    <>
      <button data-testid="unrelated-toggle" onClick={() => setToggle((v) => !v)}>
        toggle
      </button>
      <SessionList
        sessions={sessions}
        providers={providers}
        onSelect={onSelect}
        onDelete={onDelete}
        onDeleteSelected={onDeleteSelected}
        onRename={onRename}
        onPin={onPin}
        onArchive={onArchive}
        onMoveToProject={onMoveToProject}
        onWorkerEligible={onWorkerEligible}
        onAgentRenameAllowed={onAgentRenameAllowed}
        onDetails={onDetails}
      />
    </>
  );
}

describe("SessionList memoization — unrelated parent re-render", () => {
  it("does not re-render SessionList when an unrelated parent boolean flips", async () => {
    // SessionList fires fire-and-forget user-prefs fetches on mount; never
    // resolving them keeps the component quiescent after its mount effects.
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));

    const { getByTestId } = render(<Parent />);

    // Let mount-time effects (per-project filter load from localStorage,
    // the write-back effect) flush so the translation-call count stabilizes.
    await act(async () => {
      await Promise.resolve();
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    vi.mocked(useTranslation).mockClear();
    const callsBefore = vi.mocked(useTranslation).mock.calls.length;

    // Sanity: the list is quiescent — a no-op flush must not re-run any body.
    await act(async () => {
      await Promise.resolve();
    });
    expect(vi.mocked(useTranslation).mock.calls.length).toBe(callsBefore);

    // Opening the New Session modal is, from SessionList's perspective, just
    // an unrelated parent boolean flip. Memo must bail → zero body re-runs.
    fireEvent.click(getByTestId("unrelated-toggle"));

    expect(vi.mocked(useTranslation).mock.calls.length).toBe(callsBefore);
  });
});
