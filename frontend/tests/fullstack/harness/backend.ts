import { type ChildProcess, spawn, spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdtempSync, readdirSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { BACKEND_DIR, FRONTEND_DIST_DIR, REPO_ROOT } from "./paths";
import { provisionHeadlessCredentials } from "./credentials";
import { resolveVenvPython } from "./venv";
import {
  allocateLoopbackPort,
  buildBackendEnv,
  makeGroupKiller,
  waitUntilHealthyOrExit,
} from "./process-utils";

export interface FullStackBackend {
  baseURL: string;
  port: number;
  username: string;
  password: string;
  homeDir: string;
  logs(): string;
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
  const port = await allocateLoopbackPort();
  const provider = options.provider ?? "claude";

  let credentials;
  try {
    credentials = provisionHeadlessCredentials(python, path.join(homeDir, "_auth"));
  } catch (err) {
    removeHomeDir(homeDir);
    throw err;
  }

  const env = buildBackendEnv({
    homeDir,
    port,
    username: credentials.username,
    passwordHashFile: credentials.passwordHashFile,
    sessionSecretFile: credentials.sessionSecretFile,
  });

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

  // `backend/app_lifecycle.py::on_startup()` requires a primary-backend
  // launcher-lease reservation (backend_instance_lock.py) before uvicorn
  // will boot — a raw `uvicorn main:app` spawn fails startup with
  // "backend reservation environment is missing". reserve_and_launch_backend.py
  // performs that same reservation dance (mirroring
  // docker_backend_supervisor.py) scoped to this run's isolated homeDir, then
  // execs uvicorn as its child and forwards signals/exit code — so `proc`
  // below still behaves like "the backend process" for every purpose this
  // harness cares about (stdout/stderr, exit code, process-group kill).
  // Opt-in, off by default for every other fullstack spec: comma-separated
  // logger names (e.g. "backend.adapters,jsonl_tailer") to force to DEBUG
  // in the spawned backend, for investigations that need per-flush debug
  // lines without touching backend source.
  const debugLoggerArgs = (process.env.FULLSTACK_DEBUG_LOGGERS ?? "")
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean)
    .flatMap((name) => ["--debug-logger", name]);

  const proc: ChildProcess = spawn(
    python,
    [
      path.join(REPO_ROOT, "frontend", "tests", "fullstack", "harness", "reserve_and_launch_backend.py"),
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
      ...debugLoggerArgs,
    ],
    // detached so the launcher-lease bridge (and the uvicorn child it spawns
    // in the same group) leads its own process group: real turns spawn real
    // provider CLI subprocesses (and those spawn further children). Killing
    // only the top PID on stop() would orphan them instead of terminating
    // the turn; killing the negative PID signals the whole group.
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

  const killGroup = makeGroupKiller(proc);

  // Real provider CLI turns are deliberately spawned with
  // start_new_session=True (see backend/runner.py) — their own process
  // group, detached from uvicorn's, specifically so an in-flight turn
  // survives a backend crash/restart (the recovery invariant this repo's
  // CLAUDE.md documents). That means killGroup() above can never reach
  // them. homeDir is threaded into their argv (embedded in the
  // --mcp-config JSON's env block for the ambient MCP servers), so
  // pkill -f against it is the only way to reap them on teardown.
  function killDescendantsByHomeDir(): void {
    spawnSync("pkill", ["-9", "-f", homeDir]);
  }

  try {
    await waitUntilHealthyOrExit(baseURL, () => exitDescription, 60_000);
  } catch (err) {
    killGroup("SIGKILL");
    killDescendantsByHomeDir();
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
    killDescendantsByHomeDir();
    removeHomeDir(homeDir);
  }

  return {
    baseURL,
    port,
    username: credentials.username,
    password: credentials.password,
    homeDir,
    logs: () => logLines.join(""),
    stop,
  };
}
