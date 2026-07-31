import { fireEvent, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  NEW_SESSION_PROMPT_TESTID,
  NewSessionModal,
} from "../src/components/NewSessionModal";
import type { Provider } from "../src/types";
import { cacheProviders } from "../src/utils/providerCache";
import { cacheRuntimeProfilesSnapshot } from "../src/hooks/useRuntimeProfiles";
import { makeProvider, makeRuntimeProfile, makeRuntimeProfilesSnapshot } from "./fixtures";

vi.mock("../src/hooks/useMachines", () => ({
  useMachines: () => ({ machines: [] }),
}));

vi.mock("../src/hooks/useLocalNodeId", () => ({
  useLocalNodeId: () => "primary",
}));

const provider: Provider = makeProvider({
  id: "cached-claude",
  name: "Cached Claude",
});

function renderModal(open: boolean, onClose: () => void, onCreate: () => void = vi.fn()) {
  return render(
    <NewSessionModal
      open={open}
      onClose={onClose}
      onCreate={onCreate}
      defaultCwd="/tmp/project"
      projects={[]}
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
    cacheRuntimeProfilesSnapshot(makeRuntimeProfilesSnapshot({
      runtime_profiles: [makeRuntimeProfile({
        id: "rp-cached",
        provider_id: provider.id,
        name: "Cached Claude",
        default_model: "cached-default",
      })],
      default_runtime_profile_id: "rp-cached",
    }));
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

  it("uses Enter to create with the selected action", async () => {
    const onCreate = vi.fn();
    const view = renderModal(true, () => {}, onCreate);
    const textarea = await promptTextarea(view);
    fireEvent.change(textarea, { target: { value: "first line" } });

    const notPrevented = fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    expect(notPrevented).toBe(false);
    await waitFor(() => {
      expect(onCreate).toHaveBeenCalledWith(
        expect.objectContaining({ initialPrompt: "first line" }),
        undefined,
        "send-and-open",
      );
    });
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

  it("restores a draft attachment after the modal is dismissed and reopened", async () => {
    const first = renderModal(true, () => {});
    await promptTextarea(first);
    const fileInput = first.getByTestId("new-session-attachment-input") as HTMLInputElement;
    const file = new File(["hello"], "notes.txt", { type: "text/plain" });
    fireEvent.change(fileInput, { target: { files: [file] } });
    await waitFor(() => {
      expect(first.getByText("notes.txt")).toBeTruthy();
    });
    first.unmount();

    const second = renderModal(true, () => {});
    await promptTextarea(second);
    await waitFor(() => {
      expect(second.getByText("notes.txt")).toBeTruthy();
    });
    second.unmount();
  });

  it("clears the draft attachment once the create it fed into succeeds", async () => {
    const onCreate = vi.fn().mockResolvedValue(true);
    const view = renderModal(true, () => {}, onCreate);
    await promptTextarea(view);
    const fileInput = view.getByTestId("new-session-attachment-input") as HTMLInputElement;
    const file = new File(["hello"], "notes.txt", { type: "text/plain" });
    fireEvent.change(fileInput, { target: { files: [file] } });
    await waitFor(() => {
      expect(view.getByText("notes.txt")).toBeTruthy();
    });

    fireEvent.click(view.getByRole("button", { name: "newSession.createAndSendAndOpen" }));
    await waitFor(() => {
      expect(onCreate).toHaveBeenCalled();
    });
    view.unmount();

    const reopened = renderModal(true, () => {});
    await promptTextarea(reopened);
    expect(reopened.queryByText("notes.txt")).toBeNull();
    reopened.unmount();
  });
});
