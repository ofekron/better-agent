import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const repoRoot = resolve(import.meta.dirname, "../..");
const bridgeAsset = "extensions/marketplace/ui/bridge.js";
const CANONICAL_V4 =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe("Marketplace bridge UUID compatibility", () => {
  it("generates a cryptographically random v4 UUID without crypto.randomUUID", () => {
    const target: {
      crypto: {
        randomUUID?: () => string;
        getRandomValues: (bytes: Uint8Array) => Uint8Array;
      };
      marketplaceBridge?: { uuidv4: () => string };
    } = {
      crypto: {
        randomUUID: undefined,
        getRandomValues: (bytes) => {
          for (let index = 0; index < bytes.length; index += 1) bytes[index] = index;
          return bytes;
        },
      },
    };
    const source = readFileSync(resolve(repoRoot, bridgeAsset), "utf8");
    new Function("globalThis", source)(target);

    expect(target.marketplaceBridge?.uuidv4()).toMatch(CANONICAL_V4);
  });
});
