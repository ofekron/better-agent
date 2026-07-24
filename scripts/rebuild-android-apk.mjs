#!/usr/bin/env node
// Smart Android APK rebuild for the git pre-commit hook.
//
// Only rebuilds when APK-relevant files are STAGED, so unrelated commits are
// not slowed down. On rebuild it bundles the web assets, syncs Capacitor,
// assembles the debug APK (JDK 21), drops it at frontend/releases/app-debug.apk
// (untracked, gitignored) and the backend serve location
// (ba_home/mobile/better-agent-debug.apk). The APK is never committed — the
// app downloads it and checks versions straight from the serving backend's
// ba_home/mobile/ copy, which is overwritten in place each build.
//
// Escape hatch: BA_SKIP_APK=1 skips entirely. Force a rebuild even with no
// relevant staged files: BA_FORCE_APK_REBUILD=1.
import { execFileSync, execSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { withMobilePackageJson } from "../frontend/scripts/cap-sync.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const FRONTEND = join(ROOT, "frontend");
const ANDROID = join(FRONTEND, "android");
const APK_OUT = join(ANDROID, "app", "build", "outputs", "apk", "debug", "app-debug.apk");
const RELEASES_APK = join(FRONTEND, "releases", "app-debug.apk");

// JDK 21 is required (Gradle 8.14 / AGP 8.13 reject JDK 25).
const JAVA_HOME = "/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home";
const ANDROID_HOME = process.env.ANDROID_HOME || `${process.env.HOME}/Library/Android/sdk`;

const log = (m) => console.log(`[apk] ${m}`);
const warn = (m) => console.warn(`[apk] ${m}`);

// Staged paths whose change means the bundled APK is stale.
const RELEVANT = [
  /^frontend\/src\//,
  /^frontend\/public\//,
  /^frontend\/index\.html$/,
  /^frontend\/vite\.config\./,
  /^frontend\/tsconfig.*\.json$/,
  /^frontend\/package(-lock)?\.json$/,
  /^frontend\/capacitor\.config\./,
  /^frontend\/android\/app\/src\//,
  /^frontend\/android\/(app\/)?build\.gradle(\.kts)?$/,
  /^frontend\/android\/variables\.gradle$/,
  /^frontend\/android\/capacitor\.(build|settings)\.gradle$/,
];
// Build outputs / generated files — never a trigger (and gitignored anyway).
const IRRELEVANT = [
  /^frontend\/android\/(app\/)?build\//,
  /^frontend\/android\/\.gradle\//,
];

function sh(cmd) {
  return execSync(cmd, { encoding: "utf8", cwd: ROOT }).toString().trim();
}

function resolveBcHome() {
  const pythonPath = process.env.PYTHONPATH
    ? `${join(ROOT, "backend")}:${process.env.PYTHONPATH}`
    : join(ROOT, "backend");
  return execFileSync(
    process.env.PYTHON || "python3",
    ["-c", "from paths import ba_home; print(ba_home())"],
    {
      cwd: ROOT,
      encoding: "utf8",
      env: { ...process.env, PYTHONPATH: pythonPath },
    },
  ).trim();
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

if (process.env.BA_SKIP_APK === "1") {
  log("BA_SKIP_APK=1 — skipping.");
  process.exit(0);
}

const files = stagedFiles();
if (process.env.BA_FORCE_APK_REBUILD !== "1" && !files.some(isRelevant)) {
  log("no APK-relevant staged changes — skipping rebuild.");
  process.exit(0);
}

// Toolchain guard: don't block commits on machines without the toolchain.
if (!existsSync(join(ANDROID, "gradlew"))) { warn("gradlew not found — skipping."); process.exit(0); }
if (!existsSync(JAVA_HOME)) { warn(`JDK 21 not found at ${JAVA_HOME} — skipping.`); process.exit(0); }
if (!existsSync(ANDROID_HOME)) { warn(`Android SDK not found at ${ANDROID_HOME} — skipping.`); process.exit(0); }

const env = { ...process.env, JAVA_HOME, ANDROID_HOME };

// Monotonic build identity for the in-app updater. Epoch seconds is
// always-increasing and unique per rebuild (int32-safe until 2038,
// fine for sideloaded debug builds). versionName is a human label.
const VERSION_CODE = String(Math.floor(Date.now() / 1000));
let shortSha = "";
try { shortSha = sh("git rev-parse --short HEAD"); } catch { /* no git */ }
const VERSION_NAME = shortSha ? `build-${shortSha}` : `build-${VERSION_CODE}`;

log(`APK-relevant changes detected — rebuilding debug APK (v${VERSION_NAME}, code ${VERSION_CODE})…`);
try {
  // --mode mobile is required: vite.config.ts aliases @capacitor/* to
  // src/platform/web/* no-op shims (isNativePlatform() => false) unless
  // mode is exactly "mobile". Without this flag the APK ships a bundle
  // that can never detect it's native or receive real plugin events
  // (e.g. the deep-link server-URL handoff), regardless of what's
  // registered in the native Android project.
  execSync("npx vite build --mode mobile", { cwd: FRONTEND, env, stdio: "inherit" });
  // cap sync discovers native plugins from frontend/package.json's
  // dependencies, which intentionally excludes Capacitor mobile packages
  // (see cap-sync.mjs) — a bare call here strips every native plugin
  // include (e.g. @capacitor/app, which backs the deep-link server-URL
  // handoff) from the generated Android project.
  withMobilePackageJson(FRONTEND, () => {
    execSync("npx cap sync android", { cwd: FRONTEND, env, stdio: "inherit" });
  });
  execSync(
    `./gradlew assembleDebug --no-daemon -PbcVersionCode=${VERSION_CODE} -PbcVersionName=${VERSION_NAME}`,
    { cwd: ANDROID, env, stdio: "inherit" },
  );
} catch {
  warn(
    process.env.BA_ARTIFACT_BACKGROUND === "1"
      ? "BUILD FAILED — source commit already completed; artifact follow-up commit will not be created."
      : "BUILD FAILED — refusing to commit a stale APK. Set BA_SKIP_APK=1 to bypass.",
  );
  process.exit(1);
}
if (!existsSync(APK_OUT)) { warn("APK was not produced — aborting."); process.exit(1); }

copyFileSync(APK_OUT, RELEASES_APK);
const bcHome = resolveBcHome();
const mobileDir = join(bcHome, "mobile");
mkdirSync(mobileDir, { recursive: true });
copyFileSync(APK_OUT, join(mobileDir, "better-agent-debug.apk"));
// Side-channel the staged build's version so /api/mobile/status can
// report it without parsing the APK manifest.
writeFileSync(
  join(mobileDir, "version.json"),
  JSON.stringify({ version_code: Number(VERSION_CODE), version_name: VERSION_NAME, built_at: new Date().toISOString() }),
);

log("rebuilt APK → frontend/releases/app-debug.apk (untracked) + copied to mobile/ for serving.");
