export function joinPickerPath(parent: string, child: string): string {
  const name = child.trim();
  if (!parent) return name;
  if (!name) return parent;
  return parent.endsWith("/") ? `${parent}${name}` : `${parent}/${name}`;
}
