import { readFileSync } from "node:fs";
import { join } from "node:path";

/** The single source of truth for which packages are mobile-only: kept out
 * of package.json so desktop installs never pull in native Capacitor
 * plugins, and merged in on demand by whichever consumer needs them
 * (staged mobile install, `cap sync` plugin discovery). */
export function readMobileDependencies(frontendDir) {
  return JSON.parse(
    readFileSync(join(frontendDir, "mobile-dependencies.json"), "utf8"),
  );
}
