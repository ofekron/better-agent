export async function loadExtensionModule(url: string): Promise<unknown> {
  return await import(/* @vite-ignore */ url);
}
