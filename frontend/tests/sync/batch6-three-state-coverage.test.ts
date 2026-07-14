import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const read = (path: string) => readFileSync(`${process.cwd()}/src/${path}`, "utf8");

describe("Batch 6 three-state mutation coverage", () => {
  it.each([
    ["hooks/useMachines.ts", ["machineNode:delete", "machineNode:restart"]],
    ["hooks/usePendingNodeRegistrations.ts", ["machineNode:approve", "machineNode:deny"]],
    ["hooks/useProviderInstalls.ts", ["providerSetup:install"]],
    ["components/Setup.tsx", ["auth:setup"]],
    ["components/NativeImportSetting.tsx", ["nativeImport:start"]],
    ["components/AuthCredentialsSetting.tsx", ["authCredentials:save"]],
  ])("routes %s mutations through the canonical controller", (path, operations) => {
    const source = read(path);
    expect(source).toContain("runThreeStateSync({");
    for (const operation of operations) expect(source).toContain(operation);
    expect(source).toContain("reconcile:");
  });

  it("keeps login/logout transport outside three-state mutation tracking", () => {
    expect(read("components/Login.tsx")).not.toContain("runThreeStateSync");
  });
});
