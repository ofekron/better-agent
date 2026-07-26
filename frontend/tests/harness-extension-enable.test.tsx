import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { ExtensionEnabledToggle } from "../src/components/harness/HarnessGroup";
import {
  GROUP_DISABLED_BUILTIN_EXTENSIONS,
  type HarnessDescriptorGroup,
} from "../src/components/harness/types";
import type { HarnessProfile } from "../src/types";

const EXT = "ofek-dev.coordination";

function profileWith(disabled: string[]): HarnessProfile {
  return {
    id: "work",
    name: "Work",
    revision: 3,
    fields: {
      extension_instances: {},
      [GROUP_DISABLED_BUILTIN_EXTENSIONS]: {
        resolved: disabled,
        override: { add: [], remove: [] },
      },
    },
  } as unknown as HarnessProfile;
}

const group: HarnessDescriptorGroup = {
  id: GROUP_DISABLED_BUILTIN_EXTENSIONS,
  scope: "profile",
  control: "item_toggles",
  items: [
    { name: EXT, label: EXT, description: "", detail: "", default_enabled: true, locked_by: [] },
    { name: "ofek-dev.other", label: "ofek-dev.other", description: "", detail: "", default_enabled: true, locked_by: [] },
  ],
  value: null,
} as unknown as HarnessDescriptorGroup;

function renderToggle(
  extensionId: string,
  onWrite: (write: unknown) => void,
  opts: { disabledList?: string[]; diffOnly?: boolean } = {},
) {
  return render(
    <ExtensionEnabledToggle
      group={group}
      profile={profileWith(opts.disabledList ?? [])}
      extensionId={extensionId}
      isDefault={false}
      disabled={false}
      diffOnly={opts.diffOnly ?? false}
      onWrite={onWrite}
    />,
  );
}

describe("extension enable toggle on the extension card", () => {
  it("renders the profile-scoped switch for the card's own extension", () => {
    renderToggle(EXT, () => {});
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(true);
    expect(screen.getByText("Enabled in this profile")).toBeTruthy();
  });

  it("reflects the disable-list as unchecked", () => {
    renderToggle(EXT, () => {}, { disabledList: [EXT] });
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(false);
  });

  // The control moved onto the card, but the field it writes is still the
  // top-level disable list — not an extension_instances leaf.
  it("writes the top-level disabled_builtin_extensions path", () => {
    const onWrite = vi.fn();
    renderToggle(EXT, onWrite);
    fireEvent.click(screen.getByRole("checkbox"));
    expect(onWrite).toHaveBeenCalledWith({
      path: [GROUP_DISABLED_BUILTIN_EXTENSIONS, EXT],
      value: false,
    });
  });

  it("renders nothing when the group has no item for this extension", () => {
    const { container } = renderToggle("ofek-dev.absent", () => {});
    expect(container.firstChild).toBeNull();
  });

  it("is hidden in diff-only mode while the extension inherits", () => {
    const { container } = renderToggle(EXT, () => {}, { diffOnly: true });
    expect(container.firstChild).toBeNull();
  });
});
