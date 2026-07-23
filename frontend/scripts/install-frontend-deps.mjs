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
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { readMobileDependencies } from "./mobile-dependencies.mjs";

const frontend = dirname(dirname(fileURLToPath(import.meta.url)));
const repository = dirname(frontend);

export function absolutizeLocalReferences(value, sourceDirectory) {
  if (typeof value === "string") {
    if (!value.startsWith("file:")) return value;
    return pathToFileURL(resolve(sourceDirectory, value.slice("file:".length))).href;
  }
  if (Array.isArray(value)) {
    return value.map((entry) => absolutizeLocalReferences(entry, sourceDirectory));
  }
  if (value === null || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value).map(([key, entry]) => [
      key,
      absolutizeLocalReferences(entry, sourceDirectory),
    ]),
  );
}

function install(profile) {
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
      manifest.dependencies = {
        ...manifest.dependencies,
        ...readMobileDependencies(frontend),
      };
    }
    const lock = JSON.parse(
      readFileSync(
        join(
          frontend,
          profile === "mobile" ? "package-lock.mobile.json" : "package-lock.json",
        ),
        "utf8",
      ),
    );
    writeFileSync(
      join(stage, "package.json"),
      `${JSON.stringify(absolutizeLocalReferences(manifest, frontend), null, 2)}\n`,
    );
    writeFileSync(
      join(stage, "package-lock.json"),
      `${JSON.stringify(absolutizeLocalReferences(lock, frontend), null, 2)}\n`,
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
}

const invokedDirectly =
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href;
if (invokedDirectly) {
  install(process.argv[2]);
}
