#!/usr/bin/env node
import { createHash } from "node:crypto";
import { copyFileSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const MARKETING_DMG = "marketing/better-agent/downloads/BetterAgent-macOS-arm64.dmg";
const CHECKSUMS = "marketing/better-agent/downloads/SHA256SUMS.txt";
const INDEX = "marketing/better-agent/index.html";
const SOURCE_DMG = "desktop/dist/BetterAgent.dmg";
const VERSION_FILE = "desktop/_version.py";

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
  const target = join(root, MARKETING_DMG);
  copyFileSync(source, target);

  const bytes = readFileSync(target);
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  writeFileSync(join(root, CHECKSUMS), `${sha256}  BetterAgent-macOS-arm64.dmg\n`);

  const indexPath = join(root, INDEX);
  const index = readFileSync(indexPath, "utf8");
  const linkPattern = /downloads\/BetterAgent-macOS-arm64\.dmg\?v=[^"]+/g;
  if (!linkPattern.test(index)) {
    throw new Error(`${INDEX} does not reference the macOS DMG`);
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
  console.log(`marketing desktop download synced: ${result.version} ${result.sha256}`);
}
