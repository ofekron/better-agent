import type { PluginListenerHandle } from "./capacitor-core";

export interface Token {
  value: string;
}

export interface ActionPerformed {
  notification: { data?: unknown };
}

const handle: PluginListenerHandle = {
  async remove() {},
};

export const PushNotifications = {
  async addListener(
    _event: string,
    _listener: (value: never) => void,
  ): Promise<PluginListenerHandle> {
    return handle;
  },
  async checkPermissions(): Promise<{ receive: "prompt" | "granted" | "denied" }> {
    return { receive: "denied" };
  },
  async requestPermissions(): Promise<{ receive: "prompt" | "granted" | "denied" }> {
    return { receive: "denied" };
  },
  async register(): Promise<void> {},
};
