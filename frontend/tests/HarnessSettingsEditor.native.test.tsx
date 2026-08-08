import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";

// Dedicated coverage for HarnessSettingsEditor.tsx's NATIVE (v2
// `save_harness_profile`/`delete_harness_profile` intent) mutation paths —
// `HarnessSettingsEditor.test.tsx` exercises the legacy REST fallback
// exclusively (the broad suite's global `lib/systemFeedRegistry` stub
// always returns `null`/never-resolves, per `tests/setup.ts`); this file
// overrides that stub with a controllable fake so `submitSystemIntent`/
// `submitSystemIntentAwaitingUpsert` resolve like the real `/ws/v2/surface`
// connection would, proving the native branch itself (not just its REST
// fallback) is wired correctly.

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
const resolveApi = vi.hoisted(() => ({
  groupOverrideCount: vi.fn(() => 0),
  clearAllWrites: vi.fn(() => []),
}));
const systemFeed = vi.hoisted(() => ({
  submitSystemIntent: vi.fn(),
  submitSystemIntentAwaitingUpsert: vi.fn(),
}));

vi.mock("react-i18next", () => {
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
  groupOverrideCount: (...a: unknown[]) => resolveApi.groupOverrideCount(...a),
  clearAllWrites: (...a: unknown[]) => resolveApi.clearAllWrites(...a),
}));

vi.mock("../src/components/harness/HarnessGroup", () => ({
  DescriptionLink: () => <div data-testid="desc-link" />,
  ExtensionEnabledToggle: () => <div data-testid="ext-toggle" />,
  ExtensionGroups: () => <div data-testid="ext-groups" />,
  HarnessGroup: (
    { group, onWrite }: { group: { id: string }; onWrite: (write: { path: string[]; value: boolean }) => void },
  ) => (
    <div data-testid={`hgroup-${group.id}`}>
      <button data-testid={`hg-write-${group.id}`} onClick={() => onWrite({ path: [group.id], value: true })} />
    </div>
  ),
}));

vi.mock("../src/components/harness/HarnessProfileMeta", () => ({
  HarnessProfileMeta: () => <div data-testid="hpm" />,
}));

// The subject of this file: a controllable fake, NOT the broad suite's
// always-null stub — `submitSystemIntent`/`submitSystemIntentAwaitingUpsert`
// resolve per-test via `systemFeed.*.mockResolvedValue(...)`.
vi.mock("../src/lib/systemFeedRegistry", () => ({
  SYSTEM_FEED_NAMES: [],
  subscribeSystemFrames: () => () => {},
  subscribeSystemSocketConnection: () => () => {},
  isSystemSocketOpen: () => true,
  submitSystemIntent: (...a: unknown[]) => systemFeed.submitSystemIntent(...a),
  waitForSystemFrame: () => new Promise(() => {}),
  submitSystemIntentAwaitingUpsert: (...a: unknown[]) => systemFeed.submitSystemIntentAwaitingUpsert(...a),
}));

import { HarnessSettingsEditor } from "../src/components/HarnessSettingsEditor";

interface FakeProfile {
  id: string;
  name: string;
  revision: string;
  read_only: boolean;
  fields: Record<string, unknown>;
}

const defaultProfile: FakeProfile = { id: "default", name: "Default", revision: "r0", read_only: false, fields: {} };
const namedProfile: FakeProfile = { id: "p1", name: "Profile One", revision: "r1", read_only: false, fields: {} };

function makeDescriptor(over: Record<string, unknown> = {}) {
  return {
    extensions: [],
    builtin_tools: { id: "tools", scope: "profile", control: "item_toggles", items: [], value: null },
    builtin_extensions: { id: "bext", scope: "profile", control: "item_toggles", items: [], value: null },
    runtime_skills: { id: "skills", scope: "profile", control: "item_toggles", items: [], value: null },
    profile_meta: { id: "meta", scope: "profile", fields: [] },
    ...over,
  };
}

function profilesList() {
  return { ok: true, status: 200, json: async () => ({ profiles: [defaultProfile, namedProfile] }) };
}

function renderEditor() {
  return render(<HarnessSettingsEditor />);
}

async function awaitLoaded() {
  await screen.findByPlaceholderText("harnessProfile.searchExtensions");
}

beforeEach(() => {
  bus.subscribers = {};
  harnessApi.fetchDescriptor.mockReset();
  harnessApi.fetchProfile.mockReset();
  harnessApi.createProfile.mockReset();
  harnessApi.deleteProfile.mockReset();
  harnessApi.writeFields.mockReset();
  tracked.fn.mockReset();
  resolveApi.groupOverrideCount.mockReset();
  resolveApi.clearAllWrites.mockReset();
  systemFeed.submitSystemIntent.mockReset();
  systemFeed.submitSystemIntentAwaitingUpsert.mockReset();

  harnessApi.fetchDescriptor.mockResolvedValue(makeDescriptor());
  harnessApi.fetchProfile.mockImplementation((id: string) =>
    id === "default" ? Promise.resolve(defaultProfile) : Promise.resolve(namedProfile),
  );
  tracked.fn.mockResolvedValue(profilesList());
  resolveApi.groupOverrideCount.mockReturnValue(0);
});

describe("HarnessSettingsEditor — native write (save_harness_profile)", () => {
  it("submits the intent (not writeFields) and re-derives the profile via fetchProfile on accept", async () => {
    systemFeed.submitSystemIntent.mockResolvedValue({ type: "intent_accepted", intent_id: "i1" });
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));

    await act(async () => fireEvent.click(screen.getByTestId("hg-write-tools")));

    await waitFor(() => expect(systemFeed.submitSystemIntent).toHaveBeenCalledTimes(1));
    const call = systemFeed.submitSystemIntent.mock.calls[0][0];
    expect(call).toMatchObject({
      kind: "save_harness_profile",
      harness_profile_id: "p1",
      revision: "r1",
    });
    expect(harnessApi.writeFields).not.toHaveBeenCalled();
    // fetchProfile called again (post-accept re-derive) beyond the initial load.
    await waitFor(() =>
      expect(harnessApi.fetchProfile.mock.calls.filter((c: unknown[]) => c[0] === "p1").length).toBeGreaterThanOrEqual(2),
    );
  });

  it("maps a stale_revision rejection onto the existing REVISION_MISMATCH reload arm", async () => {
    systemFeed.submitSystemIntent.mockResolvedValue({
      type: "intent_rejected", intent_id: "i1", code: "stale_revision", message: "changed",
    });
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    const descriptorCallsBefore = harnessApi.fetchDescriptor.mock.calls.length;

    await act(async () => fireEvent.click(screen.getByTestId("hg-write-tools")));

    await waitFor(() => expect(systemFeed.submitSystemIntent).toHaveBeenCalledTimes(1));
    expect(harnessApi.writeFields).not.toHaveBeenCalled();
    // REVISION_MISMATCH's existing handling calls load() (a `fetchDescriptor` reload).
    await waitFor(() => expect(harnessApi.fetchDescriptor.mock.calls.length).toBeGreaterThan(descriptorCallsBefore));
  });
});

describe("HarnessSettingsEditor — native create (save_harness_profile, no id)", () => {
  it("selects the id observed on the intent_id-stamped harness_profile_upsert frame", async () => {
    systemFeed.submitSystemIntentAwaitingUpsert.mockResolvedValue({
      ok: true,
      frame: {
        type: "harness_profile_upsert",
        cv: 2,
        intent_id: "i2",
        profile: {
          harness_profile_id: "new-1", cv: 2, display: "New", is_default: false,
          disabled_builtin_extensions: [], disabled_builtin_tools: [], config_schema: null,
        },
      },
    });
    renderEditor();
    await awaitLoaded();
    const input = screen.getByPlaceholderText("harnessProfile.createProfileNamePlaceholder");
    await act(async () => fireEvent.change(input, { target: { value: "New Name" } }));
    await act(async () => fireEvent.click(screen.getByText("harnessProfile.createProfile")));

    await waitFor(() => expect(systemFeed.submitSystemIntentAwaitingUpsert).toHaveBeenCalledTimes(1));
    expect(harnessApi.createProfile).not.toHaveBeenCalled();
    // Selecting "new-1" triggers a fetchProfile("new-1") via the load effect.
    await waitFor(() =>
      expect(harnessApi.fetchProfile.mock.calls.some((c: unknown[]) => c[0] === "new-1")).toBe(true),
    );
  });

  it("surfaces an error when the upsert never arrives (frame undefined)", async () => {
    systemFeed.submitSystemIntentAwaitingUpsert.mockResolvedValue({ ok: true, frame: undefined });
    renderEditor();
    await awaitLoaded();
    const input = screen.getByPlaceholderText("harnessProfile.createProfileNamePlaceholder");
    await act(async () => fireEvent.change(input, { target: { value: "X" } }));
    await act(async () => fireEvent.click(screen.getByText("harnessProfile.createProfile")));
    expect(await screen.findByText(/harnessProfile.patchError/)).toBeTruthy();
  });
});

describe("HarnessSettingsEditor — native delete (delete_harness_profile)", () => {
  it("submits the intent (not deleteProfile) and reselects Default on accept", async () => {
    systemFeed.submitSystemIntent.mockResolvedValue({ type: "intent_accepted", intent_id: "i3" });
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await act(async () => fireEvent.click(screen.getByText("harnessProfile.deleteProfile")));

    await waitFor(() => expect(systemFeed.submitSystemIntent).toHaveBeenCalledWith({
      kind: "delete_harness_profile", harness_profile_id: "p1", revision: "r1",
    }));
    expect(harnessApi.deleteProfile).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText("harnessProfile.defaultHint")).toBeTruthy());
  });

  it("maps a 404 rejection onto the existing PROFILE_NOT_FOUND reselect arm", async () => {
    systemFeed.submitSystemIntent.mockResolvedValue({
      type: "intent_rejected", intent_id: "i3", code: "404", message: "harness profile not found",
    });
    renderEditor();
    await awaitLoaded();
    await act(async () => fireEvent.click(screen.getByText("Profile One")));
    await act(async () => fireEvent.click(screen.getByText("harnessProfile.deleteProfile")));

    expect(harnessApi.deleteProfile).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText("harnessProfile.defaultHint")).toBeTruthy());
    expect(screen.queryByText(/harnessProfile.patchError/)).toBeNull();
  });
});
