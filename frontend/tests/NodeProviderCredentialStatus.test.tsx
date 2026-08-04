import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";

import { NodeProviderCredentialStatus } from "../src/components/NodeProviderCredentialStatus";
import { parseWireEvent } from "../src/lib/webSocketIngress";
import type { NodeSnapshot, NodeProviderCredentialStatus as CredentialStatus } from "../src/types";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, values?: Record<string, string>) =>
      `${key}:${values?.provider ?? ""}:${values?.node ?? ""}`,
  }),
}));

function machine(status: CredentialStatus): NodeSnapshot {
  return {
    id: "lenovo",
    role: "worker_node",
    address: "",
    cwd_roots: [],
    state: "connected",
    connected_at: 1,
    last_seen: 1,
    app_commit_sha: "",
    app_dirty: false,
    primary_commit_sha: "",
    primary_dirty: false,
    version_status: "ok",
    provider_credentials: [status],
  };
}

function status(
  state: CredentialStatus["status"],
  updatedAt = "2026-08-04T10:00:00Z",
): CredentialStatus {
  return {
    node_id: "lenovo",
    provider_id: "zai-claude",
    provider_name: "Z.AI Claude",
    status: state,
    authorized_at: "2026-08-04T09:00:00Z",
    updated_at: updatedAt,
  };
}

test("shows pending sync until backend status completes", () => {
  const { rerender } = render(
    <NodeProviderCredentialStatus machines={[machine(status("pending"))]} />,
  );
  expect(screen.getByRole("status").textContent).toContain(
    "nodeCredentials.syncing:Z.AI Claude:lenovo",
  );

  rerender(
    <NodeProviderCredentialStatus machines={[machine(status("synced"))]} />,
  );
  expect(screen.queryByRole("status")).toBeNull();
});

test("failed sync is dismissible and reappears after backend changes", () => {
  const { rerender } = render(
    <NodeProviderCredentialStatus machines={[machine(status("failed"))]} />,
  );
  expect(screen.getByRole("status").textContent).toContain(
    "nodeCredentials.failed:Z.AI Claude:lenovo",
  );
  fireEvent.click(screen.getByRole("button", { name: /nodeCredentials.dismiss/ }));
  expect(screen.queryByRole("status")).toBeNull();

  rerender(
    <NodeProviderCredentialStatus
      machines={[machine(status("failed", "2026-08-04T10:01:00Z"))]}
    />,
  );
  expect(screen.getByRole("status")).not.toBeNull();
});

test("credential status websocket frames reject malformed projections", () => {
  const valid = parseWireEvent({
    type: "node_provider_credentials_changed",
    data: {
      node_id: "lenovo",
      provider_credentials: [status("pending")],
    },
  });
  expect(valid.ok).toBe(true);

  const invalid = parseWireEvent({
    type: "node_provider_credentials_changed",
    data: {
      node_id: "lenovo",
      provider_credentials: [
        { ...status("pending"), api_key: "must-not-cross-wire" },
      ],
    },
  });
  expect(invalid.ok).toBe(false);
});
