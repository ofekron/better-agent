import { type ChildProcess, spawn, spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdtempSync, readdirSync, rmSync, statSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { BACKEND_DIR, FRONTEND_DIST_DIR, REPO_ROOT } from "./paths";
import { provisionHeadlessCredentials } from "./credentials";
import { resolveVenvPython } from "./venv";

export interface FullStackBackend {
  baseURL: string;
  port: number;
  username: string;
  password: string;
  homeDir: string;
  stop(): Promise<void>;
}

export interface StartFullStackBackendOptions {
  /** Provider CLI to adopt for the installation profile. Defaults to "claude". */
  provider?: string;
}

// Bundled skill files under a run's workspace are materialized read-only
// (dr-xr-xr-x), so a plain rmSync can't unlink them (unlink needs write on
// the containing directory, not just the file) — chmod the tree writable
// first, then remove it.
function removeHomeDir(homeDir: string): void {
  const walk = (dir: string): void => {
    let entries: string[];
    try {
      entries = readdirSync(dir);
    } catch {
      return;
    }
    for (const entry of entries) {
      const entryPath = path.join(dir, entry);
      let stat;
      try {
        stat = statSync(entryPath);
      } catch {
        continue;
      }
      if (stat.isDirectory()) walk(entryPath);
    }
    try {
      chmodSync(dir, 0o700);
    } catch {
      // best-effort
    }
  };
  walk(homeDir);
  rmSync(homeDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address !== "object") {
        server.close(() => reject(new Error("failed to allocate a free port")));
        return;
      }
      const { port } = address;
      server.close((err) => (err ? reject(err) : resolve(port)));
    });
  });
}

async function waitUntilHealthyOrExit(
  baseURL: string,
  isExited: () => string | null,
  timeoutMs: number,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const exitDescription = isExited();
    if (exitDescription) {
      throw new Error(`backend process exited before becoming healthy: ${exitDescription}`);
    }
    try {
      const res = await fetch(`${baseURL}/api/auth/needs_setup`);
      if (res.ok) return;
    } catch {
      // backend not accepting connections yet
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`backend did not become healthy within ${timeoutMs}ms at ${baseURL}`);
}

/**
 * Spawns a REAL Better Agent backend as a subprocess (uvicorn, no --reload,
 * matching run.sh's production topology) with:
 *  - an isolated BETTER_AGENT_HOME (fresh tempdir, never the real user home)
 *  - real headless-auth credentials (random username/password, argon2id
 *    hashed via scripts/hash-password.py — the same mechanism docker/run.sh
 *    use), so the UI shows the real login screen, not the first-run setup
 *    screen, and Playwright exercises a real password verification
 *  - a real installation profile (scripts/install.py --adopt, so it never
 *    installs a new provider CLI — it only adopts the one already on PATH)
 *  - the real built frontend (frontend/dist), served by the backend itself
 *    exactly like production, so there is a single origin/port for the UI
 *
 * The spawned process inherits the ambient environment (notably it does NOT
 * override CLAUDE_CONFIG_DIR), so real provider CLI subprocesses it launches
 * authenticate with this machine's real, already-logged-in credentials.
 */
export async function startFullStackBackend(
  options: StartFullStackBackendOptions = {},
): Promise<FullStackBackend> {
  if (!existsSync(FRONTEND_DIST_DIR)) {
    throw new Error(
      `frontend dist not found at ${FRONTEND_DIST_DIR}. Run "npm run build" in frontend/ before running full-stack tests.`,
    );
  }

  const python = resolveVenvPython();
  const homeDir = mkdtempSync(path.join(tmpdir(), "ba-fullstack-"));
  const port = await freePort();
  const provider = options.provider ?? "claude";

  let credentials;
  try {
    credentials = provisionHeadlessCredentials(python, path.join(homeDir, "_auth"));
  } catch (err) {
    removeHomeDir(homeDir);
    throw err;
  }

  const sdkDir = path.join(REPO_ROOT, "sdk");
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    // main.py imports better_agent_sdk, which lives outside the backend
    // venv's site-packages (it's the repo's own sdk/ package, not a
    // installed dependency).
    PYTHONPATH: process.env.PYTHONPATH ? `${sdkDir}:${process.env.PYTHONPATH}` : sdkDir,
    BETTER_AGENT_HOME: homeDir,
    BETTER_CLAUDE_HOME: homeDir,
    BETTER_AGENT_TEST_MODE: "1",
    BETTER_AGENT_HEADLESS_AUTH: "1",
    BETTER_AGENT_USERNAME: credentials.username,
    BETTER_AGENT_PASSWORD_HASH_FILE: credentials.passwordHashFile,
    BETTER_AGENT_SESSION_SECRET_FILE: credentials.sessionSecretFile,
    BETTER_AGENT_BACKEND_PORT: String(port),
  };

  const install = spawnSync(
    python,
    [
      path.join(REPO_ROOT, "scripts", "install.py"),
      "--mode",
      "default",
      "--provider",
      provider,
      "--yes",
      "--adopt",
    ],
    { cwd: REPO_ROOT, env, encoding: "utf-8" },
  );
  if (install.status !== 0) {
    removeHomeDir(homeDir);
    throw new Error(`scripts/install.py failed:\n${install.stdout}\n${install.stderr}`);
  }

  const proc: ChildProcess = spawn(
    python,
    ["-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", String(port)],
    // detached so uvicorn leads its own process group: real turns spawn
    // real provider CLI subprocesses (and those spawn further children).
    // Killing only the uvicorn PID on stop() would orphan them instead of
    // terminating the turn; killing the negative PID signals the whole
    // group.
    { cwd: BACKEND_DIR, env, stdio: ["ignore", "pipe", "pipe"], detached: true },
  );

  const logLines: string[] = [];
  proc.stdout?.on("data", (chunk: Buffer) => logLines.push(chunk.toString()));
  proc.stderr?.on("data", (chunk: Buffer) => logLines.push(chunk.toString()));

  let exitDescription: string | null = null;
  proc.once("exit", (code, signal) => {
    exitDescription = `code=${code} signal=${signal}\n${logLines.join("")}`;
  });

  const baseURL = `http://127.0.0.1:${port}`;

  function killGroup(signal: NodeJS.Signals): void {
    try {
      process.kill(-proc.pid!, signal);
    } catch {
      // group already gone, or this platform doesn't support negative-PID
      // group kill (non-POSIX) — fall back to the direct child.
      proc.kill(signal);
    }
  }

  try {
    await waitUntilHealthyOrExit(baseURL, () => exitDescription, 30_000);
  } catch (err) {
    killGroup("SIGKILL");
    removeHomeDir(homeDir);
    throw err;
  }

  let stopped = false;
  async function stop(): Promise<void> {
    if (stopped) return;
    stopped = true;
    if (proc.exitCode === null && proc.signalCode === null) {
      killGroup("SIGTERM");
      await Promise.race([
        new Promise<void>((resolve) => proc.once("exit", () => resolve())),
        new Promise<void>((resolve) =>
          setTimeout(() => {
            killGroup("SIGKILL");
            resolve();
          }, 10_000),
        ),
      ]);
    }
    removeHomeDir(homeDir);
  }

  return {
    baseURL,
    port,
    username: credentials.username,
    password: credentials.password,
    homeDir,
    stop,
  };
}
