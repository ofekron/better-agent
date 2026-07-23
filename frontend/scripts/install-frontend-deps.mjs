import { execFileSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  mkdtempSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const frontend = dirname(dirname(fileURLToPath(import.meta.url)));
const repository = dirname(frontend);
const profile = process.argv[2];
if (profile !== "desktop" && profile !== "mobile") {
  throw new Error(`Unknown frontend dependency profile: ${profile}`);
}

const stage = mkdtempSync(join(repository, `.frontend-${profile}-install-`));
const current = join(frontend, "node_modules");
const previous = join(frontend, ".node_modules.previous");

try {
  const manifest = JSON.parse(
    readFileSync(join(frontend, "package.json"), "utf8"),
  );
  if (profile === "mobile") {
    const mobile = JSON.parse(
      readFileSync(join(frontend, "mobile-dependencies.json"), "utf8"),
    );
    manifest.dependencies = { ...manifest.dependencies, ...mobile };
  }
  writeFileSync(
    join(stage, "package.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
  );
  cpSync(
    join(frontend, profile === "mobile" ? "package-lock.mobile.json" : "package-lock.json"),
    join(stage, "package-lock.json"),
  );
  cpSync(join(frontend, "scripts"), join(stage, "scripts"), { recursive: true });
  execFileSync("npm", ["ci"], { cwd: stage, stdio: "inherit" });
  rmSync(previous, { recursive: true, force: true });
  if (existsSync(current)) {
    renameSync(current, previous);
  }
  try {
    renameSync(join(stage, "node_modules"), current);
  } catch (error) {
    if (existsSync(previous)) {
      renameSync(previous, current);
    }
    throw error;
  }
  rmSync(previous, { recursive: true, force: true });
} finally {
  rmSync(stage, { recursive: true, force: true });
}
