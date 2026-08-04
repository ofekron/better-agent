import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

// History-wiring is a separate concern with its own coverage.
vi.mock("../src/hooks/useBackButtonDismiss", () => ({
  useBackButtonDismiss: () => {},
}));

// setup.ts globally stubs DirPickerModal to `() => null` (App mount-cost
// isolation). Override here so this file exercises the REAL component.
vi.mock("../src/components/DirPickerModal", async () => {
  const actual = await vi.importActual<typeof import("../src/components/DirPickerModal")>(
    "../src/components/DirPickerModal",
  );
  return { __esModule: true, DirPickerModal: actual.DirPickerModal };
});

// Mutable holders so individual tests can reconfigure machine topology /
// browse-progress without re-importing. The factories read the holder at
// render time, so updating the holder + re-rendering is enough.
const machinesHolder = { machines: [] as { id: string }[] };
const inflightHolder = { value: false };

vi.mock("../src/hooks/useMachines", () => ({
  useMachines: () => ({ machines: machinesHolder.machines }),
}));
vi.mock("../src/hooks/useLocalNodeId", () => ({
  useLocalNodeId: () => "primary",
}));
vi.mock("../src/progress/store", () => ({
  trackedFetch: vi.fn(),
  useOpProgress: () => ({ inflight: inflightHolder.value }),
}));

import { trackedFetch } from "../src/progress/store";
import { DirPickerModal } from "../src/components/DirPickerModal";
import type { BrowseResult, FileSearchResult } from "../src/types";

const tracked = vi.mocked(trackedFetch);

// setup.ts inits i18n with empty resources, so t() returns the raw key.
// Query by raw key everywhere instead of English text.
function jsonRes(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}
function paramsOf(url: string) {
  return new URLSearchParams(url.split("?")[1] ?? "");
}

const HOME: BrowseResult = {
  path: "/home",
  parent: null,
  exists: true,
  entries: [
    { name: "alpha", path: "/home/alpha" },
    { name: "beta", path: "/home/beta" },
  ],
};
const EMPTY_HOME: BrowseResult = { path: "/home", parent: null, exists: true, entries: [] };
const DOCS: BrowseResult = { path: "/home/docs", parent: "/home", exists: true, entries: [] };
const NEWDIR: BrowseResult = { path: "/home/new", parent: "/home", exists: false, entries: [] };

let browseImpl: (url: string) => BrowseResult;
let searchImpl: (url: string) => FileSearchResult;
let createImpl: (
  body: { path: string; kind: string; node_id: string },
) => { ok: boolean; status: number; json: () => Promise<unknown> };

const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
  if (url.includes("/api/files/search")) return jsonRes(searchImpl(url));
  if (url.includes("/api/files/create")) {
    const body = JSON.parse((init?.body as string) ?? "{}");
    return createImpl(body);
  }
  return jsonRes({});
});

function renderModal(overrides: Record<string, unknown> = {}) {
  const onCancel = overrides.onCancel ?? vi.fn();
  const onPick = overrides.onPick ?? vi.fn();
  const utils = render(
    <DirPickerModal
      open={overrides.open ?? true}
      initialPath={overrides.initialPath as string | undefined}
      initialNodeId={overrides.initialNodeId as string | undefined}
      onCancel={onCancel}
      onPick={onPick}
      allowCreate={overrides.allowCreate ?? true}
    />,
  );
  return { ...utils, onCancel, onPick };
}

beforeEach(() => {
  machinesHolder.machines = [];
  inflightHolder.value = false;
  browseImpl = (url) => {
    const p = paramsOf(url).get("path") ?? "";
    if (!p) return HOME;
    return { path: p, parent: "/home", exists: true, entries: [] };
  };
  searchImpl = () => ({ root: null, truncated: false, count: 0 });
  createImpl = (body) => jsonRes({ path: body.path }) as unknown as ReturnType<typeof createImpl>;
  tracked.mockImplementation(async (_op: string, url: string) => jsonRes(browseImpl(url)));
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockClear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  tracked.mockReset();
});

const SEARCH_INPUT = "dirPicker.searchPlaceholder";
const PATH_INPUT = "dirPicker.pathPlaceholder";
const FOLDER_INPUT = "dirPicker.newFolderPlaceholder";

describe("DirPickerModal", () => {
  it("renders nothing when closed", () => {
    const { container } = renderModal({ open: false });
    expect(container.firstChild).toBeNull();
  });

  it("browses on open and renders directory entries", async () => {
    const { onPick } = renderModal();
    await waitFor(() => expect(screen.getByText("dirPicker.title")).toBeTruthy());
    const rows = document.querySelectorAll(".dir-picker-row");
    expect(rows.length).toBe(2);

    // double-click an entry → onPick immediately (do this before navigating,
    // which replaces the entry list)
    fireEvent.doubleClick(screen.getByText("beta"));
    expect(onPick).toHaveBeenCalledWith("/home/beta", "primary");

    // single-click an entry → browse into it
    fireEvent.click(rows[0]);
    await waitFor(() =>
      expect((screen.getByPlaceholderText(PATH_INPUT) as HTMLInputElement).value).toBe("/home/alpha"),
    );
  });

  it("shows the no-subdirs empty state for an existing empty directory", async () => {
    browseImpl = () => EMPTY_HOME;
    renderModal();
    await waitFor(() => expect(screen.getByText("dirPicker.noSubdirs")).toBeTruthy());
  });

  it("shows the loading state while a browse is in flight", async () => {
    inflightHolder.value = true;
    let resolveBrowse!: (v: unknown) => void;
    tracked.mockImplementationOnce(() => new Promise((r) => { resolveBrowse = r; }));
    renderModal();
    await waitFor(() => expect(screen.getByText("dirPicker.loading")).toBeTruthy());
    expect(
      (screen.getByText("dirPicker.selectDirectory").closest("button") as HTMLButtonElement).disabled,
    ).toBe(true);
    resolveBrowse(jsonRes(HOME));
    inflightHolder.value = false;
    await waitFor(() => expect(screen.queryByText("dirPicker.loading")).toBeNull());
  });

  it("surfaces a browse Error message", async () => {
    tracked.mockImplementationOnce(async () => { throw new Error("boom"); });
    renderModal();
    await waitFor(() => expect(screen.getByText("boom")).toBeTruthy());
  });

  it("surfaces 'Unknown error' when browse rejects with a non-Error", async () => {
    tracked.mockImplementationOnce(async () => { throw "oops"; });
    renderModal();
    await waitFor(() => expect(screen.getByText("Unknown error")).toBeTruthy());
  });

  it("falls back to home when initialPath does not exist", async () => {
    const calls: string[] = [];
    tracked.mockImplementation(async (_op, url) => {
      const p = paramsOf(url).get("path") ?? "";
      calls.push(p);
      if (p === "/ghost") return jsonRes({ ...NEWDIR, path: "/ghost" });
      return jsonRes(HOME);
    });
    renderModal({ initialPath: "/ghost" });
    await waitFor(() => expect(calls).toContain(""));
    expect(calls[0]).toBe("/ghost");
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());
  });

  it("submits the address bar to browse a typed path", async () => {
    renderModal();
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());
    const input = screen.getByPlaceholderText(PATH_INPUT);
    fireEvent.change(input, { target: { value: "/home/docs" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(document.querySelectorAll(".dir-picker-row").length).toBe(0));
  });

  it("navigates via parent (disabled when none) and home buttons", async () => {
    browseImpl = (url) => {
      const p = paramsOf(url).get("path") ?? "";
      return p === "/home/docs" ? DOCS : HOME;
    };
    renderModal();
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());
    const parentBtn = document.querySelector('[title="Parent directory"]') as HTMLButtonElement;
    expect(parentBtn.disabled).toBe(true);

    // go to docs via address submit for a parent-bearing node
    const input = screen.getByPlaceholderText(PATH_INPUT);
    fireEvent.change(input, { target: { value: "/home/docs" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(screen.getByText("dirPicker.noSubdirs")).toBeTruthy());
    expect(
      (document.querySelector('[title="Parent directory"]') as HTMLButtonElement).disabled,
    ).toBe(false);
    fireEvent.click(document.querySelector('[title="Parent directory"]')!);
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());

    fireEvent.click(document.querySelector('[title="dirPicker.homeTitle"]')!);
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());
  });

  it("offers create-and-select for a non-existent path and picks after create", async () => {
    browseImpl = () => NEWDIR;
    const onPick = vi.fn();
    renderModal({ initialPath: "/home/new", onPick });
    await waitFor(() => expect(screen.getByText("dirPicker.willCreate")).toBeTruthy());
    fireEvent.click(screen.getByText("dirPicker.createDirectory").closest("button")!);
    await waitFor(() => expect(onPick).toHaveBeenCalledWith("/home/new", "primary"));
  });

  it("creates a child folder (pickAfterCreate=false) and browses into it", async () => {
    renderModal();
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());
    fireEvent.change(screen.getByPlaceholderText(FOLDER_INPUT), { target: { value: "kid" } });
    fireEvent.click(screen.getByText("dirPicker.create").closest("button")!);
    await waitFor(() =>
      expect(tracked).toHaveBeenLastCalledWith(expect.anything(), expect.stringContaining("/api/browse")),
    );
    await waitFor(() =>
      expect((screen.getByPlaceholderText(FOLDER_INPUT) as HTMLInputElement).value).toBe(""),
    );
  });

  it("createChildDirectory guard: Enter on an empty name is a no-op", async () => {
    renderModal();
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());
    fireEvent.keyDown(screen.getByPlaceholderText(FOLDER_INPUT), { key: "Enter" });
    expect(fetchMock).not.toHaveBeenCalled();
    // a non-Enter key is ignored by the folder input's keydown handler
    fireEvent.keyDown(screen.getByPlaceholderText(FOLDER_INPUT), { key: "a" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("create POST surfaces server detail and the HTTP-status fallback", async () => {
    browseImpl = () => NEWDIR;
    renderModal({ initialPath: "/home/new" });
    await waitFor(() => expect(screen.getByText("dirPicker.willCreate")).toBeTruthy());

    createImpl = () => ({ ok: false, status: 400, json: async () => ({ detail: "nope" }) });
    fireEvent.click(screen.getByText("dirPicker.createDirectory"));
    await waitFor(() => expect(screen.getByText("nope")).toBeTruthy());

    createImpl = () => ({ ok: false, status: 403, json: async () => { throw new Error("parse"); } });
    fireEvent.click(screen.getByText("dirPicker.createDirectory"));
    await waitFor(() => expect(screen.getByText("HTTP 403")).toBeTruthy());
  });

  it("create flow shows 'Unknown error' when the POST rejects with a non-Error", async () => {
    browseImpl = () => HOME;
    renderModal();
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());
    fetchMock.mockImplementationOnce(async () => { throw "network down"; });
    fireEvent.change(screen.getByPlaceholderText(FOLDER_INPUT), { target: { value: "kid" } });
    fireEvent.click(screen.getByText("dirPicker.create").closest("button")!);
    await waitFor(() => expect(screen.getByText("Unknown error")).toBeTruthy());
  });

  it("picks the selected directory via the footer and respects allowCreate=false", async () => {
    browseImpl = () => HOME;
    const onPick = vi.fn();
    renderModal({ allowCreate: false, onPick });
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());
    expect(screen.queryByText("dirPicker.willCreate")).toBeNull();
    fireEvent.click(screen.getByText("dirPicker.selectDirectory").closest("button")!);
    expect(onPick).toHaveBeenCalledWith("/home", "primary");
  });

  it("runs a search: searching → results (truncated), select and pick via PickerNode", async () => {
    browseImpl = () => HOME;
    const { onPick } = renderModal();
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());

    let resolveSearch!: (v: FileSearchResult) => void;
    searchImpl = () => new Promise((r) => { resolveSearch = r; });
    fireEvent.change(screen.getByPlaceholderText(SEARCH_INPUT), { target: { value: "gam" } });
    await waitFor(() => expect(screen.getByText("dirPicker.searching")).toBeTruthy());
    // the search fetch is debounced 200ms — wait for it to actually fire so
    // the deferred promise's resolver is captured before we resolve it.
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const result: FileSearchResult = {
      root: {
        name: "home", path: "/home", type: "directory",
        children: [
          { name: "gamma", path: "/home/gamma", type: "directory" },
          { name: "file.txt", path: "/home/file.txt", type: "file" },
        ],
      },
      truncated: true,
      count: 5,
    };
    resolveSearch(result);
    await waitFor(() => expect(screen.getByText("dirPicker.truncated")).toBeTruthy());
    // PickerNode renders the dir child as .fp-dir and the file child as .fp-file
    await waitFor(() => expect(document.querySelector(".fp-dir")).toBeTruthy());
    const dirChild = document.querySelector(".fp-dir") as HTMLElement;
    const fileChild = document.querySelector(".fp-file") as HTMLElement;
    expect(fileChild).toBeTruthy();

    fireEvent.click(dirChild); // selectNode directory branch
    fireEvent.click(fileChild); // selectNode file branch — no-op
    fireEvent.doubleClick(fileChild); // pickNode file branch — no-op
    expect(onPick).not.toHaveBeenCalled();
    fireEvent.doubleClick(dirChild); // pickNode directory branch → onPick
    expect(onPick).toHaveBeenCalledWith("/home/gamma", "primary");
  });

  it("search with no matches and rejected search both render the no-matches state", async () => {
    browseImpl = () => HOME;
    renderModal();
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());

    searchImpl = () => ({ root: null, truncated: false, count: 0 });
    fireEvent.change(screen.getByPlaceholderText(SEARCH_INPUT), { target: { value: "zz" } });
    await waitFor(() => expect(screen.getByText("dirPicker.searchNoMatches")).toBeTruthy());

    fireEvent.change(screen.getByPlaceholderText(SEARCH_INPUT), { target: { value: "" } });
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());
    fetchMock.mockImplementationOnce(async () => { throw new Error("net"); });
    fireEvent.change(screen.getByPlaceholderText(SEARCH_INPUT), { target: { value: "yy" } });
    await waitFor(() => expect(screen.getByText("dirPicker.searchNoMatches")).toBeTruthy());
  });

  it("renders the machine selector and re-browses on machine change", async () => {
    machinesHolder.machines = [{ id: "primary" }, { id: "worker" }];
    const seen: string[] = [];
    tracked.mockImplementation(async (_op, url) => {
      seen.push(paramsOf(url).get("node_id") ?? "");
      return jsonRes(HOME);
    });
    renderModal();
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());
    const select = document.querySelector(".dir-picker-machine-row select") as HTMLSelectElement;
    expect(select).toBeTruthy();
    expect(screen.getByText(/dirPicker\.thisMachine/)).toBeTruthy();
    fireEvent.change(select, { target: { value: "worker" } });
    await waitFor(() => expect(seen).toContain("worker"));
  });

  it("overlay and close/cancel buttons dismiss; content click does not", async () => {
    const onCancel = vi.fn();
    renderModal({ onCancel });
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());
    fireEvent.click(document.querySelector(".modal-content")!);
    expect(onCancel).not.toHaveBeenCalled();
    fireEvent.click(document.querySelector(".modal-overlay")!);
    expect(onCancel).toHaveBeenCalledTimes(1);
    fireEvent.click(document.querySelector(".modal-close")!);
    expect(onCancel).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByText("dirPicker.cancel"));
    expect(onCancel).toHaveBeenCalledTimes(3);
  });
});
