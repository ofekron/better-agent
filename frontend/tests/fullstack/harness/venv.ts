import { existsSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { BACKEND_DIR, REPO_ROOT } from "./paths";

// Mirrors run.sh's bootstrap: `backend/.active-venv` is a gitignored marker
// (relative path under backend/) pointing at the content-hashed venv that
// backend/dependency_plan.py resolves/builds under backend/.venvs/. Each
// worktree resolves (or builds) its own marker on first use.
function activateVenv(): void {
  const uv = spawnSync("command", ["-v", "uv"], { shell: true, encoding: "utf-8" }).stdout.trim();
  if (!uv) {
    throw new Error("uv is required to resolve the backend venv (not found on PATH)");
  }
  const result = spawnSync(
    "python3",
    [path.join(BACKEND_DIR, "dependency_plan.py"), "activate", "--uv", uv],
    { cwd: REPO_ROOT, encoding: "utf-8" },
  );
  if (result.status !== 0) {
    throw new Error(
      `backend/dependency_plan.py activate failed:\n${result.stdout}\n${result.stderr}`,
    );
  }
}

export function resolveVenvPython(): string {
  const marker = path.join(BACKEND_DIR, ".active-venv");
  if (!existsSync(marker)) {
    activateVenv();
  }
  if (!existsSync(marker)) {
    throw new Error(`backend/.active-venv still missing after dependency_plan.py activate`);
  }
  const relVenvDir = readFileSync(marker, "utf-8").trim();
  const python = path.join(BACKEND_DIR, relVenvDir, "bin", "python3");
  if (!existsSync(python)) {
    throw new Error(`resolved venv python not found at ${python}`);
  }
  return python;
}
