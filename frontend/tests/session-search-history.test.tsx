import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionList } from "../src/components/SessionList";
import type { Provider, Session } from "../src/types";
import { makeSession } from "./fixtures";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const providers: Provider[] = [];

const HISTORY_KEY = "better-agent-session-search-history";

function renderList(
  sessions: Session[],
  props: Partial<ComponentProps<typeof SessionList>> = {},
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => new Promise<Response>(() => {})),
  );
  return render(
    <SessionList
      sessions={sessions}
      providers={providers}
      onSelect={() => {}}
      onDelete={() => {}}
      onRename={() => {}}
      onPin={() => {}}
      onUnpinOthers={() => {}}
      onArchive={() => {}}
      onWorkerEligible={() => {}}
      onAgentRenameAllowed={() => {}}
      onDetails={() => {}}
      {...props}
    />,
  );
}

function searchBox(): HTMLElement {
  return screen.getByRole("textbox", { name: "session.searchPlaceholder" });
}

function historyOptions(): string[] {
  return screen
    .queryAllByRole("option")
    .map((el) => el.textContent?.trim() ?? "");
}

describe("SessionList search history", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
    localStorage.clear();
  });

  it("records a committed query and surfaces it as a completion option", () => {
    localStorage.clear();
    renderList([makeSession({ id: "s1", name: "One" })]);

    const box = searchBox();
    fireEvent.focus(box);
    fireEvent.change(box, { target: { value: "invoice" } });
    fireEvent.blur(box);

    expect(JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]")).toEqual([
      "invoice",
    ]);

    fireEvent.change(box, { target: { value: "" } });
    fireEvent.focus(box);
    expect(historyOptions()).toContain("invoice");
  });

  it("filters completions to entries that fit the typed text (case-insensitive)", () => {
    localStorage.setItem(
      HISTORY_KEY,
      JSON.stringify(["invoice draft", "billing report", "invoice final"]),
    );
    renderList([makeSession({ id: "s1", name: "One" })]);

    const box = searchBox();
    fireEvent.focus(box);
    fireEvent.change(box, { target: { value: "INV" } });

    expect(historyOptions()).toEqual(["invoice draft", "invoice final"]);
  });

  it("shows at most 5 completions, most recent first", () => {
    localStorage.setItem(
      HISTORY_KEY,
      JSON.stringify(["q1", "q2", "q3", "q4", "q5", "q6", "q7"]),
    );
    renderList([makeSession({ id: "s1", name: "One" })]);

    const box = searchBox();
    fireEvent.focus(box);

    expect(historyOptions()).toEqual(["q1", "q2", "q3", "q4", "q5"]);
  });

  it("dedupes and bumps recency when re-committing a query", () => {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(["alpha", "beta"]));
    renderList([makeSession({ id: "s1", name: "One" })]);

    const box = searchBox();
    fireEvent.focus(box);
    fireEvent.change(box, { target: { value: "beta" } });
    fireEvent.blur(box);

    expect(JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]")).toEqual([
      "beta",
      "alpha",
    ]);
  });

  it("does not record blank queries", () => {
    localStorage.clear();
    renderList([makeSession({ id: "s1", name: "One" })]);

    const box = searchBox();
    fireEvent.focus(box);
    fireEvent.change(box, { target: { value: "   " } });
    fireEvent.blur(box);

    expect(localStorage.getItem(HISTORY_KEY)).toBeNull();
  });

  it("excludes an entry identical to the current text", () => {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(["needle", "needle2"]));
    renderList([makeSession({ id: "s1", name: "One" })]);

    const box = searchBox();
    fireEvent.focus(box);
    fireEvent.change(box, { target: { value: "needle" } });

    expect(historyOptions()).toEqual(["needle2"]);
  });


  it("preserves Escape-to-clear while history suggestions are visible", () => {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(["needle history"]));
    renderList([makeSession({ id: "s1", name: "One" })]);

    const box = searchBox() as HTMLInputElement;
    fireEvent.focus(box);
    fireEvent.change(box, { target: { value: "needle" } });
    expect(historyOptions()).toEqual(["needle history"]);

    fireEvent.keyDown(box, { key: "Escape" });

    expect(box.value).toBe("");
    expect(historyOptions()).toEqual([]);
  });

  it("fills the field when a completion is picked", async () => {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(["invoice draft"]));
    renderList([makeSession({ id: "s1", name: "One" })]);

    const box = searchBox() as HTMLInputElement;
    fireEvent.focus(box);
    const option = within(screen.getByRole("listbox")).getByRole("option", {
      name: /invoice draft/,
    });
    fireEvent.mouseDown(option);

    await waitFor(() => expect(box.value).toBe("invoice draft"));
  });

  it("navigates completions with arrow keys and selects with Enter", async () => {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(["first", "second"]));
    renderList([makeSession({ id: "s1", name: "One" })]);

    const box = searchBox() as HTMLInputElement;
    fireEvent.focus(box);
    fireEvent.keyDown(box, { key: "ArrowDown" });
    fireEvent.keyDown(box, { key: "ArrowDown" });
    fireEvent.keyDown(box, { key: "Enter" });

    await waitFor(() => expect(box.value).toBe("second"));
  });
});
