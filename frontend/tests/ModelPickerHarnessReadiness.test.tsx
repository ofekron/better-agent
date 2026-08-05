import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { makeProvider, makeRuntimeProfile, makeRuntimeProfilesSnapshot, makeSession } from "./fixtures";

const harness = vi.hoisted(() => ({
  onReadyChange: undefined as undefined | ((ready: boolean) => void),
}));

const runtimeProfile = makeRuntimeProfile({
  id: "runtime-profile",
  provider_id: "provider",
  default_model: "model",
});
const snapshot = makeRuntimeProfilesSnapshot({
  runtime_profiles: [runtimeProfile],
  default_runtime_profile_id: runtimeProfile.id,
});

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock("../src/hooks/useQuotaStatus", () => ({ useQuotaStatus: () => ({}) }));

vi.mock("../src/hooks/useProviderModelCatalog", () => ({
  useProviderModelCatalog: () => ({
    catalog: { status: "ready", models: ["model"], retired: [], runtime_profiles: [] },
    networkState: "online",
    loading: false,
    refresh: vi.fn(),
    refreshing: false,
    refreshError: null,
  }),
}));

vi.mock("../src/hooks/useRuntimeProfiles", () => ({
  useRuntimeProfiles: () => ({ snapshot, profiles: snapshot.runtime_profiles }),
}));

vi.mock("../src/components/ModelCatalogStatus", () => ({
  ModelCatalogStatus: () => null,
}));

vi.mock("../src/components/HarnessProfileSelector", () => ({
  HarnessProfileSelector: ({
    onChange,
    onReadyChange,
  }: {
    onChange: (value: string) => void;
    onReadyChange?: (ready: boolean) => void;
  }) => {
    harness.onReadyChange = onReadyChange;
    return <button onClick={() => onChange("default")}>choose-default-harness</button>;
  },
}));

import { ModelPickerModal } from "../src/components/ModelPickerModal";

describe("ModelPickerModal harness readiness", () => {
  beforeEach(() => {
    harness.onReadyChange = undefined;
  });

  it("blocks confirmation until the harness selection is validated", () => {
    const onConfirm = vi.fn();
    render(
      <ModelPickerModal
        session={makeSession({
          runtime_profile_id: runtimeProfile.id,
          provider_id: "provider",
          runner: "native",
          model: "model",
          harness_profile_id: "deleted-profile",
        })}
        providers={[makeProvider({ id: "provider" })]}
        onConfirm={onConfirm}
        onClose={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "choose-default-harness" }));
    const confirm = screen.getByRole("button", { name: "common.ok" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();

    act(() => harness.onReadyChange?.(true));
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);

    expect(onConfirm).toHaveBeenCalledWith({ harness_profile_id: "" });
  });
});
