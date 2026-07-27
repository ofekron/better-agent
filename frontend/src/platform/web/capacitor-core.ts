export interface PluginListenerHandle {
  remove(): Promise<void>;
}

export const Capacitor = {
  isNativePlatform: () => false,
  getPlatform: () => "web",
};

export class WebPlugin {}

export function registerPlugin<T>(
  _name: string,
  implementations?: { web?: T },
): T {
  return (implementations?.web ?? {}) as T;
}
