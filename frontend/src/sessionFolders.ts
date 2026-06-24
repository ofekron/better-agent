import type { SessionFolder } from "./types";

export function buildFolderPathMap(folders: SessionFolder[]): Map<string, string> {
  const byId = new Map(folders.map((folder) => [folder.id, folder]));
  const paths = new Map<string, string>();
  const visit = (folder: SessionFolder, seen: Set<string>): string => {
    const cached = paths.get(folder.id);
    if (cached) return cached;
    const parentId = folder.parent_folder_id;
    if (!parentId || seen.has(parentId)) {
      paths.set(folder.id, folder.name);
      return folder.name;
    }
    const parent = byId.get(parentId);
    const path = parent
      ? `${visit(parent, new Set([...seen, folder.id]))} / ${folder.name}`
      : folder.name;
    paths.set(folder.id, path);
    return path;
  };
  for (const folder of folders) visit(folder, new Set([folder.id]));
  return paths;
}

export function sortFolders(a: SessionFolder, b: SessionFolder): number {
  return a.order - b.order || a.name.localeCompare(b.name);
}
