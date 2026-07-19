import { clearStoredToken } from "./bearerAuth";
import { normalizeServerUrl, readNativeServerUrl, writeNativeServerUrl } from "./nativeServerConfig";
import { parseLineSwitchAccessUrl, writeLineSwitchConnection } from "./lineSwitchClient";

export const MOBILE_SERVER_QUERY_PARAM = "server";
export const NATIVE_CONFIG_HOST = "configure";
export const LINE_SWITCH_QUERY_PARAM = "line_switch";

export function nativeConfigUrlForServer(serverUrl: string): string {
  return `betteragent://${NATIVE_CONFIG_HOST}?${MOBILE_SERVER_QUERY_PARAM}=${encodeURIComponent(
    normalizeServerUrl(serverUrl),
  )}`;
}

export function nativeConfigUrlForLineSwitch(accessUrl: string): string {
  const connection = parseLineSwitchAccessUrl(accessUrl);
  return `betteragent://${NATIVE_CONFIG_HOST}?${LINE_SWITCH_QUERY_PARAM}=${encodeURIComponent(
    `${connection.baseUrl}/#${connection.token}`,
  )}`;
}

export function mobileInstallUrl(serverUrl: string, platform: "android" | "ios"): string {
  const normalizedServerUrl = normalizeServerUrl(serverUrl);
  const url = new URL(normalizedServerUrl);
  url.searchParams.set("download", platform);
  url.searchParams.set(MOBILE_SERVER_QUERY_PARAM, normalizedServerUrl);
  return url.toString();
}

export function serverUrlFromSearch(search: string): string | null {
  const raw = new URLSearchParams(search).get(MOBILE_SERVER_QUERY_PARAM);
  if (!raw) return null;
  try {
    return normalizeServerUrl(raw);
  } catch {
    return null;
  }
}

export function applyNativeServerConfigUrl(urlValue: string): boolean {
  let url: URL;
  try {
    url = new URL(urlValue);
  } catch {
    return false;
  }

  if (url.protocol !== "betteragent:" || url.hostname !== NATIVE_CONFIG_HOST) {
    return false;
  }

  const params = new URLSearchParams(url.search);
  const lineSwitchAccess = params.get(LINE_SWITCH_QUERY_PARAM);
  if (lineSwitchAccess) {
    try {
      writeLineSwitchConnection(parseLineSwitchAccessUrl(lineSwitchAccess));
      return true;
    } catch {
      return false;
    }
  }

  const serverUrl = serverUrlFromSearch(url.search);
  if (!serverUrl || readNativeServerUrl() === serverUrl) return false;
  writeNativeServerUrl(serverUrl);
  clearStoredToken();
  return true;
}
