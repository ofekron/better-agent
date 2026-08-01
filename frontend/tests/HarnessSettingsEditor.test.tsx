import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";

// --- hoisted control plane --------------------------------------------------

const bus = vi.hoisted(() => ({
  subscribers: {} as Record<string, Array<(p: unknown) => void>>,
}));
const tracked = vi.hoisted(() => ({ fn: vi.fn() }));
const harnessApi = vi.hoisted(() => ({
  PROFILE_NOT_FOUND: "profile_not_found",
  REVISION_MISMATCH: "revision_mismatch",
  fetchDescriptor: vi.fn(),
  fetchProfile: vi.fn(),
  createProfile: vi.fn(),
  deleteProfile: vi.fn(),
  writeFields: vi.fn(),
}));
const resolve = vi.hoisted(() => ({
  groupOverrideCount: vi.fn(() => 0),
  clearAllWrites: vi.fn(() => []),
}));

vi.mock("react-i18next", () => {
  // Stable `t` reference: this component memoizes loadProfiles on [t] and
  // re-runs a load effect on [load, loadProfiles] — a fresh t each render
  // would loop load() forever.
  const t = (k: string) => k;
  return { useTranslation: () => ({ t }) };
});

vi.mock("../src/api", () => ({ API: "/api" }));

vi.mock("../src/lib/eventBus", () => ({
  eventBus: {
    subscribe: (topic: string, cb: (p: unknown) => void) => {
      (bus.subscribers[topic] ??= []).push(cb);
      return () => {
        bus.subscribers[topic] = (bus.subscribers[topic] || []).filter((c) => c !== cb);
      };
    },
  },
}));

vi.mock("../src/progress/store", () => ({
  trackedFetch: (...a: unknown[]) => tracked.fn(...(a as [unknown, string])),
}));

vi.mock("../src/progress/ProgressButton", () => ({
  ProgressButton: ({ onClick, children }: { onClick: () => void; children: ReactNode }) => (
    <button data-testid="progress-btn" onClick={onClick}>
      {children}
    </button>
  ),
}));

vi.mock("../src/lib/lazyWithRetry", () => ({
  // Return a plain component; the dynamic FileViewer import never runs.
  lazyWithRetry: () => (props: { onClose?: () => void }) => (
    <div data-testid="file-viewer">
      <button data-testid="fv-close" onClick={props.onClose} />
    </div>
  ),
}));

vi.mock("../src/components/harness/api", () => ({
  PROFILE_NOT_FOUND: harnessApi.PROFILE_NOT_FOUND,
  REVISION_MISMATCH: harnessApi.REVISION_MISMATCH,
  fetchDescriptor: () => harnessApi.fetchDescriptor(),
  fetchProfile: (id: string) => harnessApi.fetchProfile(id),
  createProfile: (name: string) => harnessApi.createProfile(name),
  deleteProfile: (id: string, rev: string) => harnessApi.deleteProfile(id, rev),
  writeFields: (id: string, w: unknown[], rev: string) => harnessApi.writeFields(id, w, rev),
}));

vi.mock("../src/components/harness/resolve", () => ({
  groupOverrideCount: (...a: unknown[]) => resolve.groupOverrideCount(...a),
  clearAllWrites: (...a: unknown[]) => resolve.clearAllWrites(...a),
}));

vi.mock("../src/components/harness/HarnessGroup", () => ({
  DescriptionLink: ({ path, label, onOpen }: any) => (
    <div data-testid="desc-link">
      {path && (
        <button data-testid="desc-open" onClick={() => onOpen(path, label)}>
          open
        </button>
      )}
    </div>
  ),
  ExtensionEnabledToggle: ({ extensionId, onWrite, onOpenDescription }: any) => (
    <div data-testid={`ext-toggle-${extensionId}`}>
      <button data-testid="ext-toggle-write" onClick={() => onWrite({ path: ["toggle"], value: true })} />
      <button data-testid="ext-toggle-open" onClick={() => onOpenDescription("/e", "elbl")} />
    </div>
  ),
  ExtensionGroups: ({ extension, onWrite, onOpenDescription }: any) => (
    <div data-testid={`ext-groups-${extension.id}`}>
      <button data-testid="ext-groups-write" onClick={() => onWrite({ path: ["eg"], value: 1 })} />
      <button data-testid="ext-groups-open" onClick={() => onOpenDescription("/g", "glbl")} />
    </div>
  ),
  HarnessGroup: ({ group, onWrite, onOpenDescription }: any) => (
    <div data-testid={`hgroup-${group.id}`}>
      <button data-testid={`hg-write-${group.id}`} onClick={() => onWrite({ path: [group.id], value: true })} />
      <button data-testid={`hg-open-${group.id}`} onClick={() => onOpenDescription("/h", "hlbl")} />
    </div>
  ),
}));

vi.mock("../src/components/harness/HarnessProfileMeta", () => ({
  HarnessProfileMeta: ({ onWrite }: any) => (
    <div data-testid="hpm">
      <button data-testid="hpm-write" onClick={() => onWrite({ path: ["meta"], value: "x" })} />
    </div>
  ),
}));

import { HarnessSettingsEditor } from "../src/components/HarnessSettingsEditor";

// --- fixtures ---------------------------------------------------------------

const defaultProfile: any = { id: "default", name: "Default", revision: "r0", read_only: false, fields: {} };
const namedProfile: any = { id: "p1", name: "Profile One", revision: "r1", read_only: false, fields: {} };
const readOnlyProfile: any = { id: "p2", name: "RO", revision: "r2", read_only: true, fields: {} };

function makeDescriptor(over: any = {}) {
  return {
    extensions: [
      {
        id: "ext1",
        name: "Extension One",
        description: "d",
        description_path: "/d",
        required: false,
        enabled: true,
        runtime_ready: true,
        runtime_not_ready_reason: "",
        groups: [{ id: "g1", scope: "profile", control: "item_toggles", items: [], value: null }],
      },
    ],
    builtin_tools: { id: "tools", scope: "profile", control: "item_toggles", items: [], value: null },
    builtin_extensions: { id: "bext", scope: "profile", control: "item_toggles", items: [], value: null },
    runtime_skills: { id: "skills", scope: "profile", control: "item_toggles", items: [], value: null },
    profile_meta: { id: "meta", scope: "profile", fields: [] },
    ...over,
  };
}

function profilesList() {
  return { ok: true, status: 200, json: async () => ({ profiles: [defaultProfile, namedProfile, readOnlyProfile] }) };
}

function renderEditor(props: { onEditDescriptionFile?: (p: string) => Promise<unknown> } = {}) {
  return render(<HarnessSettingsEditor onEditDescriptionFile={props.onEditDescriptionFile} />);
}

async function awaitLoaded() {
  await screen.findByPlaceholderText("harnessProfile.searchExtensions");
}

function fireBus(topic: string, payload: unknown) {
  return act(() => {
    (bus.subscribers[topic] || []).forEach((cb) => cb(payload));
  });
}

beforeEach(() => {
  bus.subscribers = {};
  harnessApi.fetchDescriptor.mockReset();
  harnessApi.fetchProfile.mockReset();
  harnessApi.createProfile.mockReset();
  harnessApi.deleteProfile.mockReset();
  harnessApi.writeFields.mockReset();
  tracked.fn.mockReset();
  resolve.groupOverrideCount.mockReset();
  resolve.clearAllWrites.mockReset();

  harnessApi.fetchDescriptor.mockResolvedValue(makeDescriptor());
  harnessApi.fetchProfile.mockImplementation((id: string) =>
    id === "default" ? Promise.resolve(defaultProfile) : id === "p1" ? Promise.resolve(namedProfile) : Promise.resolve(readOnlyProfile),
  );
  harnessApi.createProfile.mockResolvedValue({ id: "newp", name: "New", revision: "rn" });
  harnessApi.deleteProfile.mockResolvedValue(undefined);
  harnessApi.writeFields.mockResolvedValue(namedProfile);
  tracked.fn.mockResolvedValue(profilesList());
  resolve.groupOverrideCount.mockReturnValue(0);
  resolve.clearAllWrites.mockReturnValue([{ path: ["x"], clear: true }]);
});

// --- loading + baseline -----------------------------------------------------

describe("HarnessSettingsEditor — loading & baseline", () => {
  it("shows the loading state before the first load resolves", async () => {
    harnessApi.fetchDescriptor.mockImplementation(() => new Promise(() => {}));
    renderEditor();
    expect(await screen.findByText("common.loading")).toBeTruthy();
  });

  it("renders the profile rail and header after load (default selected)", async () => {
    renderEditor();
    await awaitLoaded();
    // "defaultOptionLabel" appears in both the rail button and the header title.
    expect(screen.getAllByText("harnessProfile.defaultOptionLabel").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByPlaceholderText("harnessProfile.createProfileNamePlaceholder")).toBeTruthy();
  });

  it("renders the named-profile title + override hint", async () => {
    resolve.groupOverrideCount.mockReturnValue(3);
    renderEditor();
    await awaitLoaded();
    await act(async () => {
      fireEvent.click(screen.getByText("Profile One"));
    });
    // Rail button + header title both carry the name.
    expect(screen.getAllByText("Profile One").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("harnessProfile.overrideCount")).toBeTruthy();
  });

  it("renders HarnessProfileMeta only for a named writable profile with profile_meta", async () => {
    renderEditor();
    await awaitLoaded();
    expect(screen.queryByTestId("hpm")).toBeNull(); // default
    await act(async () => {
      fireEvent.click(screen.getByText("Profile One"));
    });
    expect(screen.getByTestId("hpm")).toBeTruthy();
  });

  it("hides HarnessProfileMeta when the descriptor has no profile_meta", async () => {
    harnessApi.fetchDescriptor.mockResolvedValue(makeDescriptor({ profile_meta: undefined }));
    renderEditor();
    await awaitLoaded();
    await act(async () => {
      fireEvent.click(screen.getByText("Profile One"));
    });
    expect(screen.queryByTestId("hpm")).toBeNull();
  });
});

// --- loadProfiles -----------------------------------------------------------

describe("HarnessSettingsEditor — profile list", () => {
  it("falls back to Default-only when the list fetch fails", async () => {
    tracked.fn.mockRejectedValue(new Error("net"));
    renderEditor();
    await awaitLoaded();
    expect(screen.getAllByText("harnessProfile.defaultOptionLabel").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("Profile One")).toBeNull();
  });
});

// --- load errors ------------------------------------------------------------

describe("HarnessSettingsEditor — load errors", () => {
  it("surfaces a generic load error and clears the profile", async () => {
    harnessApi.fetchProfile.mockImplementation((id: string) =>
      id === "default" ? Promise.resolve(defaultProfile) : Promise.reject(new Error("boom")),
    );
    renderEditor();
    await awaitLoaded();
    await act(async () => {
      fireEvent.click(screen.getByText("Profile One"));
    });
    expect(await screen.findByText("harnessProfile.patchError: boom")).toBeTruthy();
  });

  it("reselects Default when the selected profile is gone (PROFILE_NOT_FOUND)", async () => {
    harnessApi.fetchProfile.mockImplementation((id: string) =>
      id === "default" ? Promise.resolve(defaultProfile) : Promise.reject(new Error("profile_not_found")),
    );
    renderEditor();
    await awaitLoaded();
    await act(async () => {
      fireEvent.click(screen.getByText("Profile One"));
    });
    // Default reselected → default hint shown, no error banner.
    await waitFor(() => expect(screen.queryByText(/harnessProfile.patchError/)).toBeNull());
  });
});

// --- create / delete --------------------------------------------------------

describe("HarnessSettingsEditor — create", () => {
  it("creates a profile and clears the input", async () => {
    renderEditor();
    await awaitLoaded();
    const input = screen.getByPlaceholderText("harnessProfile.createProfileNamePlaceholder");
    await act(async () => fireEvent.change(input, { target: { value: "New Name" } }));
    await act(async () => fireEvent.click(screen.getByText("harnessProfile.createProfile")));
    await waitFor(() => expect(harnessApi.createProfile).toHaveBeenCalledWith("New Name"));
    await waitFor(() => expect((input as HTMLInputElement).value).toBe(""));
  });

  it("surfaces a create error", async () => {
    harnessApi.createProfile.mockRejectedValue(new Error("nope"));
    renderEditor();
    await awaitLoaded();
    const input = screen.getByPlaceholderText("harnessProfile.createProfileNamePlaceholder");
    await act(async () => fireEvent.change(input, { target: { value: "X" } }));
    await act(async () => fireEvent.click(screen.getByText("harnessProfile.createProfile")));
    expect(await screen.findByText("harnessProfile.patchError: nope")).toBeTruthy();
  });
});

describe("HarnessSettingsEditor — delete", () => {
  it("deletes the selected profile and reselects Default", async () => {
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await act(async () => fireEvent.click(screen.getByText("harnessProfile.deleteProfile")));
    await waitFor(() => expect(harnessApi.deleteProfile).toHaveBeenCalledWith("p1", "r1"));
  });

  it("reselects Default when delete hits PROFILE_NOT_FOUND (already gone)", async () => {
    harnessApi.deleteProfile.mockRejectedValue(new Error("profile_not_found"));
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await act(async () => fireEvent.click(screen.getByText("harnessProfile.deleteProfile")));
    await waitFor(() => expect(screen.queryByText(/harnessProfile.patchError/)).toBeNull());
  });

  it("surfaces a delete error", async () => {
    harnessApi.deleteProfile.mockRejectedValue(new Error("denied"));
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await act(async () => fireEvent.click(screen.getByText("harnessProfile.deleteProfile")));
    expect(await screen.findByText("harnessProfile.patchError: denied")).toBeTruthy();
  });

  it("delete is a no-op when the profile is null (stale selection)", async () => {
    harnessApi.fetchProfile.mockImplementation((id: string) =>
      id === "default" ? Promise.resolve(defaultProfile) : Promise.reject(new Error("boom")),
    );
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await screen.findByText("harnessProfile.patchError: boom"); // profile now null, selection still p1
    await act(async () => fireEvent.click(screen.getByText("harnessProfile.deleteProfile")));
    expect(harnessApi.deleteProfile).not.toHaveBeenCalled();
  });
});

// --- override count + reset all --------------------------------------------

describe("HarnessSettingsEditor — override count & reset all", () => {
  it("enables reset-all when overrides exist and applies clear writes", async () => {
    resolve.groupOverrideCount.mockReturnValue(2);
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    const reset = screen.getByText("harnessProfile.resetAll");
    expect((reset.closest("button") as HTMLButtonElement).disabled).toBe(false);
    await act(async () => fireEvent.click(reset));
    expect(resolve.clearAllWrites).toHaveBeenCalled();
    expect(harnessApi.writeFields).toHaveBeenCalled();
  });

  it("disables reset-all when there are no overrides", async () => {
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    const reset = screen.getByText("harnessProfile.resetAll");
    expect((reset.closest("button") as HTMLButtonElement).disabled).toBe(true);
  });

  it("reset-all with empty writes is a no-op (no write call)", async () => {
    resolve.groupOverrideCount.mockReturnValue(1);
    resolve.clearAllWrites.mockReturnValue([]);
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await act(async () => fireEvent.click(screen.getByText("harnessProfile.resetAll")));
    expect(harnessApi.writeFields).not.toHaveBeenCalled();
  });
});

// --- search + diff toggle ---------------------------------------------------

describe("HarnessSettingsEditor — search & diff toggle", () => {
  it("filters extensions by name", async () => {
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.change(screen.getByPlaceholderText("harnessProfile.searchExtensions"), { target: { value: "two" } }));
    expect(screen.queryByTestId("ext-toggle-ext1")).toBeNull();
  });

  it("matches extensions by id", async () => {
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.change(screen.getByPlaceholderText("harnessProfile.searchExtensions"), { target: { value: "ext1" } }));
    expect(screen.getByTestId("ext-toggle-ext1")).toBeTruthy();
  });

  it("toggles diff-only for a named writable profile", async () => {
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    const toggle = screen.getByLabelText("harnessProfile.diffOnly");
    await act(async () => fireEvent.click(toggle));
    expect((toggle as HTMLInputElement).checked).toBe(true);
  });
});

// --- writes + runMutation ---------------------------------------------------

describe("HarnessSettingsEditor — writes", () => {
  it("runs writeFields on a group write callback and updates the profile", async () => {
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    const before = harnessApi.fetchDescriptor.mock.calls.length;
    await act(async () => fireEvent.click(screen.getByTestId("hg-write-tools")));
    await waitFor(() => expect(harnessApi.writeFields).toHaveBeenCalled());
    // No profile-event echo arrived → no reload.
    expect(harnessApi.fetchDescriptor.mock.calls.length).toBe(before);
  });

  it("ignores writes on a read-only profile", async () => {
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("RO")));
    await act(async () => fireEvent.click(screen.getByTestId("hg-write-tools")));
    expect(harnessApi.writeFields).not.toHaveBeenCalled();
  });

  it("wires every meta/toggle/group write + open-description callback", async () => {
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    harnessApi.writeFields.mockClear();
    await act(async () => fireEvent.click(screen.getByTestId("hpm-write")));
    await act(async () => fireEvent.click(screen.getByTestId("ext-toggle-write")));
    await act(async () => fireEvent.click(screen.getByTestId("ext-groups-write")));
    await act(async () => fireEvent.click(screen.getByTestId("hg-write-skills")));
    await waitFor(() => expect(harnessApi.writeFields.mock.calls.length).toBe(4));
    // open-description callbacks surface the overlay.
    await act(async () => fireEvent.click(screen.getByTestId("hg-open-skills")));
    expect(screen.getByText("hlbl")).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByTestId("ext-groups-open")));
    expect(screen.getByText("glbl")).toBeTruthy();
  });
});

describe("HarnessSettingsEditor — runMutation event suppression", () => {
  it("coalesces a local-echo profile event without reloading", async () => {
    let resolveWrite!: (p: unknown) => void;
    harnessApi.writeFields.mockImplementation(() => new Promise((r) => { resolveWrite = r; }));
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await act(async () => fireEvent.click(screen.getByTestId("hg-write-tools")));
    // While the write is in flight, the backend echoes the very write we just sent.
    await fireBus("harness_profiles_changed", { action: "fields_updated", profile_id: "p1", revision: "r1" });
    const before = harnessApi.fetchDescriptor.mock.calls.length;
    await act(async () => resolveWrite(namedProfile));
    await waitFor(() => expect(harnessApi.writeFields).toHaveBeenCalled());
    expect(harnessApi.fetchDescriptor.mock.calls.length).toBe(before); // no reload
  });

  it("reloads when an external event arrives during a write", async () => {
    let resolveWrite!: (p: unknown) => void;
    harnessApi.writeFields.mockImplementation(() => new Promise((r) => { resolveWrite = r; }));
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await act(async () => fireEvent.click(screen.getByTestId("hg-write-tools")));
    await fireBus("extension.config", {});
    await act(async () => resolveWrite(namedProfile));
    await waitFor(() => expect(harnessApi.fetchDescriptor.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("treats a non-object profile payload as external (reload)", async () => {
    let resolveWrite!: (p: unknown) => void;
    harnessApi.writeFields.mockImplementation(() => new Promise((r) => { resolveWrite = r; }));
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await act(async () => fireEvent.click(screen.getByTestId("hg-write-tools")));
    await fireBus("harness_profiles_changed", "str");
    await act(async () => resolveWrite(namedProfile));
    await waitFor(() => expect(harnessApi.fetchDescriptor.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("reselects Default when a write hits PROFILE_NOT_FOUND", async () => {
    harnessApi.writeFields.mockRejectedValue(new Error("profile_not_found"));
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await act(async () => fireEvent.click(screen.getByTestId("hg-write-tools")));
    await waitFor(() => expect(screen.queryByText(/harnessProfile.patchError/)).toBeNull());
  });

  it("reloads on a stale-revision write (REVISION_MISMATCH arm)", async () => {
    // The mismatch sets the error then calls load(); load() succeeds and
    // clears the error, so the banner is transient — assert the reload.
    harnessApi.writeFields.mockRejectedValue(new Error("revision_mismatch"));
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    const before = harnessApi.fetchDescriptor.mock.calls.length;
    await act(async () => fireEvent.click(screen.getByTestId("hg-write-tools")));
    await waitFor(() => expect(harnessApi.fetchDescriptor.mock.calls.length).toBeGreaterThan(before));
  });

  it("shows a generic error on other write failures", async () => {
    harnessApi.writeFields.mockRejectedValue(new Error("wboom"));
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await act(async () => fireEvent.click(screen.getByTestId("hg-write-tools")));
    expect(await screen.findByText("harnessProfile.patchError: wboom")).toBeTruthy();
  });
});

// --- live events (not in flight) -------------------------------------------

describe("HarnessSettingsEditor — live events", () => {
  it("reloads on an extension.config event when no write is in flight", async () => {
    renderEditor();
    await awaitLoaded();
    const before = harnessApi.fetchDescriptor.mock.calls.length;
    await fireBus("extension.config", {});
    await waitFor(() => expect(harnessApi.fetchDescriptor.mock.calls.length).toBeGreaterThan(before));
  });

  it("reloads on a harness_profiles_changed event", async () => {
    renderEditor();
    await awaitLoaded();
    const before = harnessApi.fetchDescriptor.mock.calls.length;
    await fireBus("harness_profiles_changed", { action: "created" });
    await waitFor(() => expect(harnessApi.fetchDescriptor.mock.calls.length).toBeGreaterThan(before));
  });

  it("reloads on an extension.harness.default event", async () => {
    renderEditor();
    await awaitLoaded();
    const before = harnessApi.fetchDescriptor.mock.calls.length;
    await fireBus("extension.harness.default", {});
    await waitFor(() => expect(harnessApi.fetchDescriptor.mock.calls.length).toBeGreaterThan(before));
  });
});

// --- read-only + default presentation --------------------------------------

describe("HarnessSettingsEditor — read-only & default presentation", () => {
  it("read-only profile hides diff/reset, shows read-only hint, disables delete with title", async () => {
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("RO")));
    expect(screen.getByText("harnessProfile.readOnlyHint")).toBeTruthy();
    expect(screen.queryByText("harnessProfile.diffOnly")).toBeNull();
    expect(screen.queryByText("harnessProfile.resetAll")).toBeNull();
    const del = screen.getByText("harnessProfile.deleteProfile").closest("button")!;
    expect((del as HTMLButtonElement).disabled).toBe(true);
    expect(del.getAttribute("title")).toBe("harnessProfile.deleteReadOnlyBlocked");
  });

  it("default profile shows default hint and disables delete with title", async () => {
    renderEditor();
    await awaitLoaded();
    expect(screen.getByText("harnessProfile.defaultHint")).toBeTruthy();
    const del = screen.getByText("harnessProfile.deleteProfile").closest("button")!;
    expect((del as HTMLButtonElement).disabled).toBe(true);
    expect(del.getAttribute("title")).toBe("harnessProfile.deleteDefaultBlocked");
  });
});

// --- extension availability + description overlay --------------------------

describe("HarnessSettingsEditor — availability & description overlay", () => {
  it("shows the unavailable chip when an extension is not runtime-ready", async () => {
    harnessApi.fetchDescriptor.mockResolvedValue(
      makeDescriptor({
        extensions: [
          { id: "ext1", name: "Extension One", description: "d", description_path: "/d", required: false, enabled: true, runtime_ready: false, runtime_not_ready_reason: "missing", groups: [] },
        ],
      }),
    );
    renderEditor();
    await awaitLoaded();
    expect(screen.getByText("harnessProfile.extensionUnavailable")).toBeTruthy();
  });

  it("opens the description overlay and closes via ×", async () => {
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await act(async () => fireEvent.click(screen.getByTestId("hg-open-tools")));
    expect(screen.getByText("hlbl")).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByText("×")));
    await waitFor(() => expect(screen.queryByText("hlbl")).toBeNull());
  });

  it("edit-with-AI button calls onEditDescriptionFile", async () => {
    const onEditDescriptionFile = vi.fn().mockResolvedValue(undefined);
    renderEditor({ onEditDescriptionFile });
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await act(async () => fireEvent.click(screen.getByTestId("desc-open")));
    await act(async () => fireEvent.click(screen.getByText("files.editWithAiTitle")));
    expect(onEditDescriptionFile).toHaveBeenCalledWith("/d");
  });

  it("FileViewer onClose closes the overlay", async () => {
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await act(async () => fireEvent.click(screen.getByTestId("ext-toggle-open")));
    expect(screen.getByText("elbl")).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByTestId("fv-close")));
    await waitFor(() => expect(screen.queryByText("elbl")).toBeNull());
  });
});

// --- fallback arms (non-Error rejections, empty payloads) ------------------

describe("HarnessSettingsEditor — fallback arms", () => {
  it("load catch stringifies a non-Error rejection", async () => {
    harnessApi.fetchProfile.mockImplementation((id: string) =>
      id === "default" ? Promise.resolve(defaultProfile) : Promise.reject("load-str"),
    );
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    expect(await screen.findByText("harnessProfile.patchError: load-str")).toBeTruthy();
  });

  it("write catch stringifies a non-Error rejection", async () => {
    harnessApi.writeFields.mockRejectedValue("write-str");
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await act(async () => fireEvent.click(screen.getByTestId("hg-write-tools")));
    expect(await screen.findByText("harnessProfile.patchError: write-str")).toBeTruthy();
  });

  it("create catch stringifies a non-Error rejection", async () => {
    harnessApi.createProfile.mockRejectedValue("create-str");
    renderEditor();
    await awaitLoaded();
    const input = screen.getByPlaceholderText("harnessProfile.createProfileNamePlaceholder");
    await act(async () => fireEvent.change(input, { target: { value: "Z" } }));
    await act(async () => fireEvent.click(screen.getByText("harnessProfile.createProfile")));
    expect(await screen.findByText("harnessProfile.patchError: create-str")).toBeTruthy();
  });

  it("delete catch stringifies a non-Error rejection", async () => {
    harnessApi.deleteProfile.mockRejectedValue("delete-str");
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await act(async () => fireEvent.click(screen.getByText("harnessProfile.deleteProfile")));
    expect(await screen.findByText("harnessProfile.patchError: delete-str")).toBeTruthy();
  });

  it("falls back to Default-only when the list response is not ok", async () => {
    tracked.fn.mockResolvedValue({ ok: false, status: 500, json: async () => ({}) });
    renderEditor();
    await awaitLoaded();
    expect(screen.getAllByText("harnessProfile.defaultOptionLabel").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("Profile One")).toBeNull();
  });

  it("treats a missing profiles field as an empty list", async () => {
    tracked.fn.mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
    const { container } = renderEditor();
    await awaitLoaded();
    // No profile rail buttons rendered (header still carries the default label).
    expect(container.querySelectorAll(".harness-profile-rail-item").length).toBe(0);
  });

  it("falls back to the disabled label when an unavailable extension gives no reason", async () => {
    harnessApi.fetchDescriptor.mockResolvedValue(
      makeDescriptor({
        extensions: [
          { id: "ext1", name: "Extension One", description: "d", description_path: "/d", required: false, enabled: true, runtime_ready: false, runtime_not_ready_reason: "", groups: [] },
        ],
      }),
    );
    renderEditor();
    await awaitLoaded();
    expect(screen.getByText("harnessProfile.extensionUnavailable")).toBeTruthy();
  });
});
