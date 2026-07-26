import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import "../src/i18n";
import { HarnessGroup } from "../src/components/harness/HarnessGroup";
import type { HarnessDescriptorGroup } from "../src/components/harness/types";
import type { HarnessProfile } from "../src/types";

const profile = {
  id: "default",
  name: "Default",
  revision: 1,
  fields: { extension_instances: {} },
} as unknown as HarnessProfile;

function group(overrides: Partial<HarnessDescriptorGroup>): HarnessDescriptorGroup {
  return {
    id: "permissions",
    scope: "global",
    control: "item_toggles",
    items: [],
    value: null,
    ...overrides,
  };
}

function renderGroup(descriptorGroup: HarnessDescriptorGroup) {
  return render(
    <HarnessGroup
      group={descriptorGroup}
      profile={profile}
      extensionId="ext"
      isDefault
      disabled={false}
      diffOnly={false}
      onWrite={() => {}}
    />,
  );
}

describe("harness control explanations", () => {
  it("explains what a group configures alongside its toggles", () => {
    renderGroup(
      group({
        id: "skills",
        scope: "profile",
        items: [{ name: "deploy", label: "deploy", description: "", default_enabled: true }],
      }),
    );
    expect(screen.getByText(/loads on demand/i)).toBeTruthy();
  });

  it("explains what an individual permission grants", () => {
    renderGroup(
      group({
        items: [{ name: "filesystem", label: "filesystem", description: "optional", default_enabled: false }],
      }),
    );
    expect(screen.getByText(/Read and write files on your machine/i)).toBeTruthy();
  });

  it("gives UI surfaces a readable name instead of the raw key", () => {
    renderGroup(
      group({
        id: "ui_surfaces",
        items: [{ name: "quick_button", label: "quick_button", description: "", default_enabled: true }],
      }),
    );
    expect(screen.getByText("Quick button")).toBeTruthy();
    expect(screen.queryByText("quick_button")).toBeNull();
  });

  it("leaves extension-authored items to their own descriptor text", () => {
    const { container } = renderGroup(
      group({
        id: "mcp_servers",
        scope: "profile",
        items: [{ name: "boards", label: "boards", description: "Board tools", default_enabled: true }],
      }),
    );
    expect(screen.getByText("Board tools")).toBeTruthy();
    expect(container.querySelector(".harness-item-hint")).toBeNull();
  });
});
