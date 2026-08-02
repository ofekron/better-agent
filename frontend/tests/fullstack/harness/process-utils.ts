import type { ChildProcess } from "node:child_process";
import net from "node:net";
import path from "node:path";
import { REPO_ROOT } from "./paths";

export function allocateLoopbackPort(excluded: ReadonlySet<number> = new Set()): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address !== "object") {
        server.close(() => reject(new Error("failed to allocate a loopback port")));
        return;
      }
      const { port } = address;
      server.close((error) => {
        if (error) {
          reject(error);
          return;
        }
        if (excluded.has(port)) {
          void allocateLoopbackPort(excluded).then(resolve, reject);
          return;
        }
        resolve(port);
      });
    });
  });
}

/** Polls `${baseURL}/api/auth/needs_setup` (always-reachable per the
 * installation-bootstrap gate) until it responds OK, or throws if the
 * process exits first or the timeout elapses. */
export async function waitUntilHealthyOrExit(
  baseURL: string,
  isExited: () => string | null,
  timeoutMs: number,
  label = "backend",
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const exitDescription = isExited();
    if (exitDescription) {
      throw new Error(`${label} process exited before becoming healthy: ${exitDescription}`);
    }
    try {
      const res = await fetch(`${baseURL}/api/auth/needs_setup`);
      if (res.ok) return;
    } catch {
      // not accepting connections yet
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`${label} did not become healthy within ${timeoutMs}ms at ${baseURL}`);
}

/** Binds a signal-sender to a detached child's process GROUP (negative
 * PID), falling back to the direct child if group-kill isn't available
 * (already dead, or a non-POSIX platform). */
export function makeGroupKiller(proc: ChildProcess): (signal: NodeJS.Signals) => void {
  return (signal: NodeJS.Signals) => {
    try {
      process.kill(-proc.pid!, signal);
    } catch {
      proc.kill(signal);
    }
  };
}

export interface BackendEnvOptions {
  homeDir: string;
  port: number;
  username: string;
  passwordHashFile: string;
  sessionSecretFile: string;
}

/** The exact env var set a Better Agent backend subprocess needs for
 * isolated-home, headless-auth, test-mode operation — shared by the
 * initial spawn (harness/backend.ts) and a crash-recovery respawn
 * against the same home (harness/recovery.ts). */
export function buildBackendEnv(options: BackendEnvOptions): NodeJS.ProcessEnv {
  const sdkDir = path.join(REPO_ROOT, "sdk");
  // Mirrors run.sh's own export of BETTER_{AGENT,CLAUDE}_BACKEND_URL
  // alongside the port. Self-referential internal calls (e.g. the
  // marketplace extension's own credential-store lookup via
  // better_agent_sdk.Client) read this env var and fall back to the
  // hardcoded `http://localhost:8000` default when it's unset — wrong
  // on this harness's randomly assigned port.
  const backendUrl = `http://127.0.0.1:${options.port}`;
  return {
    ...process.env,
    // main.py imports better_agent_sdk, which lives outside the backend
    // venv's site-packages (it's the repo's own sdk/ package, not an
    // installed dependency).
    PYTHONPATH: process.env.PYTHONPATH ? `${sdkDir}:${process.env.PYTHONPATH}` : sdkDir,
    BETTER_AGENT_HOME: options.homeDir,
    BETTER_CLAUDE_HOME: options.homeDir,
    BETTER_AGENT_TEST_MODE: "1",
    BETTER_AGENT_HEADLESS_AUTH: "1",
    BETTER_AGENT_USERNAME: options.username,
    BETTER_AGENT_PASSWORD_HASH_FILE: options.passwordHashFile,
    BETTER_AGENT_SESSION_SECRET_FILE: options.sessionSecretFile,
    BETTER_AGENT_BACKEND_PORT: String(options.port),
    BETTER_CLAUDE_BACKEND_PORT: String(options.port),
    BETTER_AGENT_BACKEND_URL: backendUrl,
    BETTER_CLAUDE_BACKEND_URL: backendUrl,
  };
}
