import { readFileSync } from "node:fs";
import { basename, dirname, join, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
// @ts-expect-error — build script, no type declarations
import {
  cleanupInstallStage,
  prepareInstallManifest,
  retiredNodeModulesPath,
} from "../scripts/install-frontend-deps.mjs";

const scriptPath = join(
  dirname(dirname(fileURLToPath(import.meta.url))),
  "scripts",
  "install-frontend-deps.mjs",
);
const frontendPath = dirname(dirname(scriptPath));

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

  it("keeps the node_modules basename so file watchers continue to ignore it", () => {
    expect(basename(retiredNodeModulesPath("/repo/.frontend-install-aaa"))).toBe(
      "node_modules",
    );
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

  it("keeps the mobile lock aligned with the merged install manifest", () => {
    const manifest = JSON.parse(
      readFileSync(join(frontendPath, "package.json"), "utf8"),
    );
    const mobileDependencies = JSON.parse(
      readFileSync(join(frontendPath, "mobile-dependencies.json"), "utf8"),
    );
    const lock = JSON.parse(
      readFileSync(join(frontendPath, "package-lock.mobile.json"), "utf8"),
    );

    const merged = prepareInstallManifest(manifest, mobileDependencies);
    expect(lock.packages[""]?.dependencies).toEqual(merged.dependencies);
    expect(lock.packages[""]?.devDependencies).toEqual(merged.devDependencies);
  });

  it("retries transient recursive cleanup failures", () => {
    const remove = vi.fn();

    cleanupInstallStage("/repo/.frontend-install-aaa", remove);

    expect(remove).toHaveBeenCalledWith("/repo/.frontend-install-aaa", {
      recursive: true,
      force: true,
      maxRetries: 5,
      retryDelay: 100,
    });
  });
});
