import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import type { ReactNode } from "react";

// Surface i18next interpolation args so the useMemo join logic (warm-pool tool
// list, permission scope list) is assertable. With empty resources the real
// t() returns only the key, hiding the computed values — which would make the
// useMemo coverage a line-toucher.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) => {
      if (!opts) return key;
      const vals = Object.values(opts);
      return vals.length ? `${key}:${vals.join(",")}` : key;
    },
  }),
}));

// Isolate the parent's own logic. The child's focus-trap / back-button / escape
// behavior belongs to its own slice; render a thin pass-through that forwards
// the props we assert on and renders children (the permission rows + notices).
vi.mock("../src/components/MarketplaceConfirmationModal", async () => {
  const React = (await import("react")).default;
  type ChildProps = {
    open: boolean;
    title: string;
    busy: boolean;
    error: string;
    onConfirm: () => void;
    onCancel: () => void;
    children: ReactNode;
  };
  return {
    MarketplaceConfirmationModal: ({
      open,
      title,
      busy,
      error,
      onConfirm,
      onCancel,
      children,
    }: ChildProps) =>
      open
        ? React.createElement(
            "div",
            { "data-testid": "mcm" },
            React.createElement("div", { "data-testid": "mcm-title" }, title),
            React.createElement("div", { "data-testid": "mcm-busy" }, String(busy)),
            React.createElement("div", { "data-testid": "mcm-error" }, error),
            React.createElement(
              "button",
              { "data-testid": "mcm-confirm", onClick: onConfirm },
              "confirm",
            ),
            React.createElement(
              "button",
              { "data-testid": "mcm-cancel", onClick: onCancel },
              "cancel",
            ),
            React.createElement("div", { "data-testid": "mcm-body" }, children),
          )
        : null,
  };
});

import { MarketplaceInstallModal } from "../src/components/MarketplaceInstallModal";
import type { MarketplaceInstallManifest } from "../src/components/MarketplaceInstallModal";

function renderModal(overrides: Partial<{
  open: boolean;
  manifest: MarketplaceInstallManifest;
  busy: boolean;
  error: string;
  onConfirm: () => void;
  onCancel: () => void;
}> = {}) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  const manifest: MarketplaceInstallManifest = overrides.manifest ?? {
    id: "ext-1",
    name: "My Extension",
    version: "1.0.0",
  };
  const utils = render(
    <MarketplaceInstallModal
      open={overrides.open ?? true}
      manifest={manifest}
      busy={overrides.busy ?? false}
      error={overrides.error ?? ""}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />,
  );
  return { ...utils, onConfirm, onCancel, manifest };
}

describe("MarketplaceInstallModal", () => {
  describe("child wiring + open gating", () => {
    it("renders nothing when open is false (child returns null)", () => {
      const { queryByTestId } = renderModal({ open: false });
      expect(queryByTestId("mcm")).toBeNull();
    });

    it("forwards manifest.name as the modal title", () => {
      const { getByTestId } = renderModal();
      expect(getByTestId("mcm-title").textContent).toBe("My Extension");
    });

    it("forwards busy and error to the child", () => {
      const { getByTestId } = renderModal({ busy: true, error: "boom" });
      expect(getByTestId("mcm-busy").textContent).toBe("true");
      expect(getByTestId("mcm-error").textContent).toBe("boom");
    });

    it("invokes onConfirm / onCancel through the child buttons", () => {
      const { getByTestId, onConfirm, onCancel } = renderModal();
      fireEvent.click(getByTestId("mcm-confirm"));
      fireEvent.click(getByTestId("mcm-cancel"));
      expect(onConfirm).toHaveBeenCalledTimes(1);
      expect(onCancel).toHaveBeenCalledTimes(1);
    });
  });

  describe("header", () => {
    it("always renders the permissions header + help text", () => {
      const { getByTestId } = renderModal();
      const body = getByTestId("mcm-body").textContent ?? "";
      expect(body).toContain("settings.extensionsPermissions");
      expect(body).toContain("settings.extensionsPermissionsHelp");
    });
  });

  describe("permissions useMemo — filtering + sorting", () => {
    it("filters out permissions whose value is strictly false", () => {
      const { getByTestId } = renderModal({
        manifest: {
          id: "x",
          name: "X",
          version: "1",
          permissions: { hidden: false, shown: true },
        },
      });
      const body = getByTestId("mcm-body").textContent ?? "";
      expect(body).toContain("shown");
      expect(body).not.toContain("hidden");
    });

    it("sorts permissions alphabetically by key", () => {
      const { container } = renderModal({
        manifest: {
          id: "x",
          name: "X",
          version: "1",
          permissions: { zeta: true, alpha: true, mango: true },
        },
      });
      const keys = Array.from(
        container.querySelectorAll(".extension-ui-settings-permission-key"),
      ).map((el) => el.textContent);
      expect(keys).toEqual(["alpha", "mango", "zeta"]);
    });

    it("renders no permission rows when permissions is absent", () => {
      const { container } = renderModal();
      expect(
        container.querySelectorAll(".extension-ui-settings-permission").length,
      ).toBe(0);
    });
  });

  describe("permission value type discrimination — mode span", () => {
    it("boolean true → required mode", () => {
      const { container } = renderModal({
        manifest: {
          id: "x",
          name: "X",
          version: "1",
          permissions: { filesystem: true },
        },
      });
      const mode = container.querySelector(
        ".extension-ui-settings-permission-mode",
      );
      expect(mode?.textContent).toBe("settings.extensionsPermissionMode.required");
    });

    it('"optional" → optionalOff mode', () => {
      const { container } = renderModal({
        manifest: {
          id: "x",
          name: "X",
          version: "1",
          permissions: { network: "optional" },
        },
      });
      const mode = container.querySelector(
        ".extension-ui-settings-permission-mode",
      );
      expect(mode?.textContent).toBe(
        "settings.extensionsPermissionMode.optionalOff",
      );
    });

    it("array → scoped mode", () => {
      const { container } = renderModal({
        manifest: {
          id: "x",
          name: "X",
          version: "1",
          permissions: { storage: ["/a", "/b"] },
        },
      });
      const mode = container.querySelector(
        ".extension-ui-settings-permission-mode",
      );
      expect(mode?.textContent).toBe(
        "settings.extensionsPermissionMode.scoped",
      );
    });
  });

  describe("permission scope rendering", () => {
    it("renders the scope list (joined) for a non-empty array value", () => {
      const { container } = renderModal({
        manifest: {
          id: "x",
          name: "X",
          version: "1",
          permissions: { storage: ["/a", "/b"] },
        },
      });
      const scope = container.querySelector(
        ".extension-ui-settings-permission-scope",
      );
      // Interpolation surfaced by the mock: key + joined option value.
      expect(scope?.textContent).toBe(
        "settings.extensionsPermission.scope:/a, /b",
      );
    });

    it("omits the scope div for an empty array", () => {
      const { container } = renderModal({
        manifest: {
          id: "x",
          name: "X",
          version: "1",
          permissions: { storage: [] },
        },
      });
      // Empty array still classifies as scoped mode...
      const mode = container.querySelector(
        ".extension-ui-settings-permission-mode",
      );
      expect(mode?.textContent).toBe(
        "settings.extensionsPermissionMode.scoped",
      );
      // ...but the scope div is gated on length > 0.
      expect(
        container.querySelector(".extension-ui-settings-permission-scope"),
      ).toBeNull();
    });
  });

  describe("extensionPermissionTranslationKey integration", () => {
    it("uses the known-permission key for a recognized permission", () => {
      const { container } = renderModal({
        manifest: {
          id: "x",
          name: "X",
          version: "1",
          permissions: { filesystem: true },
        },
      });
      const body = container.textContent ?? "";
      expect(body).toContain("settings.extensionsPermission.filesystem.label");
      expect(body).toContain("settings.extensionsPermission.filesystem.risk");
    });

    it("falls back to the unknown-permission key for an unrecognized permission", () => {
      const { container } = renderModal({
        manifest: {
          id: "x",
          name: "X",
          version: "1",
          permissions: { totallyMadeUp: true },
        },
      });
      const body = container.textContent ?? "";
      expect(body).toContain("settings.extensionsPermission.unknown.label");
      expect(body).toContain("settings.extensionsPermission.unknown.risk");
    });
  });

  describe("warmPoolServers useMemo", () => {
    it("renders the warm-pool notice joining warm_pool server labels", () => {
      const { container } = renderModal({
        manifest: {
          id: "x",
          name: "X",
          version: "1",
          entrypoints: {
            mcp: [
              { name: "warm-a", label: "Warm A", execution: "warm_pool" },
              { name: "warm-b", label: "Warm B", execution: "warm_pool" },
            ],
          },
        },
      });
      const notice = container.querySelector(
        ".extension-ui-settings-permission-risk",
      );
      // Both warm_pool labels joined; subprocess servers excluded.
      expect(notice?.textContent).toBe(
        "settings.extensionsWarmPoolNotice:Warm A, Warm B",
      );
    });

    it("excludes non-warm_pool (subprocess) servers from the notice", () => {
      const { container } = renderModal({
        manifest: {
          id: "x",
          name: "X",
          version: "1",
          entrypoints: {
            mcp: [
              { name: "sub-1", execution: "subprocess" },
              { name: "warm-a", label: "Warm A", execution: "warm_pool" },
            ],
          },
        },
      });
      const notice = container.querySelector(
        ".extension-ui-settings-permission-risk",
      );
      expect(notice?.textContent).toBe(
        "settings.extensionsWarmPoolNotice:Warm A",
      );
    });

    it("falls back to server name when label is missing", () => {
      const { container } = renderModal({
        manifest: {
          id: "x",
          name: "X",
          version: "1",
          entrypoints: {
            mcp: [{ name: "name-only", execution: "warm_pool" }],
          },
        },
      });
      const notice = container.querySelector(
        ".extension-ui-settings-permission-risk",
      );
      expect(notice?.textContent).toBe(
        "settings.extensionsWarmPoolNotice:name-only",
      );
    });

    it("drops warm_pool servers with neither label nor name", () => {
      const { container } = renderModal({
        manifest: {
          id: "x",
          name: "X",
          version: "1",
          entrypoints: {
            mcp: [
              { execution: "warm_pool" },
              { name: "real", label: "Real", execution: "warm_pool" },
            ],
          },
        },
      });
      const notice = container.querySelector(
        ".extension-ui-settings-permission-risk",
      );
      // Only the named server survives the Boolean filter.
      expect(notice?.textContent).toBe(
        "settings.extensionsWarmPoolNotice:Real",
      );
    });

    it("renders no warm-pool notice when no warm_pool servers exist", () => {
      const { container } = renderModal({
        manifest: {
          id: "x",
          name: "X",
          version: "1",
          entrypoints: {
            mcp: [{ name: "sub-1", execution: "subprocess" }],
          },
        },
      });
      expect(container.querySelector(".extension-ui-settings-permission")).toBeNull();
    });

    it("renders no warm-pool notice when entrypoints are absent", () => {
      const { container } = renderModal();
      expect(container.querySelector(".extension-ui-settings-permission")).toBeNull();
    });
  });
});
