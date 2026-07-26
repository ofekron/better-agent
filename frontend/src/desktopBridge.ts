export interface DesktopBridgeApi {
  notify_user?: (title: string, body: string) => Promise<unknown>;
}

declare global {
  interface Window {
    pywebview?: {
      api?: DesktopBridgeApi;
    };
  }
}
