const WINDOWS_PATH = /^(?:[A-Za-z]:[\\/]|\\\\)/;

/**
 * Spelling-independent identity for a project path.
 *
 * Mirrors backend `session_manager.canonical_project_path`. A Windows
 * directory has two equally valid spellings (`C:\Users\x` and `C:/Users/x`),
 * and a session hosted on a Windows node is stored with whichever one its
 * creator used — exact comparison files the same directory as two projects.
 *
 * Only Windows-shaped paths are normalized: on POSIX a backslash is a legal
 * filename character, so rewriting separators there would merge genuinely
 * different directories.
 */
export function canonicalProjectPath(path: string | null | undefined): string {
  const text = (path ?? "").trim();
  if (!text || !WINDOWS_PATH.test(text)) return text;
  const unified = text.replace(/\\/g, "/").replace(/\/+$/, "");
  if (unified.length >= 2 && unified[1] === ":") {
    return unified[0].toUpperCase() + unified.slice(1);
  }
  return unified;
}

/** True when a session cwd and a project path name the same directory. */
export function sameProjectPath(
  cwd: string | null | undefined,
  projectPath: string | null | undefined,
): boolean {
  if (cwd === projectPath) return true;
  return canonicalProjectPath(cwd) === canonicalProjectPath(projectPath);
}

export function projectPathName(path: string | null | undefined): string {
  const text = (path ?? "").replace(/[\\/]+$/, "");
  return text.split(/[\\/]/).pop() || text;
}
