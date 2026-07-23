import type { PluginListenerHandle } from "./capacitor-core";

export interface AppState {
  isActive: boolean;
}

const handle: PluginListenerHandle = {
  async remove() {},
};

function addListener(
  event: "appUrlOpen",
  listener: (event: { url: string }) => void,
): Promise<PluginListenerHandle>;
function addListener(
  event: "appStateChange",
  listener: (event: AppState) => void,
): Promise<PluginListenerHandle>;
function addListener(
  event: "backButton" | "resume",
  listener: () => void,
): Promise<PluginListenerHandle>;
async function addListener(
  _event: string,
  _listener: (...args: never[]) => unknown,
): Promise<PluginListenerHandle> {
  return handle;
}

export const App = {
  async getLaunchUrl(): Promise<{ url: string } | undefined> {
    return undefined;
  },
  async getInfo(): Promise<{ version: string; build: string }> {
    return { version: "", build: "" };
  },
  addListener,
  async exitApp(): Promise<void> {},
};
