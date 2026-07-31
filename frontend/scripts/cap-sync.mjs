// `npx cap sync` discovers native plugins by reading `dependencies` in
// package.json (it does not scan node_modules). frontend/package.json
// intentionally excludes Capacitor packages so desktop installs stay free
// of mobile native plugin bloat (see mobile-dependencies.mjs) — so a bare
// `cap sync` sees zero plugins and strips every plugin include from the
// generated Android/iOS project files. This wraps `cap sync` with the
// mobile deps merged into package.json for just the duration of the call,
// then restores the on-disk file so the isolation holds afterward.
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { readMobileDependencies } from "./mobile-dependencies.mjs";

const frontend = dirname(dirname(fileURLToPath(import.meta.url)));
const nativeRunnerPaths = {
  android: [
    "android", "app", "src", "main", "assets",
    "public", "runners", "offline-sync.js",
  ],
  ios: ["ios", "App", "App", "public", "runners", "offline-sync.js"],
};

export function mergeManifestWithMobileDeps(manifest, mobileDependencies) {
  return {
    ...manifest,
    dependencies: { ...manifest.dependencies, ...mobileDependencies },
  };
}

/** Runs `run` with frontend/package.json temporarily holding the merged
 * mobile manifest, then restores the original bytes on disk — on success,
 * on throw, and on SIGINT/SIGTERM. */
export function withMobilePackageJson(frontendDir, run) {
  const packageJsonPath = join(frontendDir, "package.json");
  const original = readFileSync(packageJsonPath, "utf8");
  const merged = mergeManifestWithMobileDeps(
    JSON.parse(original),
    readMobileDependencies(frontendDir),
  );
  writeFileSync(packageJsonPath, `${JSON.stringify(merged, null, 2)}\n`);

  const restore = () => writeFileSync(packageJsonPath, original);
  const onSignal = () => {
    restore();
    process.exit(1);
  };
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);
  try {
    return run();
  } finally {
    process.removeListener("SIGINT", onSignal);
    process.removeListener("SIGTERM", onSignal);
    restore();
  }
}

export function assertNativeRunnerProjection(
  frontendDir,
  platforms = ["android", "ios"],
) {
  const canonical = readFileSync(
    join(frontendDir, "dist", "runners", "offline-sync.js"),
  );
  for (const platform of platforms) {
    const pathParts = nativeRunnerPaths[platform];
    if (!pathParts) {
      throw new Error(`unsupported native platform: ${platform}`);
    }
    const projected = readFileSync(join(frontendDir, ...pathParts));
    if (!canonical.equals(projected)) {
      throw new Error(
        `${platform} offline runner does not match the canonical mobile bundle`,
      );
    }
  }
}

const invokedDirectly =
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href;
if (invokedDirectly) {
  withMobilePackageJson(frontend, () => {
    execFileSync("npx", ["cap", "sync"], { cwd: frontend, stdio: "inherit" });
  });
  assertNativeRunnerProjection(frontend);
}
