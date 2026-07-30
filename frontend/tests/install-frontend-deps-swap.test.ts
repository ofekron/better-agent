import { readFileSync } from "node:fs";
import { dirname, join, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
// @ts-expect-error — build script, no type declarations
import {
  prepareInstallManifest,
  retiredNodeModulesPath,
} from "../scripts/install-frontend-deps.mjs";

const scriptPath = join(
  dirname(dirname(fileURLToPath(import.meta.url))),
  "scripts",
  "install-frontend-deps.mjs",
);

describe("frontend dependency install swap slot", () => {
  it("gives every concurrent install its own retired slot", () => {
    const first = retiredNodeModulesPath("/repo/.frontend-desktop-install-aaa");
    const second = retiredNodeModulesPath("/repo/.frontend-mobile-install-bbb");
    expect(first).not.toBe(second);
  });

  it("keeps the retired slot inside the stage the run cleans up", () => {
    const stage = "/repo/.frontend-desktop-install-aaa";
    expect(retiredNodeModulesPath(stage).startsWith(stage + sep)).toBe(true);
  });

  it("never parks the retired copy at a shared fixed path", () => {
    expect(readFileSync(scriptPath, "utf8")).not.toContain(".node_modules.previous");
  });

  it("does not run the frontend postinstall inside the temporary stage", () => {
    const source = {
      name: "frontend",
      scripts: { postinstall: "node scripts/detect-ips.mjs", build: "vite build" },
      dependencies: { react: "^19.0.0" },
    };

    expect(prepareInstallManifest(source, {})).toEqual({
      name: "frontend",
      scripts: { build: "vite build" },
      dependencies: { react: "^19.0.0" },
    });
    expect(source.scripts.postinstall).toBe("node scripts/detect-ips.mjs");
  });
});
