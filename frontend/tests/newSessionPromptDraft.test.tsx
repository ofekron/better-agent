import { fireEvent, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  NEW_SESSION_PROMPT_TESTID,
  NewSessionModal,
} from "../src/components/NewSessionModal";
import type { Provider } from "../src/types";
import { cacheProviders } from "../src/utils/providerCache";

vi.mock("../src/hooks/useMachines", () => ({
  useMachines: () => ({ machines: [] }),
}));

vi.mock("../src/hooks/useLocalNodeId", () => ({
  useLocalNodeId: () => "primary",
}));

const provider: Provider = {
  id: "cached-claude",
  name: "Cached Claude",
  kind: "claude",
  mode: "subscription",
  base_url: "",
  config_dir: "",
  custom_models: [],
  default_model: "cached-default",
  runner: "native",
  runner_options: ["native"],
  suspended: false,
  reasoning_effort_options: ["low", "medium", "high", "xhigh"],
  default_reasoning_effort: "medium",
  permission_options: {},
  default_permission: {},
  has_api_key: false,
  supports_fork: true,
  supports_manager_mode: true,
  supports_rewind: true,
  supports_steering: true,
  supports_native_subagents: false,
  supports_reasoning_effort: true,
  capability_overrides: {},
};

const capabilityPickerClient = {
  listCapabilityPickerSources: vi.fn(async () => ({ sources: [] })),
};

function renderModal(open: boolean, onClose: () => void, onCreate: () => void = vi.fn()) {
  return render(
    <NewSessionModal
      open={open}
      onClose={onClose}
      onCreate={onCreate}
      defaultCwd="/tmp/project"
      projects={[]}
      capabilityPickerClient={capabilityPickerClient}
    />,
  );
}

async function promptTextarea(view: ReturnType<typeof renderModal>) {
  await waitFor(() => {
    expect(view.getByTestId(NEW_SESSION_PROMPT_TESTID)).toBeTruthy();
  });
  return view.getByTestId(NEW_SESSION_PROMPT_TESTID) as HTMLTextAreaElement;
}

describe("NewSessionModal prompt draft", () => {
  beforeEach(() => {
    localStorage.clear();
    cacheProviders([provider], provider.id);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
  });

  it("restores unsent text after the modal is dismissed and reopened", async () => {
    const first = renderModal(true, () => {});
    fireEvent.change(await promptTextarea(first), { target: { value: "half written" } });
    first.unmount();

    const second = renderModal(true, () => {});
    expect((await promptTextarea(second)).value).toBe("half written");
    second.unmount();
  });

  it("asks before losing text on explicit cancel and keeps it when asked", async () => {
    const onClose = vi.fn();
    const view = renderModal(true, onClose);
    fireEvent.change(await promptTextarea(view), { target: { value: "keep me" } });

    fireEvent.click(view.getByRole("button", { name: "newSession.cancel" }));
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(view.getByRole("button", { name: "newSession.keepDraftKeep" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    view.unmount();

    const reopened = renderModal(true, () => {});
    expect((await promptTextarea(reopened)).value).toBe("keep me");
    reopened.unmount();
  });

  it("discards the text when the user chooses discard", async () => {
    const onClose = vi.fn();
    const view = renderModal(true, onClose);
    fireEvent.change(await promptTextarea(view), { target: { value: "throw me away" } });

    fireEvent.click(view.getByRole("button", { name: "newSession.cancel" }));
    fireEvent.click(view.getByRole("button", { name: "newSession.keepDraftDiscard" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    view.unmount();

    const reopened = renderModal(true, () => {});
    expect((await promptTextarea(reopened)).value).toBe("");
    reopened.unmount();
  });

  it("never closes on a click outside the modal", async () => {
    const onClose = vi.fn();
    const view = renderModal(true, onClose);
    await promptTextarea(view);

    const overlay = view.container.querySelector(".ns-session-overlay") as HTMLElement;
    fireEvent.click(overlay);
    expect(onClose).not.toHaveBeenCalled();
    view.unmount();
  });

  it("returns to the modal when the keep/discard question is dismissed", async () => {
    const onClose = vi.fn();
    const view = renderModal(true, onClose);
    fireEvent.change(await promptTextarea(view), { target: { value: "still deciding" } });

    fireEvent.click(view.getByRole("button", { name: "newSession.cancel" }));
    const confirmOverlay = view.container.querySelectorAll(".modal-overlay")[1] as HTMLElement;
    fireEvent.click(confirmOverlay);

    expect(onClose).not.toHaveBeenCalled();
    expect(view.queryByRole("button", { name: "newSession.keepDraftDiscard" })).toBeNull();
    expect((await promptTextarea(view)).value).toBe("still deciding");
    view.unmount();
  });

  it("keeps Enter as a newline instead of creating the session", async () => {
    const onCreate = vi.fn();
    const view = renderModal(true, () => {}, onCreate);
    const textarea = await promptTextarea(view);
    fireEvent.change(textarea, { target: { value: "first line" } });

    const notPrevented = fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    expect(notPrevented).toBe(true);
    expect(onCreate).not.toHaveBeenCalled();
    view.unmount();
  });

  it("closes immediately when there is no text", async () => {
    const onClose = vi.fn();
    const view = renderModal(true, onClose);
    await promptTextarea(view);

    fireEvent.click(view.getByRole("button", { name: "newSession.cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(view.queryByRole("button", { name: "newSession.keepDraftDiscard" })).toBeNull();
    view.unmount();
  });
});
