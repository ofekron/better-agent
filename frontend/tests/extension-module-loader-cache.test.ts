import { afterEach, expect, it, vi } from "vitest";

vi.mock("../src/lib/frontendLogger", () => ({ logDurable: vi.fn() }));

import {
  disposeExtensionModules,
  loadExtensionModule,
} from "../src/components/extensionModuleLoader";

afterEach(() => {
  disposeExtensionModules("scope-a");
  disposeExtensionModules("scope-b");
});

it("shares one import promise per full versioned URL and auth scope", async () => {
  const urlV1 = "data:text/javascript,export const version='v1'";
  const urlV2 = "data:text/javascript,export const version='v2'";

  const first = loadExtensionModule(urlV1, "scope-a");
  expect(loadExtensionModule(urlV1, "scope-a")).toBe(first);
  expect(loadExtensionModule(urlV1, "scope-b")).not.toBe(first);
  expect(loadExtensionModule(urlV2, "scope-a")).not.toBe(first);

  await expect(first).resolves.toMatchObject({ version: "v1" });
});

it("evicts rejected imports so a later load can retry", async () => {
  const invalidUrl = `data:text/javascript,throw new Error('${crypto.randomUUID()}')`;
  const first = loadExtensionModule(invalidUrl, "scope-a");
  await expect(first).rejects.toThrow();
  const retry = loadExtensionModule(invalidUrl, "scope-a");
  expect(retry).not.toBe(first);
  await expect(retry).rejects.toThrow();
});
