import { randomBytes } from "node:crypto";

/** A random, non-secret test value (usernames, throwaway passwords for
 * change-credentials specs, etc.) — generated at runtime, never a literal,
 * so it can't be mistaken for a checked-in credential. */
export function randomTestValue(label: string): string {
  return `${label}-${randomBytes(9).toString("base64url")}`;
}
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { REPO_ROOT } from "./paths";

export interface HeadlessCredentials {
  username: string;
  password: string;
  passwordHashFile: string;
  sessionSecretFile: string;
}

/**
 * Provisions a fresh random username/password for a test backend instance,
 * hashed via the same sanctioned scripts/hash-password.py the docker/run.sh
 * headless-auth path uses (argon2id, matching what auth.py verifies against).
 * The plaintext password is returned only so Playwright can type it into the
 * real login form; it is never written to disk except in a 0600 temp file
 * that is deleted immediately after hashing.
 */
export function provisionHeadlessCredentials(
  python: string,
  authDir: string,
): HeadlessCredentials {
  mkdirSync(authDir, { recursive: true });

  const username = `ba-fullstack-${randomBytes(4).toString("hex")}`;
  const password = randomBytes(18).toString("base64url");

  const plaintextPath = path.join(authDir, "password.plain");
  const passwordHashFile = path.join(authDir, "password.hash");
  const sessionSecretFile = path.join(authDir, "session.secret");

  writeFileSync(plaintextPath, password, { mode: 0o600 });
  try {
    const result = spawnSync(
      python,
      [
        path.join(REPO_ROOT, "scripts", "hash-password.py"),
        "--password-file",
        plaintextPath,
        "--out",
        passwordHashFile,
      ],
      { cwd: REPO_ROOT, encoding: "utf-8" },
    );
    if (result.status !== 0) {
      throw new Error(`scripts/hash-password.py failed:\n${result.stdout}\n${result.stderr}`);
    }
  } finally {
    rmSync(plaintextPath, { force: true });
  }

  writeFileSync(sessionSecretFile, randomBytes(32).toString("hex"), { mode: 0o600 });

  return { username, password, passwordHashFile, sessionSecretFile };
}
