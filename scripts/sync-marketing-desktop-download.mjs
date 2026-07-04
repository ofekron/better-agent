#!/usr/bin/env node
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const SOURCE_DMG = "desktop/dist/BetterAgent.dmg";
const VERSION_FILE = "desktop/_version.py";

// Marketing sources live in the nested private checkout when present;
// a plain marketing/ dir is the standalone fallback.
export function marketingDir(root = ROOT) {
  const privateDir = join(root, "better-agent-private", "marketing", "better-agent");
  if (existsSync(privateDir)) {
    return privateDir;
  }
  return join(root, "marketing", "better-agent");
}

export function desktopVersion(root = ROOT) {
  const source = readFileSync(join(root, VERSION_FILE), "utf8");
  const match = source.match(/__version__ = "([^"]+)"/);
  if (!match) {
    throw new Error(`${VERSION_FILE} does not contain __version__`);
  }
  return match[1];
}

export function syncMarketingDesktopDownload(root = ROOT) {
  const version = desktopVersion(root);
  const source = join(root, SOURCE_DMG);
  if (!existsSync(source)) {
    return { skipped: true, reason: `${SOURCE_DMG} is missing`, version };
  }
  const marketing = marketingDir(root);
  const indexPath = join(marketing, "index.html");
  if (!existsSync(indexPath)) {
    return { skipped: true, reason: `${indexPath} is missing`, version };
  }
  const target = join(marketing, "downloads", "BetterAgent-macOS-arm64.dmg");
  mkdirSync(dirname(target), { recursive: true });
  copyFileSync(source, target);

  const bytes = readFileSync(target);
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  writeFileSync(join(marketing, "downloads", "SHA256SUMS.txt"), `${sha256}  BetterAgent-macOS-arm64.dmg\n`);

  const index = readFileSync(indexPath, "utf8");
  const linkPattern = /downloads\/BetterAgent-macOS-arm64\.dmg\?v=[^"]+/g;
  if (!linkPattern.test(index)) {
    throw new Error(`${indexPath} does not reference the macOS DMG`);
  }
  const next = index.replace(
    linkPattern,
    `downloads/BetterAgent-macOS-arm64.dmg?v=${version}`,
  );
  writeFileSync(indexPath, next);
  return { version, sha256 };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const result = syncMarketingDesktopDownload(process.cwd());
  if (result.skipped) {
    console.log(`marketing desktop download skipped: ${result.reason}`);
    process.exit(0);
  }
  console.log(`marketing desktop download synced: ${result.version} ${result.sha256}`);
}
