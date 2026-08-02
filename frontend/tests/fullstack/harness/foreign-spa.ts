import { spawnSync } from "node:child_process";
import { createReadStream, existsSync, mkdtempSync, rmSync, statSync } from "node:fs";
import { createServer, type Server } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";

import { FRONTEND_DIR } from "./paths";
import { allocateLoopbackPort } from "./process-utils";

export interface ForeignOriginSpa {
  baseURL: string;
  stop(): Promise<void>;
}

const CONTENT_TYPES: Record<string, string> = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
};

function closeServer(server: Server): Promise<void> {
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

export async function startForeignOriginSpa(
  backendBaseURL: string,
  excludedPorts: ReadonlySet<number>,
): Promise<ForeignOriginSpa> {
  const buildRoot = mkdtempSync(path.join(tmpdir(), "ba-foreign-spa-"));
  const vite = path.join(FRONTEND_DIR, "node_modules", "vite", "bin", "vite.js");
  const build = spawnSync(
    process.execPath,
    [vite, "build", "--outDir", buildRoot, "--emptyOutDir"],
    {
      cwd: FRONTEND_DIR,
      encoding: "utf-8",
      env: { ...process.env, VITE_API_URL: backendBaseURL },
    },
  );
  if (build.status !== 0) {
    rmSync(buildRoot, { recursive: true, force: true });
    throw new Error(`foreign-origin SPA build failed:\n${build.stdout}\n${build.stderr}`);
  }

  const indexPath = path.join(buildRoot, "index.html");
  const server = createServer((request, response) => {
    const pathname = decodeURIComponent(new URL(request.url ?? "/", "http://localhost").pathname);
    const requestedPath = path.resolve(buildRoot, `.${pathname}`);
    const confined = requestedPath === buildRoot || requestedPath.startsWith(`${buildRoot}${path.sep}`);
    const filePath = confined && existsSync(requestedPath) && statSync(requestedPath).isFile()
      ? requestedPath
      : indexPath;
    response.setHeader("Cache-Control", "no-store");
    response.setHeader("Content-Type", CONTENT_TYPES[path.extname(filePath)] ?? "application/octet-stream");
    createReadStream(filePath).pipe(response);
  });

  const port = await allocateLoopbackPort(new Set([...excludedPorts, 3_000, 8_000]));
  try {
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(port, "127.0.0.1", resolve);
    });
  } catch (error) {
    rmSync(buildRoot, { recursive: true, force: true });
    throw error;
  }

  let stopped = false;
  return {
    baseURL: `http://127.0.0.1:${port}`,
    async stop() {
      if (stopped) return;
      stopped = true;
      await closeServer(server);
      rmSync(buildRoot, { recursive: true, force: true });
    },
  };
}
