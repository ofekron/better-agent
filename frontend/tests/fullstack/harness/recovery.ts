import { type ChildProcess, spawn, spawnSync } from "node:child_process";
import path from "node:path";
import { BACKEND_DIR } from "./paths";
import { resolveVenvPython } from "./venv";
import { buildBackendEnv, makeGroupKiller, waitUntilHealthyOrExit } from "./process-utils";
import type { FullStackBackend } from "./backend";

/**
 * Crash-recovery test harness: simulates a REAL backend crash (uvicorn
 * process dies, its already-spawned real provider CLI subprocess is left
 * running/orphaned) followed by a fresh backend process picking the same
 * isolated home + port back up — exactly scenario 3 ("Restore") from the
 * repo root CLAUDE.md's "Session event ingestion — three scenarios MUST
 * converge" section.
 *
 * `startFullStackBackend` (harness/backend.ts) does not expose the spawned
 * uvicorn `ChildProcess`/PID on `FullStackBackend` — only `stop()`, which
 * kills the whole detached process group (uvicorn + every provider CLI
 * subprocess it spawned). That's correct for normal teardown, but wrong for
 * crash simulation: a real backend crash does NOT take its already-spawned
 * children down with it (they're separate processes; only a `kill -9` of the
 * whole group would, and that's not what happens when e.g. uvicorn OOMs or a
 * bug crashes the asyncio loop).
 *
 * So instead of reaching into backend.ts's internals (which this task must
 * not edit), this module locates the uvicorn process from the outside via
 * the one thing that's externally observable about it: the listening socket
 * on `backend.port`. Killing exactly that PID (not the process group)
 * reproduces "backend died, orphaned children kept running" precisely.
 */

function findListenerPid(port: number): number | null {
  const result = spawnSync("lsof", ["-t", "-iTCP:" + port, "-sTCP:LISTEN"], {
    encoding: "utf-8",
  });
  const pids = result.stdout
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((s) => Number(s))
    .filter((n) => Number.isFinite(n));
  if (pids.length === 0) return null;
  // uvicorn (no --reload, no multi-worker) runs as a single process bound to
  // the port. If more than one PID shows up (e.g. a shell wrapper), the
  // lowest is the original listener; killing it is still correct because a
  // wrapper without its own listening socket wouldn't appear here at all.
  return Math.min(...pids);
}

/**
 * SIGKILLs ONLY the process holding the listening socket on
 * `backend.port` — a real crash of the backend/uvicorn process itself,
 * leaving any real provider CLI subprocess it spawned for an in-flight turn
 * running as an orphan (reparented, not terminated). This is deliberately
 * NOT `backend.stop()`, which kills the whole detached process group
 * (uvicorn + children) and would not reproduce a crash-mid-turn scenario.
 */
export async function killBackendProcessOnly(backend: FullStackBackend): Promise<void> {
  const pid = findListenerPid(backend.port);
  if (pid === null) {
    throw new Error(`no listening process found on port ${backend.port} to crash`);
  }
  process.kill(pid, "SIGKILL");
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    if (findListenerPid(backend.port) === null) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`backend process ${pid} on port ${backend.port} did not die within 10s`);
}

export interface RestartedBackend {
  baseURL: string;
  port: number;
  pid: number;
  stop(): Promise<void>;
}

/**
 * Spawns a NEW backend/uvicorn process against the SAME isolated home +
 * port an existing `FullStackBackend` used, simulating a real
 * crash-and-restart: same `BETTER_AGENT_HOME`, same headless-auth
 * credentials/files, same port — i.e. the exact topology
 * `startFullStackBackend` (harness/backend.ts) spawns, minus the
 * `install.py` provisioning step (the home is already installed; a restart
 * must not re-provision it, matching how a real crash-recovered process
 * boots against existing on-disk state).
 *
 * `FullStackBackend` only exposes `{homeDir, username, port, baseURL}` (not
 * the credential file paths `provisionHeadlessCredentials` wrote), but
 * those paths are deterministic given `homeDir` — `startFullStackBackend`
 * always provisions them under `<homeDir>/_auth/`.
 */
export async function spawnBackendAgainstExistingHome(
  backend: FullStackBackend,
): Promise<RestartedBackend> {
  const python = resolveVenvPython();
  const authDir = path.join(backend.homeDir, "_auth");

  const env = buildBackendEnv({
    homeDir: backend.homeDir,
    port: backend.port,
    username: backend.username,
    passwordHashFile: path.join(authDir, "password.hash"),
    sessionSecretFile: path.join(authDir, "session.secret"),
  });

  const proc: ChildProcess = spawn(
    python,
    ["-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", String(backend.port)],
    // detached for the same reason backend.ts spawns detached: a real
    // restarted backend leads its own process group for any turns it
    // (re)spawns, and stop() below needs to be able to signal that whole
    // group rather than orphan them a second time.
    { cwd: BACKEND_DIR, env, stdio: ["ignore", "pipe", "pipe"], detached: true },
  );

  const logLines: string[] = [];
  proc.stdout?.on("data", (chunk: Buffer) => logLines.push(chunk.toString()));
  proc.stderr?.on("data", (chunk: Buffer) => logLines.push(chunk.toString()));

  let exitDescription: string | null = null;
  proc.once("exit", (code, signal) => {
    exitDescription = `code=${code} signal=${signal}\n${logLines.join("")}`;
  });

  const killGroup = makeGroupKiller(proc);

  try {
    await waitUntilHealthyOrExit(backend.baseURL, () => exitDescription, 60_000, "restarted backend");
  } catch (err) {
    killGroup("SIGKILL");
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
  }

  return { baseURL: backend.baseURL, port: backend.port, pid: proc.pid!, stop };
}
