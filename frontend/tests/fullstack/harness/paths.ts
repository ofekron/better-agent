import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export const REPO_ROOT = path.resolve(__dirname, "..", "..", "..", "..");
export const BACKEND_DIR = path.join(REPO_ROOT, "backend");
export const FRONTEND_DIR = path.join(REPO_ROOT, "frontend");
export const FRONTEND_DIST_DIR = path.join(FRONTEND_DIR, "dist");
