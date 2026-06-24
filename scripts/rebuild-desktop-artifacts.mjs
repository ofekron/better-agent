#!/usr/bin/env node
// Smart desktop rebuild for the git pre-commit hook.
//
// Only rebuilds when desktop-relevant files are staged. On macOS it runs the
// macOS PyInstaller/DMG build; on Windows it runs the Windows installer build.
//
// Escape hatch: BA_SKIP_DESKTOP=1 skips entirely.
// Force a rebuild even with no relevant staged files: BA_FORCE_DESKTOP_REBUILD=1.
import { execSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const DESKTOP = join(ROOT, "desktop");
const MAC_DMG = join(DESKTOP, "dist", "BetterAgent.dmg");
const WIN_INSTALLER = join(DESKTOP, "dist", "BetterAgentSetup.exe");
const VERSION_FILE = join(DESKTOP, "_version.py");

const log = (message) => console.log(`[desktop] ${message}`);
const warn = (message) => console.warn(`[desktop] ${message}`);

const RELEVANT = [
  /^desktop\/(BetterAgent\.spec|_version\.py|app_main\.py|build_macos\.sh|build_windows\.ps1|installer\.iss|release\.py|setup\.py|shell\.py|shell_env\.py|supervisor\.py|updater\.py|\.gitignore)$/,
  /^backend\/(app_entry|auth_secrets|paths)\.py$/,
  /^backend\/requirements.*\.txt$/,
  /^frontend\/src\//,
  /^frontend\/public\//,
  /^frontend\/index\.html$/,
  /^frontend\/vite\.config\./,
  /^frontend\/tsconfig.*\.json$/,
  /^frontend\/package(-lock)?\.json$/,
];

const IRRELEVANT = [
  /^desktop\/build\//,
  /^desktop\/dist\//,
  /^desktop\/__pycache__\//,
  /^desktop\/\.pytest_cache\//,
];

function sh(command, options = {}) {
  return execSync(command, { encoding: "utf8", cwd: ROOT, ...options }).toString().trim();
}

function stagedFiles() {
  if (process.env.BA_STAGED_FILES_JSON) {
    const parsed = JSON.parse(process.env.BA_STAGED_FILES_JSON);
    if (!Array.isArray(parsed) || !parsed.every((item) => typeof item === "string")) {
      throw new Error("BA_STAGED_FILES_JSON must be a JSON string array");
    }
    return parsed;
  }
  const out = sh("git diff --cached --name-only --diff-filter=ACMR");
  return out ? out.split("\n") : [];
}

function isRelevant(file) {
  if (IRRELEVANT.some((re) => re.test(file))) return false;
  return RELEVANT.some((re) => re.test(file));
}

function bumpVersion() {
  const version = process.env.BA_DESKTOP_VERSION || `0.1.${Math.floor(Date.now() / 1000)}`;
  const source = readFileSync(VERSION_FILE, "utf8");
  const next = source.replace(/__version__ = "[^"]+"/, `__version__ = "${version}"`);
  if (next === source) {
    throw new Error("desktop/_version.py does not contain a replaceable __version__");
  }
  writeFileSync(VERSION_FILE, next);
  if (process.env.BA_ARTIFACT_BACKGROUND !== "1") {
    execSync("git add desktop/_version.py", { cwd: ROOT, stdio: "ignore" });
  }
  log(`desktop version set to ${version}`);
  return version;
}

function runMacBuild() {
  log("desktop-relevant changes detected — rebuilding macOS DMG...");
  execSync("./build_macos.sh", { cwd: DESKTOP, stdio: "inherit", env: process.env });
  if (!existsSync(MAC_DMG)) {
    throw new Error(`macOS DMG was not produced at ${MAC_DMG}`);
  }
}

function runWindowsBuild() {
  log("desktop-relevant changes detected — rebuilding Windows installer...");
  execSync(
    "powershell -ExecutionPolicy Bypass -File desktop\\build_windows.ps1",
    { cwd: ROOT, stdio: "inherit", env: process.env },
  );
  if (!existsSync(WIN_INSTALLER)) {
    throw new Error(`Windows installer was not produced at ${WIN_INSTALLER}`);
  }
}

if (process.env.BA_SKIP_DESKTOP === "1") {
  log("BA_SKIP_DESKTOP=1 — skipping.");
  process.exit(0);
}

const files = stagedFiles();
if (process.env.BA_FORCE_DESKTOP_REBUILD !== "1" && !files.some(isRelevant)) {
  log("no desktop-relevant staged changes — skipping rebuild.");
  process.exit(0);
}

try {
  bumpVersion();
  if (process.platform === "darwin") {
    runMacBuild();
  } else if (process.platform === "win32") {
    runWindowsBuild();
  } else {
    warn(`unsupported desktop build host ${process.platform} — skipping.`);
  }
} catch (error) {
  warn(`${error instanceof Error ? error.message : String(error)}`);
  warn(
    process.env.BA_ARTIFACT_BACKGROUND === "1"
      ? "BUILD FAILED — source commit already completed; artifact follow-up commit will not be created."
      : "BUILD FAILED — refusing to commit stale desktop artifacts. Set BA_SKIP_DESKTOP=1 to bypass.",
  );
  process.exit(1);
}
