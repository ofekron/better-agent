import { Suspense, useState, useCallback, useEffect, useMemo, useRef } from "react";
import { Capacitor } from "@capacitor/core";
import { App as CapApp, type AppState } from "@capacitor/app";
import { useTranslation } from "react-i18next";
import {
  useWebSocket,
  type ImagePayload,
  type FilePayload,
  type RearrangerStateUpdate,
  type RearrangerUpdate,
} from "./hooks/useWebSocket";
import { useLocalStorage } from "./hooks/useLocalStorage";
import { useOfflineQueue } from "./hooks/useOfflineQueue";
import { useSession, type SessionMetadataPatch } from "./hooks/useSession";
import { useResizable } from "./hooks/useResizable";
import { useViewport } from "./hooks/useViewport";
import { useVisualViewport } from "./hooks/useVisualViewport";
import { useMachines } from "./hooks/useMachines";
import { Chat } from "./components/Chat";
import { ASK_SINGLETON_ID } from "./askSession";
import { editSingletonId } from "./projectStructureEditSession";
import { AdvSyncWindow } from "./components/AdvSyncWindow";
import { SessionList, SESSION_DRAG_MIME } from "./components/SessionList";
import { SessionDetailsPanel } from "./components/SessionDetailsPanel";
import type { FileEditorHandle } from "./components/FileViewer";
import { ConfigPanelContext } from "./components/configPanelContext";
import { FileChooserModal } from "./components/FileChooserModal";
import { isAbsolutePath } from "./utils/linkifyFilePaths";
import { setFocusedTagHighlight } from "./utils/tagHighlights";
import { scrollCommentTargetIntoView } from "./utils/commentFocus";
import { TokenUsageDisplay } from "./components/TokenUsage";
import { StartupTasksBanner } from "./components/StartupTasksBanner";
import { SettingsPage } from "./components/SettingsPage";
import { ExtensionModuleSlot, useExtensionFrontendModules } from "./components/ExtensionSlots";
import { useAttentionSound } from "./utils/attentionSound";
import {
  createFetchProviderConfigSyncClient,
  type ProviderConfigSyncCapabilityPickerOutput,
  type ProviderConfigSyncCapabilityPickerSource,
  type ProviderConfigSyncFetchRoutes,
} from "@better-agent/provider-config-sync-ui";
import { ConfirmModal } from "./components/ConfirmModal";
import { BypassPermissionDialog } from "./components/BypassPermissionDialog";
import { sessionIsBypass } from "./utils/permission";
import {
  ProjectSuggestionModal,
  type ProjectSuggestion,
} from "./components/ProjectSuggestionModal";
import {
  NewSessionModal,
  type SessionConfig,
  type InvestigationContext,
} from "./components/NewSessionModal";
import { InvestigateContextMenu, type InvestigationData } from "./components/InvestigateContextMenu";
import { MobileActionSheetProvider } from "./components/MobileActionSheet";
import { DirPickerModal } from "./components/DirPickerModal";
import { FileEditorOverlay } from "./components/FileEditorOverlay";
import type { FileAnchorComment } from "./components/FileEditor";
import { ProjectSettings } from "./components/ProjectSettings";
import { ProjectTabs } from "./components/ProjectTabs";
import { ProjectGitStatus } from "./components/ProjectGitStatus";

import { Login } from "./components/Login";
import { DesktopInstallPrompt } from "./components/DesktopInstallPrompt";
import {
  DonationRedirectNotice,
  DonationWelcomeModal,
  donationWelcomeStorage,
} from "./components/DonationWelcomeModal";
import { UpdatePopup } from "./components/UpdatePopup";
import { useDesktopInstallOffer } from "./hooks/useDesktopInstallOffer";
import { useNativeAppUpdate } from "./hooks/useNativeAppUpdate";
import { Setup } from "./components/Setup";
import { DownloadRedirect } from "./components/DownloadRedirect";
import { ServerSetup } from "./components/ServerSetup";
import { NotesPanel } from "./components/NotesPanel";
import { TodosPanel, visibleTodoCount } from "./components/TodosPanel";
import { CommentsPanel } from "./components/CommentsPanel";
import Icon from "./components/Icon";
import { ExtensionPageIcons, ExtensionQuickButtons } from "./components/ExtensionUiHooks";
import { RefreshResult } from "./components/RefreshResult";
import { applyAppearancePrefs, type AppearancePrefs } from "./components/AppearanceSetting";
import { scaledFontSize } from "./utils/typography";
import { clearRefreshContext, saveRefreshContext } from "./lib/refreshContext";
import { hardRefreshCurrentPage } from "./lib/hardRefresh";
import { lazyWithRetry } from "./lib/lazyWithRetry";
import { uuidv4 } from "./lib/uuid";
import { logPromptSend } from "./lib/promptSendLog";
import { openProviderConfigSyncPage } from "./lib/providerConfigSyncRoute";
import { markFirstRunWizardSeen } from "./lib/firstRunWizard";
import {
  VOICE_APPEND_DRAFT_EVENT,
  VOICE_NEW_SESSION_EVENT,
  VOICE_OPEN_PROMPT_EVENT,
  VOICE_SEND_PROMPT_EVENT,
  type VoicePromptEventDetail,
} from "./lib/voiceActivation";
import { useRoute, sessionPath } from "./hooks/useRoute";
import { ackSessionSeen, sessionRegistry, statusRankForRow, useSessionMeta } from "./lib/sessionRegistry";
import type { CapabilityContext, ChatMessage, FileAttachment, FileDiscussion, FileFocus, OrchestrationMode, PastedImage, Project, Provider, QueuedPrompt, SendMode, Session, TokenUsage } from "./types";
import { SharePicker } from "./components/SharePicker";
import { useShareTarget } from "./hooks/useShareTarget";
import { buildShareDraftPatch } from "./utils/shareAttach";
import { isLeakedProviderMirror } from "./utils/modelDrift";
import { nextDraftSeq, filterStaleDraftPatch } from "./utils/draftSeq";
import type { FileAnchor } from "./types/inlineTag";
import type { PromptEngState } from "./types/promptEng";
import type { FileEditingState } from "./types/fileEditing";
import { mergeTagsIntoPrompt } from "./utils/inlineTagsPrompt";
import { buildOpenFilesPreamble } from "./utils/openFilesPreamble";
import { patchFileDiscussionMeta, upsertFileDiscussionMeta } from "./utils/fileDiscussions";
import { appendPendingUnlessAcked } from "./utils/pendingMessages";
import { resolveAskPrompt } from "./utils/askPrompt";
import {
  getRememberedSessionId,
  pickSessionForProject,
  setRememberedSessionId,
} from "./utils/rememberedSession";
import { buildSendPromptForm, mergeQueuedAttachments } from "./utils/sendPromptForm";
import { isRetryableOfflineError } from "src/utils/offlineRequest";
import { publishBetterAgentTestApeState } from "src/lib/testapeConsumer";
import {
  handleWSEvent as progressHandleWSEvent,
  trackPromise as progressTrackPromise,
  trackedFetch as progressTrackedFetch,
} from "./progress/store";
import { clearStoredToken } from "./bearerAuth";
import "./styles/globals.css";
import "@better-agent/provider-config-sync-ui/styles.css";

import { API, WS_URL } from "./api";
import { extBackendBase, extId } from "./extensionIds";
import { eventBus, subscribeMany } from "./lib/eventBus";
import { makeSessionExtender } from "./utils/wsExtender";
import { cacheProviders } from "./utils/providerCache";
import { useProviderChanged } from "./hooks/useProviderChanged";
import { useBackButtonDismiss } from "./hooks/useBackButtonDismiss";

type RefreshMode = "now" | "idle";
type RestartStatus = {
  accepted: boolean;
  refresh_result?: { request_id?: string } | null;
};

const rearrangerApi = () => extBackendBase("rearranger");
const SESSION_BRIDGE_API = `${API}/api/extensions/ofek-dev.session-bridge/backend`;
const supervisorApi = () => extBackendBase("supervisor");
const PROVIDER_CONFIG_SYNC_PATH = "/api/extensions/ofek-dev.provider-config-sync/backend";
const PROVIDER_CONFIG_SYNC_ROUTES: ProviderConfigSyncFetchRoutes = {
  projects: "/api/projects",
  state: PROVIDER_CONFIG_SYNC_PATH,
  settings: `${PROVIDER_CONFIG_SYNC_PATH}/settings`,
  file: `${PROVIDER_CONFIG_SYNC_PATH}/file`,
  restoreFile: `${PROVIDER_CONFIG_SYNC_PATH}/file/restore`,
  capability: `${PROVIDER_CONFIG_SYNC_PATH}/capability`,
  transferCapability: `${PROVIDER_CONFIG_SYNC_PATH}/capability/transfer`,
  apply: `${PROVIDER_CONFIG_SYNC_PATH}/apply`,
  autoSync: `${PROVIDER_CONFIG_SYNC_PATH}/auto-sync`,
  capabilityPicker: `${PROVIDER_CONFIG_SYNC_PATH}/capability-picker`,
  repository: `${PROVIDER_CONFIG_SYNC_PATH}/repository`,
  repositoryInit: `${PROVIDER_CONFIG_SYNC_PATH}/repository/init`,
  repositoryLoad: `${PROVIDER_CONFIG_SYNC_PATH}/repository/load`,
  repositorySync: `${PROVIDER_CONFIG_SYNC_PATH}/repository/sync`,
};

interface ViewingFile {
  path: string;
  diffBefore?: string;
  diffAfter?: string;
  focus?: FileFocus;
}

// Frozen module-level empty arrays so the no-data branches of props
// passed into <Chat> hand referentially-stable values across renders.
// A fresh `[]` per render would invalidate `memo(MessageGroup)` and
// the renderedGroups useMemo inside Chat on every parent re-render.
const EMPTY_MSGS: readonly ChatMessage[] = Object.freeze([]);
const EMPTY_RUNS_PROP: readonly import("./types").RunInfo[] = Object.freeze([]);
const EMPTY_EVENTS: readonly import("./types").WSEvent[] = Object.freeze([]);
const EMPTY_INLINE_TAGS: readonly import("./types/inlineTag").InlineTag[] =
  Object.freeze([]);
const MIN_TAB_WIDTH_PX = 90;
const MAX_TAB_CAP = 15;

const ProviderConfigSyncPage = lazyWithRetry(() =>
  import("@better-agent/provider-config-sync-ui").then((m) => ({
    default: m.ProviderConfigSyncPage,
  })),
);
const ProviderCapabilityPicker = lazyWithRetry(() =>
  import("@better-agent/provider-config-sync-ui").then((m) => ({
    default: m.ProviderCapabilityPicker,
  })),
);
const FileViewer = lazyWithRetry(() =>
  import("./components/FileViewer").then((m) => ({ default: m.FileViewer })),
);
const FilePanels = lazyWithRetry(() =>
  import("./components/FilePanels").then((m) => ({ default: m.FilePanels })),
);
const ConfigPanels = lazyWithRetry(() =>
  import("./components/ConfigPanels").then((m) => ({ default: m.ConfigPanels })),
);
const FileEditor = lazyWithRetry(() =>
  import("./components/FileEditor").then((m) => ({ default: m.FileEditor })),
);
const MultiFileEditor = lazyWithRetry(() =>
  import("./components/MultiFileEditor").then((m) => ({ default: m.MultiFileEditor })),
);
const AnalyticsPage = lazyWithRetry(() =>
  import("./components/AnalyticsPage").then((m) => ({ default: m.AnalyticsPage })),
);
const providerConfigSyncClient = createFetchProviderConfigSyncClient({
  baseUrl: API,
  credentials: "include",
  routes: PROVIDER_CONFIG_SYNC_ROUTES,
});

type DonationRedirect = {
  status: "success" | "return";
  checkoutId: string | null;
};

export function donationRedirectFromLocation(): DonationRedirect | null {
  const params = new URLSearchParams(window.location.search);
  const status = params.get("donation");
  if (status !== "success" && status !== "return") return null;
  return {
    status,
    checkoutId: params.get("checkout_id"),
  };
}

export function clearDonationRedirectFromUrl() {
  const url = new URL(window.location.href);
  url.searchParams.delete("donation");
  url.searchParams.delete("checkout_id");
  window.history.replaceState(
    window.history.state,
    "",
    `${url.pathname}${url.search}${url.hash}`,
  );
}

function BackendUnavailable({ error, onRetry }: { error: string; onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="login-shell">
      <div className="login-card">
        <h1 className="login-title">{t("backendUnavailable.title", "Cannot reach Better Agent")}</h1>
        <p className="login-subtitle">
          {error || t("backendUnavailable.subtitle", "The backend is unavailable or rejected this browser origin.")}
        </p>
        <button className="login-submit" type="button" onClick={onRetry}>
          {t("backendUnavailable.retry", "Retry")}
        </button>
      </div>
    </div>
  );
}

function LazySurfaceFallback() {
  const { t } = useTranslation();
  return <div className="app-surface-loading">{t("app.loading", "Loading...")}</div>;
}

function BetterAgentBrandMark({ className = "" }: { className?: string }) {
  const classes = ["better-agent-brand-mark", className].filter(Boolean).join(" ");
  return (
    <span className={classes} aria-hidden="true">
      B
    </span>
  );
}

function capabilityContextFromPickerSource(
  source: ProviderConfigSyncCapabilityPickerSource,
  output?: ProviderConfigSyncCapabilityPickerOutput,
): CapabilityContext {
  const outputs = output ? [output] : source.outputs;
  return {
    source_id: source.source_id,
    capability_id: source.capability.capability_id,
    name: source.capability.name,
    category: source.capability.category,
    outputs: outputs
      .filter((item) => item.content && !item.render_error)
      .map((item) => ({
        provider_kind: item.provider_kind,
        provider_name: item.provider_name,
        content_kind: item.content_kind,
        content: item.content,
      })),
  };
}

export default function App() {
  // Server URL gate — on Capacitor native, require the user to enter
  // the backend URL on first launch before anything else runs. The
  // WebView stays at http://localhost/ for the lifetime of the app;
  // auth crosses origins via bearer token (see bearerAuth.ts).
  const [serverUrlReady] = useState(() => {
    if (typeof Capacitor !== "undefined" && Capacitor.isNativePlatform()) {
      return !!localStorage.getItem("better_agent_server_url");
    }
    return true;
  });

  if (!serverUrlReady) {
    return <ServerSetup onConfigured={() => window.location.reload()} />;
  }

  // Auth gate — every API call and the /ws/chat WebSocket require
  // a valid better_agent_session cookie. Render <Login /> while
  // unauthenticated; bounce to it again on WS-1008 close. The auth
  // check is mounted ABOVE the adv-sync branch so a drill-down
  // window opened by an unauth user lands on the login screen too.
  const [authStatus, setAuthStatus] = useState<
    "loading" | "anon" | "authed" | "setup" | "unreachable"
  >("loading");
  const [authProbeError, setAuthProbeError] = useState<string>("");
  const [authedUser, setAuthedUser] = useState<{ username: string } | null>(
    null
  );
  // Native-only: prompt when the backend has a newer APK staged.
  const { update: nativeUpdate, dismiss: dismissNativeUpdate } =
    useNativeAppUpdate();
  const { offer: desktopInstallOffer, dismiss: dismissDesktopInstallOffer } =
    useDesktopInstallOffer();
  const [donationRedirect, setDonationRedirect] =
    useState<DonationRedirect | null>(() => {
      if (typeof window === "undefined") return null;
      return donationRedirectFromLocation();
    });

  const checkAuth = useCallback(async (retryCount = 0) => {
    try {
      const r = await fetch(`${API}/api/auth/me`, { credentials: "include" });
      if (r.status === 200) {
        setAuthedUser(await r.json());
        setAuthProbeError("");
        setAuthStatus("authed");
      } else {
        if (r.status !== 401) {
          setAuthedUser(null);
          setAuthProbeError(
            r.status === 403
              ? "Backend rejected this browser origin."
              : `Backend auth probe failed with status ${r.status}.`
          );
          setAuthStatus("unreachable");
          return;
        }
        // Unauthenticated. On a fresh install (no credentials yet) the
        // backend reports needs_setup — show the first-run <Setup />
        // screen instead of <Login />.
        try {
          const s = await fetch(`${API}/api/auth/needs_setup`, {
            credentials: "include",
          });
          if (s.ok && (await s.json()).needs_setup) {
            setAuthedUser(null);
            setAuthProbeError("");
            setAuthStatus("setup");
            return;
          }
          if (!s.ok) {
            setAuthedUser(null);
            setAuthProbeError(
              s.status === 403
                ? "Backend rejected this browser origin."
                : `Backend setup probe failed with status ${s.status}.`
            );
            setAuthStatus("unreachable");
            return;
          }
        } catch {
          setAuthedUser(null);
          setAuthProbeError("Could not reach the backend.");
          setAuthStatus("unreachable");
          return;
        }
        setAuthedUser(null);
        setAuthProbeError("");
        setAuthStatus("anon");
      }
    } catch (e) {
      if (retryCount < 3) {
        // Linear backoff: 1s, 2s, 3s
        setTimeout(() => checkAuth(retryCount + 1), (retryCount + 1) * 1000);
      } else {
        setAuthedUser(null);
        setAuthProbeError("Could not reach the backend.");
        setAuthStatus("unreachable");
      }
    }
  }, []);

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  // Logout — POST clears the better_agent_session cookie; reload so React
  // re-mounts the top-level App and re-runs the auth check.
  // replaceState to "/" so the post-login session lands on the Ask
  // entry view rather than re-entering whatever /s/<id> was open.
  const handleLogout = useCallback(async () => {
    try {
      await fetch(`${API}/api/auth/logout`, {
        method: "POST",
        credentials: "include",
      });
    } finally {
      // Bearer-token clients also need the local token cleared — the
      // cookie endpoint only kills the cookie. (Same call is a no-op
      // on cookie-only browsers because nothing is stored.)
      clearStoredToken();
      window.history.replaceState(null, "", "/");
      window.location.reload();
    }
  }, []);

  // Re-gate when the WS reports an auth failure.
 useWebSocket
  // surfaces this via a custom event so we don't have to thread
  // a callback through the whole tree.
  useEffect(() => {
    const onAuthFail = () => {
      setAuthedUser(null);
      // Re-evaluate rather than hard-forcing "anon". On a fresh install
      // the correct unauthenticated screen is <Setup/>, not <Login/>, and
      // during the initial "loading" render AppMain briefly mounts and its
      // WS auth-failure fires this event — without the re-check it would
      // clobber the "setup" state with "anon" a beat after load.
      checkAuth();
    };
    window.addEventListener("better-agent-auth-failed", onAuthFail);
    return () => window.removeEventListener("better-agent-auth-failed", onAuthFail);
  }, [checkAuth]);

  if (authStatus === "setup") {
    return <Setup onComplete={checkAuth} />;
  }

  if (authStatus === "anon") {
    return <Login onSuccess={checkAuth} />;
  }

  if (authStatus === "unreachable") {
    return <BackendUnavailable error={authProbeError} onRetry={() => checkAuth()} />;
  }

  // Mobile-app download — the QR points at `/?download=android|ios`. We
  // reach here only once authed (the anon/setup gates returned above), so
  // an unauthenticated phone saw <Login /> first; after login this renders
  // and auto-starts the (now-authenticated) APK/IPA download.
  const downloadPlatform = (() => {
    const d = new URLSearchParams(window.location.search).get("download");
    return d === "android" || d === "ios" ? d : null;
  })();

  if (downloadPlatform) {
    return <DownloadRedirect platform={downloadPlatform} />;
  }

  // Adv-sync drill-down mode — when the URL carries
  // ?adv_sync_overlay=<id>&parent=<id>, render the dedicated
  // AdvSyncWindow instead of the regular workspace.
  const advSyncParams = (() => {
    const p = new URLSearchParams(window.location.search);
    const overlayId = p.get("adv_sync_overlay");
    const parentId = p.get("parent");
    return overlayId && parentId ? { overlayId, parentId } : null;
  })();

  if (advSyncParams) {
    return (
      <AdvSyncWindow
        overlayId={advSyncParams.overlayId}
        parentId={advSyncParams.parentId}
      />
    );
  }

  return (
    <>
      <AppMain
        authStatus={authStatus}
        authedUser={authedUser}
        onLogout={handleLogout}
        suppressDonationWelcome={!!donationRedirect}
      />
      {donationRedirect && (
        <DonationRedirectNotice
          open={authStatus === "authed"}
          status={donationRedirect.status}
          checkoutId={donationRedirect.checkoutId}
          onClose={() => {
            clearDonationRedirectFromUrl();
            setDonationRedirect(null);
          }}
        />
      )}
      {nativeUpdate && (
        <UpdatePopup
          versionName={nativeUpdate.versionName}
          onDismiss={dismissNativeUpdate}
        />
      )}
      {desktopInstallOffer && (
        <DesktopInstallPrompt
          offer={desktopInstallOffer}
          onDismiss={dismissDesktopInstallOffer}
        />
      )}
    </>
  );
}

interface AppMainProps {
  authStatus: "loading" | "authed";
  authedUser: { username: string } | null;
  onLogout: () => void;
  suppressDonationWelcome: boolean;
}

const BUILTIN_FLAG_KEYS = [
  "ask",
  "team",
  "supervisor",
  "projectStructure",
  "machineNodes",
  "credentialBroker",
  "providerConfigSync",
  "canvas",
  "rearranger",
  "promptEngineer",
  "browserHarness",
  "testape",
] as const;

type BuiltinExtensionFlags = Record<(typeof BUILTIN_FLAG_KEYS)[number], boolean>;

const DEFAULT_BUILTIN_EXTENSION_FLAGS: BuiltinExtensionFlags = {
  ask: true,
  team: true,
  supervisor: true,
  projectStructure: true,
  machineNodes: true,
  credentialBroker: true,
  providerConfigSync: true,
  canvas: true,
  rearranger: true,
  promptEngineer: true,
  browserHarness: true,
  testape: true,
};

function useBuiltinExtensionFlags(authStatus: "loading" | "authed"): BuiltinExtensionFlags {
  const [flags, setFlags] = useState<BuiltinExtensionFlags>(DEFAULT_BUILTIN_EXTENSION_FLAGS);

  const refresh = useCallback(async () => {
    if (authStatus !== "authed") return;
    try {
      const res = await fetch(`${API}/api/extensions`, { credentials: "include" });
      if (!res.ok) return;
      const payload = await res.json();
      const records = Array.isArray(payload.extensions) ? payload.extensions : [];
      const next = { ...DEFAULT_BUILTIN_EXTENSION_FLAGS };
      for (const key of BUILTIN_FLAG_KEYS) {
        const record = records.find((item: any) => item?.manifest?.id === extId(key));
        next[key] = record ? record.enabled === true : false;
      }
      setFlags(next);
    } catch {
      setFlags(DEFAULT_BUILTIN_EXTENSION_FLAGS);
    }
  }, [authStatus]);

  useEffect(() => {
    void refresh();
    const off = eventBus.subscribe("extensions_changed", () => {
      void refresh();
    });
    return off;
  }, [refresh]);

  return flags;
}

function AppMain({
  authStatus,
  authedUser,
  onLogout,
  suppressDonationWelcome,
}: AppMainProps) {
  const { t, i18n } = useTranslation();
  const sessionToolbarModules = useExtensionFrontendModules("session-toolbar");
  const mobileSessionTopbarModules = useExtensionFrontendModules("mobile-session-topbar");
  const teamSidebarModules = useExtensionFrontendModules("team-sidebar");
  const routePageModules = useExtensionFrontendModules("route-page");
  const sidebarScopeModules = useExtensionFrontendModules("sidebar-scope-tabs");
  const globalApprovalModules = useExtensionFrontendModules("global-approval-overlay");
  const canvasPanelModules = useExtensionFrontendModules("right-panel-canvas");
  const screenPanelModules = useExtensionFrontendModules("right-panel-screen");
  const askGreetingModules = useExtensionFrontendModules("ask-greeting");
  const askSessionPickerModules = useExtensionFrontendModules("ask-session-picker");
  const sessionActionModalModules = useExtensionFrontendModules("session-action-modal");
  const sessionWorkspaceOverlayModules = useExtensionFrontendModules("session-workspace-overlay");
  const sessionDragOverlayModules = useExtensionFrontendModules("session-drag-overlay");
  const builtinExtensions = useBuiltinExtensionFlags(authStatus);
  useAttentionSound();

  // The session id currently being dragged in the sidebar, or null. Pure
  // transient UI state bridged from the `session_drag_start/end` facts so
  // the agent-board extension's drop overlay can reveal itself via context.
  const [draggingSession, setDraggingSession] = useState<{ id: string; name: string } | null>(null);
  useEffect(() => {
    const offStart = eventBus.subscribe("session_drag_start", (p) =>
      setDraggingSession({ id: p.session_id, name: p.name ?? "" }),
    );
    const offEnd = eventBus.subscribe("session_drag_end", () =>
      setDraggingSession(null),
    );
    return () => {
      offStart();
      offEnd();
    };
  }, []);

  // Responsive layout mode. Width-only (see useViewport docs).
  // When mode !== 'desktop' the sidebar and right-panel become
  // overlay drawers; ephemeral open/closed state lives here.
  const viewport = useViewport();
  const isMobile = viewport.mode !== "desktop";
  const isPortrait = viewport.height > viewport.width;
  // Multi-machine deploys surface a Machines link in the sidebar.
  const { machines } = useMachines(authStatus);
  const showMachinesLink = builtinExtensions.machineNodes && machines.length > 1;
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [mobileHeaderMenuOpen, setMobileHeaderMenuOpen] = useState(false);
  const mobileHeaderMenuRef = useRef<HTMLDivElement>(null);
  const [mobileRightOpen, setMobileRightOpen] = useState(false);
  const [mobileRightFullscreen, setMobileRightFullscreen] = useState(false);
  const closeMobileRightPanel = useCallback(() => {
    setMobileRightOpen(false);
    setMobileRightFullscreen(false);
  }, []);
  // Hardware/browser back closes the drawer instead of navigating
  // away. Each drawer is its own modal layer (nested mobile sheets
  // close innermost-first via the hook's module-scope stack).
  useBackButtonDismiss(mobileSidebarOpen, () => setMobileSidebarOpen(false));
  useBackButtonDismiss(mobileRightOpen, closeMobileRightPanel);
  // Close the mobile header overflow menu on outside click.
  useEffect(() => {
    if (!mobileHeaderMenuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (!mobileHeaderMenuRef.current?.contains(e.target as Node)) {
        setMobileHeaderMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [mobileHeaderMenuOpen]);
  // Drive --vv-offset only when a virtual keyboard is plausible.
  useVisualViewport(isMobile);
  // Close drawers automatically when transitioning to desktop so
  // we don't leave drawer state set incorrectly.
  useEffect(() => {
    if (!isMobile) {
      setMobileSidebarOpen(false);
      setMobileRightOpen(false);
      setMobileRightFullscreen(false);
    }
  }, [isMobile]);
  // Escape closes whichever mobile drawer is open. Matches the
  // ARIA modal-dialog pattern (drawers carry role="dialog"
  // aria-modal="true" when open). Only active on mobile because
  // the drawers don't exist on desktop. Stops propagation so a
  // modal nested inside the drawer can still own its own Escape.
  useEffect(() => {
    if (!isMobile) return;
    if (!mobileSidebarOpen && !mobileRightOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      setMobileSidebarOpen(false);
      closeMobileRightPanel();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [isMobile, mobileSidebarOpen, mobileRightOpen, closeMobileRightPanel]);
  // Keyboard shortcut: Alt+R opens project structure edits
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "r" || !e.altKey) return;
      e.preventDefault();
      handleProjectStructureEditRef.current?.();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
  // Forward-declared ref so the keyboard handler can call the latest
  // version of handleProjectStructureEdit without being a dep.
  const handleProjectStructureEditRef = useRef<() => void>(() => {});
  const { route, navigate } = useRoute();
  // Set before a navigate() that must NOT auto-close the mobile
  // sidebar (e.g. switching projects keeps the menu open so the new
  // project's session list stays visible). Consumed by the close
  // effect on the next route change.
  const skipSidebarCloseOnNavRef = useRef(false);
  // Close the sidebar drawer whenever the user navigates to a
  // different session. The inline close in the onSelect handler
  // covers the happy path; this effect catches edge cases where
  // the inline close is lost (e.g. React batching timing on some
  // mobile browsers). Declared after `useRoute()` so its `route`
  // dep doesn't trip the temporal-dead-zone check.
  useEffect(() => {
    if (skipSidebarCloseOnNavRef.current) {
      skipSidebarCloseOnNavRef.current = false;
      return;
    }
    if (isMobile) setMobileSidebarOpen(false);
  }, [route, isMobile]);
  const {
    sessions,
    sessionsLoaded,
    sessionsHasMore,
    sessionsLoadingMore,
    sessionsSearching,
    // Renamed: this is the FULL tree (root + embedded forks). The UI
    // splits "the tree" (rendered by Chat / ForkSplitView) from "the
    // focused node" (target of every send/draft/stop action). The
    // focused node is the variable named `currentSession` below; most
    // of App.tsx's logic operates on the focused node, not the tree.
    currentSession: currentTree,
    createSession,
    addOfflineSession,
    restoreOfflineSession,
    selectSession,
    clearCurrentSession,
    deleteSession,
    addMessages,
    replaceMessages,
    applyMessagesReplay,
    applyStubInvalidated,
    getSinceSeq,
    getEventsFromSeq,
    getEventsCursorKnown,
    advanceEventSeq,
    updateSessionName,
    renameSession,
    togglePin,
    unpinOtherSessions,
    archiveSession,
    toggleWorkerEligible,
    updateRearranger,
    applySessionMetadata,
    appendSessionIfNew,
    dropSessionIfPresent,
    refreshSessions,
    loadMoreSessions,
    runStateBySession,
    applyRunState,
    applyLiveEvent,
    markTurnTerminal,
    markTurnDetached,
    markTurnStale,
    applyMessageRecovering,
    applyMessageRetrying,
    applyMessageAutoRetry,
    applyMessageAskResult,
    applyMessageAskChoice,
    processingByRoot,
    applySessionProcessing,
    applySessionReconciled,
    patchMessageStatus,
    appendFork,
    allOpenSessionIds,
    getNode,
    loadOlderMessages,
    sessionLoading,
    searchSessions,
    setSessionListFilters,
    wsTargetSessionId,
  } = useSession(authStatus);
  const [donationWelcomeMilestone, setDonationWelcomeMilestone] =
    useState<number | null>(null);
  useEffect(() => {
    if (authStatus !== "authed" || !sessionsLoaded || suppressDonationWelcome) {
      setDonationWelcomeMilestone(null);
      return;
    }
    setDonationWelcomeMilestone(donationWelcomeStorage.nextMilestone(sessions.length));
  }, [authStatus, sessionsLoaded, sessions.length, suppressDonationWelcome]);

  // Split-view focus: which pane (root or fork) is the active target
  // for Send / Fork / draft etc. Stored per-root so switching between
  // sessions in the sidebar restores each session's last-focused pane
  // (focus fork B in session X, switch to Y, switch back — still B).
  // Pure UI state, ephemeral, in-memory only — per CLAUDE.md.
  const [focusedByRoot, setFocusedByRoot] = useState<Record<string, string>>({});
  const [sendTarget, setSendTarget] = useState<"worker" | "supervisor">("worker");

  // Cross-session "running" + "unread" state now lives in the
  // sessionRegistry singleton. <SessionStatusBadge> / <ProjectStatusBadge>
  // read from it via the typed eventBus — no prop drilling, no window
  // CustomEvent, no per-session callback plumbing. The registry binds
  // its bus subscriptions once on mount and bootstraps from REST.
  useEffect(() => {
    sessionRegistry.bind();
    if (authStatus === "authed" || !authStatus) {
      void sessionRegistry.bootstrap();
    }
  }, [authStatus]);

  const focusedForkId: string | null = currentTree
    ? focusedByRoot[currentTree.id] ?? currentTree.id
    : null;
  const setFocusedForkId = useCallback(
    (id: string | null) => {
      if (!currentTree) return;
      setFocusedByRoot((prev) => {
        const rid = currentTree.id;
        if (id === null || id === rid) {
          // Default-to-root: drop the entry so a future tree refresh
          // uses the natural fallback.
          if (!(rid in prev)) return prev;
          const { [rid]: _drop, ...rest } = prev;
          void _drop;
          return rest;
        }
        return { ...prev, [rid]: id };
      });
    },
    [currentTree]
  );

  /** Alias: the focused node = the session every action targets. When
   * no fork is open, this is the root itself. The 80+ references to
   * `currentSession` throughout App.tsx all want this — the focused
   * pane — so naming it `currentSession` keeps the rest of the file
   * working without a sweeping rename. */
  const currentSession =
    focusedForkId && currentTree
      ? getNode(focusedForkId) ?? currentTree
      : currentTree;
  const focusedSession = currentSession;
  // Ref mirror so callbacks (syncProvider) can read the current session
  // without stale closures or re-triggering effects.
  const currentSessionRef = useRef(currentSession);
  currentSessionRef.current = currentSession;

  // Ack-on-focus: whenever the focused (root or fork) session id
  // changes, OR whenever an unread event lands on the focused session,
  // POST /api/sessions/:sid/seen so the backend zeros the
  // unread counter and broadcasts `session_unread_changed{unread:0}`
  // to every connected tab. Debounced 300ms — a user scrolling
  // through sessions via keyboard shortcut shouldn't fire one POST
  // per row. `null` (Home view, no session focused) skips the ack.
  const { unread_count: focusedUnreadCount } = useSessionMeta(currentSession?.id);
  useEffect(() => {
    const sid = currentSession?.id;
    if (!sid || focusedUnreadCount === 0) return;
    const id = window.setTimeout(() => {
      void ackSessionSeen(sid);
    }, 300);
    return () => window.clearTimeout(id);
  }, [currentSession?.id, focusedUnreadCount]);

  // Stable per-tab id: sent in PATCH bodies for tag/draft mutations
  // and echoed back in `session_metadata_updated` WS events. The
  // useWebSocket hook drops events whose `originated_by` matches this
  // id so a debounced draft PATCH can't race ahead and clobber newer
  // keystrokes typed after the PATCH but before its broadcast lands.
  // Lazy `useState` init so the random id is generated exactly once
  // (a `useRef(<expr>)` would re-run the initializer on every render
  // even though only the first result is kept).
  const [clientId] = useState(
    () => `tab-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
  );

  type AutoOpenReason = "files" | "notes" | "canvas" | "comments" | "todos" | "navigate" | "screen";
  const [localRightPanelStates, setLocalRightPanelStates] = useLocalStorage<
    Record<string, { open?: boolean; tab?: "files" | "notes" | "canvas" | "comments" | "todos" | "screen"; todosDismissed?: boolean; autoOpenedBy?: AutoOpenReason[] }>
  >("better-agent-right-panel-states", {});

  /** Patch the persisted right-panel state for a session. Now stored in local storage instead of backend. */
  const patchRightPanel = useCallback(
    (
      sessionId: string,
      patch: {
        open?: boolean;
        tab?: "files" | "notes" | "canvas" | "comments" | "todos" | "screen";
        addAutoReason?: AutoOpenReason;
        clearAutoReasons?: boolean;
      },
    ) => {
      setLocalRightPanelStates((prev) => {
        const current = prev[sessionId] || {};
        let autoOpenedBy = [...(current.autoOpenedBy ?? [])];
        if (patch.clearAutoReasons) autoOpenedBy = [];
        if (patch.addAutoReason && !autoOpenedBy.includes(patch.addAutoReason)) {
          autoOpenedBy.push(patch.addAutoReason);
        }
        const next: typeof current = {
          ...current,
          open: patch.open ?? current.open,
          tab: patch.tab ?? current.tab,
          todosDismissed: current.todosDismissed,
          autoOpenedBy: autoOpenedBy.length > 0 ? autoOpenedBy : undefined,
        };
        return { ...prev, [sessionId]: next };
      });
    },
    [setLocalRightPanelStates],
  );

  /** Toggle the right panel. Mobile: flips `mobileRightOpen`
   * (transient drawer). Desktop: flips persisted `right_panel_open`. */
  const handleToggleRightPanel = useCallback(() => {
    if (isMobile) {
      setMobileRightOpen((v) => !v);
      setMobileRightFullscreen(false);
      setMobileSidebarOpen(false);
      return;
    }
    if (!currentSession) return;
    const state = localRightPanelStates[currentSession.id];
    const currentOpen = state?.open ?? false;
    const closing = currentOpen;
    const closingTodos = closing && (state?.tab === "todos" || state?.tab === undefined);
    patchRightPanel(currentSession.id, {
      open: !currentOpen,
      ...(closingTodos ? { todosDismissed: true } : {}),
      clearAutoReasons: true, // manual toggle — clear auto-open tracking
    });
  }, [isMobile, currentSession, patchRightPanel, localRightPanelStates]);

  /** Switch to a specific tab AND ensure the panel is open. Marks as auto-opened. */
  const openRightPanelWithTab = useCallback(
    (tab: "files" | "notes" | "canvas" | "comments" | "todos") => {
      setRightPanelTab(tab);
      if (isMobile) {
        setMobileRightOpen(true);
        setMobileRightFullscreen(false);
        setMobileSidebarOpen(false);
        return;
      }
      if (!currentSession) return;
      patchRightPanel(currentSession.id, { open: true, tab, addAutoReason: tab });
    },
    [isMobile, currentSession, patchRightPanel],
  );

  // Auto-open todos panel when todos first appear, unless the user
  // previously closed it while on the todos tab for this session.
  const prevTodosRef = useRef<unknown>(null);
  useEffect(() => {
    if (!currentSession) return;
    const todos = currentSession.current_todos;
    const hadTodos = prevTodosRef.current;
    prevTodosRef.current = todos;
    if (hadTodos) return; // already had todos — skip
    if (!todos || todos.length === 0) return; // no todos yet
    const state = localRightPanelStates[currentSession.id];
    if (state?.todosDismissed) return; // user closed it — respect
    openRightPanelWithTab("todos");
  }, [currentSession, currentSession?.current_todos, localRightPanelStates, openRightPanelWithTab]);

  const lastOpenFilePanelCountBySessionRef = useRef<Record<string, number>>({});
  useEffect(() => {
    if (!currentSession) return;
    const count = currentSession.open_file_panels?.length ?? 0;
    const previous = lastOpenFilePanelCountBySessionRef.current[currentSession.id] ?? count;
    lastOpenFilePanelCountBySessionRef.current[currentSession.id] = count;
    if (count <= previous) return;
    if (isMobile) {
      setRightPanelTab("files");
      setMobileRightOpen(true);
      setMobileSidebarOpen(false);
      return;
    }
    patchRightPanel(currentSession.id, { open: true, tab: "files", addAutoReason: "files" });
    setRightPanelTab("files");
  }, [
    currentSession?.id,
    currentSession?.open_file_panels,
    isMobile,
    patchRightPanel,
  ]);

  // Auto-close: when ALL reasons the panel was auto-opened have
  // disappeared, close the panel. Only applies when `autoOpenedBy`
  // is non-empty — manual opens are never auto-closed.
  const autoReasonHasContent = useCallback(
    (reason: AutoOpenReason): boolean => {
      if (!currentSession) return false;
      switch (reason) {
        case "todos":
          return (currentSession.current_todos?.length ?? 0) > 0;
        case "comments":
          return (currentSession.inline_tags?.length ?? 0) > 0;
        case "files":
          return (currentSession.open_file_panels?.length ?? 0) > 0;
        case "notes":
          return (currentSession.notes?.length ?? 0) > 0;
        case "canvas":
          return false;
        case "screen":
          return false;
        case "navigate": {
          return (
            (currentSession.inline_tags?.length ?? 0) > 0 ||
            (currentSession.notes?.length ?? 0) > 0
          );
        }
      }
    },
    [currentSession],
  );

  useEffect(() => {
    if (!currentSession) return;
    const state = localRightPanelStates[currentSession.id];
    if (!state?.open || !state.autoOpenedBy?.length) return;

    const allGone = state.autoOpenedBy.every((r) => !autoReasonHasContent(r));
    if (allGone) {
      patchRightPanel(currentSession.id, { open: false, clearAutoReasons: true });
    }
  }, [
    currentSession,
    currentSession?.current_todos,
    currentSession?.inline_tags,
    currentSession?.open_file_panels,
    currentSession?.notes,
    localRightPanelStates,
    patchRightPanel,
    autoReasonHasContent,
  ]);

  // Most recent supervisor failure / cap-hit notification. Shown as
  // a small dismissible banner near the top of the chat panel; auto-
  // dismisses after 8s. Kept as a single slot (latest wins) — these
  // events are rare enough that queuing isn't worth the complexity.
  const [supervisorBanner, setSupervisorBanner] = useState<{
    kind: string;
    message: string;
    sessionId?: string;
    at: number;
  } | null>(null);
  useEffect(() => {
    if (!supervisorBanner) return;
    // `await_user` banners persist until the user dismisses them or
    // sends a new message — they're an actionable hint about what
    // input the worker is blocked on, not a transient failure notice.
    if (supervisorBanner.kind === "await_user") return;
    const t = setTimeout(() => setSupervisorBanner(null), 8000);
    return () => clearTimeout(t);
  }, [supervisorBanner]);
  const handleSupervisorEvent = useCallback(
    (info: {
      sessionId?: string;
      kind: string;
      message?: string;
      error?: string;
      reason?: string;
    }) => {
      let text: string;
      if (info.kind === "await_user") {
        text = info.reason
          ? `Supervisor: please answer — ${info.reason}`
          : "Supervisor: the worker is waiting on your input.";
      } else {
        text = info.message
          || (info.error ? `Supervisor error: ${info.error}` : `Supervisor: ${info.kind}`);
      }
      setSupervisorBanner({
        kind: info.kind,
        message: text,
        sessionId: info.sessionId,
        at: Date.now(),
      });
    },
    [],
  );

  // Ephemeral PR-created toast shown in the chat panel. Single slot
  // (latest wins), auto-dismisses after 10s. Fired only on the LIVE
  // pr-link push (useWebSocket onPrLink), never on replay.
  const [prToast, setPrToast] = useState<{
    prNumber?: number;
    prUrl: string;
    prRepository?: string;
    at: number;
  } | null>(null);
  useEffect(() => {
    if (!prToast) return;
    const t = setTimeout(() => setPrToast(null), 10000);
    return () => clearTimeout(t);
  }, [prToast]);
  const handlePrLink = useCallback(
    (info: {
      sessionId?: string;
      prNumber?: number;
      prUrl: string;
      prRepository?: string;
    }) => {
      setPrToast({
        prNumber: info.prNumber,
        prUrl: info.prUrl,
        prRepository: info.prRepository,
        at: Date.now(),
      });
    },
    [],
  );

  // Supervisor prompt modal — shown when enabling supervisor so the user
  // can edit the per-turn custom prompt before activation.
  const [supervisorPromptModalOpen, setSupervisorPromptModalOpen] = useState(false);
  // "enable" — confirm button reads Enable and activates supervisor.
  // "edit" — confirm button reads Save; supervisor is already enabled, the
  // enabled:true write in onConfirm is a no-op and only the prompt updates.
  const [supervisorPromptModalMode, setSupervisorPromptModalMode] = useState<"enable" | "edit">("enable");
  // INVARIANT: the modal is bound to whichever session was current
  // when it opened — `onConfirm` writes via `currentSession.id` at
  // click time. Force-close on session switch so a stale modal can't
  // misroute the user's prompt to the new session, and so in-progress
  // typing for session A doesn't silently bleed into session B's
  // textarea after the next open.
  useEffect(() => {
    setSupervisorPromptModalOpen(false);
  }, [currentSession?.id]);

  // Overlay state is DERIVED from the backend session record per the
  // state-ownership rule (CLAUDE.md). Whenever `currentSession.working_mode`
  // says the focused session is in a working mode, the matching overlay
  // mounts. No local shadowing — selecting the session in any way (sidebar
  // click, fresh start, resume badge) auto-mounts via re-render.
  const promptEngState = useMemo<PromptEngState | null>(() => {
    if (!currentSession) return null;
    if (currentSession.working_mode !== "prompt_engineering") return null;
    const meta = currentSession.working_mode_meta;
    if (!meta?.parent_session_id || !meta?.temp_file_path) return null;
    return {
      engSessionId: currentSession.id,
      parentSessionId: meta.parent_session_id,
      tempFilePath: meta.temp_file_path,
      originalContent: meta.original_content ?? "",
      mode: meta.mode ?? "fork",
    };
  }, [currentSession]);

  const fileEditingState = useMemo<FileEditingState | null>(() => {
    if (!currentSession) return null;
    if (currentSession.working_mode !== "file_editing") return null;
    const meta = currentSession.working_mode_meta;
    if (!meta?.file_paths || meta.file_paths.length === 0) return null;
    return {
      sessionId: currentSession.id,
      filePaths: meta.file_paths,
      originalContents: meta.original_contents ?? {},
      fileDiscussions: meta.file_discussions ?? [],
    };
  }, [currentSession]);

  const rightPanelOpenDesktop =
    (localRightPanelStates[currentSession?.id ?? ""]?.open ?? false) && !!currentSession;
  const rightPanelVisible =
    !promptEngState &&
    !fileEditingState &&
    (isMobile ? mobileRightOpen : rightPanelOpenDesktop);

  const fileEditingPersistent = Boolean(
    currentSession?.working_mode === "file_editing" &&
    currentSession?.working_mode_meta?.persistent,
  );

  /** Start a file-editor session for a given file path. Idempotent on
   * the backend (resumes an existing one for the same file). After
   * `selectSession`, derivation auto-mounts the overlay. */
  const startFileEditor = useCallback(async (filePath: string) => {
    const cwd = currentSession?.cwd || "";
    const model = currentSession?.model || "";
    if (!model) {
      throw new Error("Current session has no model configured");
    }
    const providerId = (currentSession as unknown as Record<string, unknown> | undefined)?.provider_id as string | undefined;
    try {
      const handle = progressTrackPromise(
        `fileEditor:start:${filePath}`,
        async () => {
          const r = await fetch(`${API}/api/file-editor`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              file_path: filePath,
              cwd,
              model,
              provider_id: providerId,
              reasoning_effort: currentSession?.reasoning_effort || "",
            }),
          });
          if (!r.ok) {
            const err = await r.json().catch(() => ({ detail: r.statusText }));
            throw new Error(err.detail || `HTTP ${r.status}`);
          }
          return (await r.json()) as { session_id: string };
        },
      );
      const data = await handle.promise;
      // Keep the op in-flight until the file-editor session's first
      // meta-prompt turn completes — the REST returns the session id
      // before claude has produced any output.
      const fileEditSid = data.session_id;
      handle.armWSExtender(makeSessionExtender(fileEditSid, "turn_complete"));
      await selectSession(fileEditSid);
      setViewingFile(null);
      setProjectSettingsCwd(null);
    } catch (e) {
      alert(t("app.fileEditorStartFailed") + (e instanceof Error ? e.message : e));
    }
  }, [currentSession, selectSession, t]);

  /** Done: navigate away from the temporal file-edit session. The
   * session record stays alive on disk and is resumable via AI Edit
   * on the same file (idempotent backend resume). Only shown for the
   * temporal flavor — persistent file-sessions have no Done button. */
  const handleFileEditorDone = useCallback(async () => {
    clearCurrentSession();
  }, [clearCurrentSession]);

  /** Cancel: tear down the session entirely, then navigate away so
   * derivation yields null and the overlay unmounts. */
  const handleFileEditorCancel = useCallback(async () => {
    if (!fileEditingState) return;
    try {
      await progressTrackedFetch(
        `fileEditor:cancel:${fileEditingState.sessionId}`,
        `${API}/api/file-editor/${fileEditingState.sessionId}`,
        { method: "DELETE" },
      );
    } catch { /* best effort */ }
    clearCurrentSession();
  }, [fileEditingState, clearCurrentSession]);

  /** Set when the user clicks "⚙ Engineer" with a non-empty draft.
   * Carries the trimmed draft text so the modal's mode pick fires the
   * POST without re-reading state. Cleared when the modal closes. */
  const [promptEngModalDraft, setPromptEngModalDraft] = useState<string | null>(null);
  /** UI-only error string shown to the user when starting the eng
   * session fails (e.g. fork mode picked but parent has no agent_sid).
   * Cleared on next open. */
  const [promptEngStartError, setPromptEngStartError] = useState<string>("");

  const handleRearrangerUpdate = useCallback(
    (u: RearrangerUpdate) => {
      updateRearranger(u.appSessionId, {
        tree: u.tree,
        rearranger_session_id: u.rearrangerSessionId,
        last_message_count: u.lastMessageCount,
        rearranger_stats: u.rearrangerStats,
        token_usage_total: u.tokenUsageTotal,
        token_usage_last: u.tokenUsageLast,
      });
    },
    [updateRearranger]
  );
  const handleRearrangerState = useCallback(
    (s: RearrangerStateUpdate) => {
      updateRearranger(s.appSessionId, { enabled: s.enabled });
    },
    [updateRearranger]
  );

  const initialOfflineState = useMemo(() => {
    const pendingQueueText: Record<string, string> = {};
    const pendingBySession: Record<string, ChatMessage[]> = {};
    const pendingImages: Record<string, PastedImage[]> = {};
    const pendingFiles: Record<string, FileAttachment[]> = {};
    try {
      const raw = localStorage.getItem("better_agent_offline_queue");
      if (raw) {
        const queue = JSON.parse(raw) as import("./hooks/useOfflineQueue").OfflineQueueEntry[];
        for (const entry of queue) {
          const sessionId = entry.type === "create_session" ? entry.session.id : entry.sessionId;
          
          if (entry.prompt) {
            pendingBySession[sessionId] = [...(pendingBySession[sessionId] || []), {
              id: entry.clientId,
              role: "user",
              content: entry.prompt,
              events: [],
              timestamp: entry.type === "create_session" ? (entry.session.created_at || new Date().toISOString()) : new Date().toISOString(),
              isStreaming: false,
              status: "offline"
            }];
            
            if (entry.type !== "create_session" && entry.sendMode === "queue") {
              pendingQueueText[sessionId] = entry.prompt;
              if (entry.images?.length) {
                pendingImages[sessionId] = entry.images.map(img => ({
                  mediaType: img.media_type,
                  base64: img.data,
                  dataUrl: `data:${img.media_type};base64,${img.data}`,
                  file: new File([], "image"), // dummy file for typing
                }));
              }
              if (entry.files?.length) {
                pendingFiles[sessionId] = entry.files.map(f => ({
                  name: f.name,
                  mediaType: f.media_type,
                  base64: f.data,
                  size: f.size,
                  file: new File([], f.name), // dummy file
                }));
              }
            }
          }
        }
      }
    } catch (err) {
      void err;
    }
    return { pendingQueueText, pendingBySession, pendingImages, pendingFiles };
  }, []);

  // Optimistic user bubbles, keyed by app_session_id. Declared up here
  // (above useWebSocket) so `handleUserMessagePersisted` can clear the
  // pending entry directly when the ack frame arrives, without having
  // to round-trip through React's events buffer (which a tight burst
  // of frames can wipe via `turn_start`'s `setEvents([])` before the
  // events-effect runs).
  const [pendingBySession, setPendingBySession] = useState<
    Record<string, ChatMessage[]>
  >(initialOfflineState.pendingBySession);
  const setPendingForSession = useCallback(
    (sessionId: string, updater: (prev: ChatMessage[]) => ChatMessage[]) => {
      setPendingBySession((all) => {
        const prev = all[sessionId] ?? [];
        const next = updater(prev);
        if (next.length === 0) {
          if (!(sessionId in all)) return all;
          const { [sessionId]: _drop, ...rest } = all;
          void _drop;
          return rest;
        }
        return { ...all, [sessionId]: next };
      });
    },
    []
  );
  const removePendingByClientId = useCallback((pendingClientId: string) => {
    setPendingBySession((all) => {
      let changed = false;
      const next: typeof all = {};
      for (const [sid, msgs] of Object.entries(all)) {
        const filtered = msgs.filter((m) => m.id !== pendingClientId);
        if (filtered.length !== msgs.length) {
          changed = true;
          if (filtered.length > 0) next[sid] = filtered;
        } else {
          next[sid] = msgs;
        }
      }
      return changed ? next : all;
    });
  }, []);
  const removePendingForSessionByClientId = useCallback(
    (sessionId: string, pendingClientId: string) => {
      setPendingForSession(sessionId, (prev) =>
        prev.filter((message) => message.id !== pendingClientId)
      );
    },
    [setPendingForSession]
  );
  // Textbox-merge slot: the full text of the most recent in-flight send
  // per session, set synchronously in handleSend BEFORE the backend acks
  // queued vs immediate. The queue banner (`queuedBySession`) is a pure
  // backend mirror — set only on `prompt_queued`. This slot bridges the
  // gap so a fast double-send can merge against the previous text even
  // before the backend ack arrives. Cleared on `prompt_queued` (text
  // moves into `queuedBySession.preview`) or `user_message_persisted`
  // (immediate dispatch, no merge needed).
  const [pendingQueueTextBySession, setPendingQueueTextBySession] = useState<
    Record<string, string>
  >(initialOfflineState.pendingQueueText);
  // Ref mirror so the WS closure handlers can read the latest map
  // without re-subscribing on every keystroke. Ref must be updated
  // SYNCHRONOUSLY at every write site — a `useEffect`-based mirror
  // races against the network: a fast `prompt_queued` arriving in
  // the same tick as the corresponding `setState` would see the
  // stale ref (effect runs after commit, network roundtrip can
  // beat it on localhost). Helper below enforces ref-then-state
  // ordering at every mutation site.
  const pendingQueueTextRef = useRef<Record<string, string>>(initialOfflineState.pendingQueueText);
  const writePendingQueueText = useCallback(
    (sid: string, value: string | null) => {
      const prev = pendingQueueTextRef.current;
      let next: Record<string, string>;
      if (value === null) {
        if (!(sid in prev)) return;
        const { [sid]: _drop, ...rest } = prev;
        void _drop;
        next = rest;
      } else {
        if (prev[sid] === value) return;
        next = { ...prev, [sid]: value };
      }
      pendingQueueTextRef.current = next;
      setPendingQueueTextBySession(next);
    },
    [],
  );
  // Stash images/files sent with an in-flight queued prompt so the banner can
  // display thumbnails. Cleared when prompt_queued ack arrives (they
  // move into queuedBySession) or on user_message_persisted / cancel.
  const pendingQueueImagesRef = useRef<Record<string, PastedImage[]>>(initialOfflineState.pendingImages);
  const pendingQueueFilesRef = useRef<Record<string, FileAttachment[]>>(initialOfflineState.pendingFiles);
  const offlineQueue = useOfflineQueue();
  const removeAckedOfflineAction = offlineQueue.removeBySessionAndClient;
  const offlineDispatchedRef = useRef<Set<string>>(new Set());
  const [offlineRetryTick, setOfflineRetryTick] = useState(0);
  const ackedRef = useRef<Set<string>>(new Set());
  const ackedClientIdsRef = useRef<Set<string>>(new Set());
  const skipNextPendingAppendBySessionRef = useRef<Set<string>>(new Set());
  const appendPendingForSession = useCallback(
    (sessionId: string, pendingMsg: ChatMessage) => {
      logPromptSend("pending_append", {
        app_session_id: sessionId,
        client_id: pendingMsg.id,
        status: pendingMsg.status ?? null,
        content_length: pendingMsg.content.length,
      });
      setPendingForSession(sessionId, (prev) => {
        return appendPendingUnlessAcked(prev, sessionId, pendingMsg, {
          ackedClientIds: ackedClientIdsRef.current,
          skipNextAppendBySession: skipNextPendingAppendBySessionRef.current,
        });
      });
    },
    [setPendingForSession],
  );
  const handleUserMessagePersisted = useCallback(
    (sessionId: string, userMessage: ChatMessage) => {
      if (ackedRef.current.has(userMessage.id)) return;
      logPromptSend("user_message_persisted", {
        app_session_id: sessionId,
        message_id: userMessage.id,
        client_id: userMessage.client_id ?? null,
        content_length: userMessage.content.length,
      });
      ackedRef.current.add(userMessage.id);
      addMessages(sessionId, [userMessage]);
      // A queued prompt being persisted means it was actually sent to the
      // agent — clear the queued banner for this session.
      setQueuedForSession(sessionId, null);
      // The textbox-merge slot is the pre-ack mirror for this very prompt
      // — once it lands as a persisted user message, the slot has served
      // its purpose. Clear it so a later send doesn't re-merge against
      // text that's already in the timeline.
      writePendingQueueText(sessionId, null);
      // Clear stashed queue attachments for this session.
      if (sessionId in pendingQueueImagesRef.current) {
        const { [sessionId]: _drop, ...rest } = pendingQueueImagesRef.current;
        void _drop;
        pendingQueueImagesRef.current = rest;
      }
      if (sessionId in pendingQueueFilesRef.current) {
        const { [sessionId]: _drop, ...rest } = pendingQueueFilesRef.current;
        void _drop;
        pendingQueueFilesRef.current = rest;
      }
      const cid = userMessage.client_id ?? null;
      if (cid) {
        ackedClientIdsRef.current.add(cid);
        offlineDispatchedRef.current.delete(cid);
        removeAckedOfflineAction(sessionId, cid);
      }
      if (cid) {
        removePendingForSessionByClientId(sessionId, cid);
      } else {
        // No client_id (legacy): clear all pending for the acked session.
        setPendingBySession((all) => {
          const prev = all[sessionId] ?? [];
          if (prev.length === 0) {
            skipNextPendingAppendBySessionRef.current.add(sessionId);
            return all;
          }
          skipNextPendingAppendBySessionRef.current.delete(sessionId);
          const { [sessionId]: _drop, ...rest } = all;
          void _drop;
          return rest;
        });
      }
      // Refresh sidebar so timestamps + sort order update immediately.
      refreshSessions();
    },
    [addMessages, refreshSessions, writePendingQueueText, removeAckedOfflineAction, removePendingForSessionByClientId]
  );
  const handleSteerPromptPersisted = useCallback(
    (_sessionId: string, steerClientId?: string | null) => {
      if (!steerClientId) return;
      logPromptSend("steer_prompt_persisted", {
        app_session_id: _sessionId,
        client_id: steerClientId,
      });
      offlineDispatchedRef.current.delete(steerClientId);
      removeAckedOfflineAction(_sessionId, steerClientId);
      removePendingByClientId(steerClientId);
    },
    [removeAckedOfflineAction, removePendingByClientId]
  );
  const handlePromptSendError = useCallback(
    (sessionId: string, promptClientId: string, errorText: string) => {
      if (ackedClientIdsRef.current.has(promptClientId)) return;
      logPromptSend("prompt_send_error", {
        app_session_id: sessionId,
        client_id: promptClientId,
        error: errorText,
      }, "error");
      offlineDispatchedRef.current.delete(promptClientId);
      removeAckedOfflineAction(sessionId, promptClientId);
      setPendingForSession(sessionId, (prev) =>
        prev.map((m) =>
          m.id === promptClientId
            ? { ...m, status: "error" as const, errorText }
            : m
        )
      );
    },
    [removeAckedOfflineAction, setPendingForSession]
  );

  // Forward-declared shim so useWebSocket's `onProjectsChanged` option can
  // dispatch to refreshProjects, which is declared further down the file.
  // The ref is patched by an effect right after refreshProjects's
  // declaration. Stable identity ⇒ no churn in useWebSocket option deps.
  const refreshProjectsRef = useRef<() => void>(() => {});
  const handleProjectsChanged = useCallback(() => {
    refreshProjectsRef.current();
  }, []);

  const [projectUpdatesCounts, setProjectUpdatesCounts] = useState<Record<string, number>>({});
  const setProjectUpdatesCount = useCallback((projectId: string, count: number) => {
    setProjectUpdatesCounts(prev => ({ ...prev, [projectId]: count }));
  }, []);

  const {
    connected,
    sendMessage,
    stopStreaming,
    sendPromoteQueued,
    sendCancelQueued,
    sendUpdateQueued,
    events,
    traceSteps,
    isStreaming,
    isStopping,
    streamingLoadPhase,
    lastResult,
    streamingAppSessionId,
  } = useWebSocket(WS_URL, {
    onRearrangerUpdate: handleRearrangerUpdate,
    onRearrangerState: handleRearrangerState,
    currentAppSessionId: wsTargetSessionId,
    // Subscribe to every pane in the open tree. The focused pane is
    // already covered by `currentAppSessionId`; this list carries the
    // rest. useWebSocket de-duplicates and diffs against the previous
    // set so subscribe/unsubscribe frames only fire on actual changes.
    additionalAppSessionIds: allOpenSessionIds().filter(
      (id) => id !== focusedSession?.id
    ),
    onRewindComplete: replaceMessages,
    onMessagesReplay: applyMessagesReplay,
    onStubInvalidated: applyStubInvalidated,
    onMessagesDelta: applyMessagesReplay, // same upsert reducer
    onUserMessagePersisted: handleUserMessagePersisted,
    onSteerPromptPersisted: handleSteerPromptPersisted,
    onPromptSendError: handlePromptSendError,
    onRunState: applyRunState,
    onLiveTurnEvent: applyLiveEvent,
    onTurnTerminal: markTurnTerminal,
    onTurnDetached: markTurnDetached,
    onTurnStale: markTurnStale,
    onMessageRecoveringChanged: applyMessageRecovering,
    onMessageRetryingChanged: applyMessageRetrying,
    onMessageAutoRetryChanged: applyMessageAutoRetry,
    onMessageAskResultChanged: applyMessageAskResult,
    onMessageAskChoiceChanged: applyMessageAskChoice,
    onSessionProcessing: applySessionProcessing,
    onSessionReconciled: applySessionReconciled,
    getSinceSeq,
    getEventsFromSeq,
    getEventsCursorKnown,
    onEventSeqAdvance: advanceEventSeq,
    onSessionMetadataUpdated: (sessionId: string, patch: SessionMetadataPatch) => {
      // Drop draft fields from a stale/out-of-order echo: while this tab is
      // actively typing (pending debounce), or when the incoming
      // draft_input_seq is not newer than the one we already hold (e.g. the
      // pre-send text broadcast arriving after the clear-on-send). Otherwise
      // it would resurrect just-sent text into the composer.
      const storedSeq = getNode(sessionId)?.draft_input_seq;
      const toApply = filterStaleDraftPatch(
        patch,
        storedSeq,
        draftDebounceRef.current.has(sessionId),
      );
      applySessionMetadata(sessionId, toApply);
      if ("queued_prompts" in patch) {
        const queuedPrompts = (patch.queued_prompts ?? []) as QueuedPrompt[];
        const first = queuedPrompts[0] ?? null;
        setQueuedForSession(sessionId, first
          ? { id: first.id, preview: first.content }
          : null);
      }
    },
    onSessionForked: appendFork,
    onSessionCreated: appendSessionIfNew,
    onSessionDeleted: dropSessionIfPresent,
    onSessionRenamed: updateSessionName,
    onProjectsChanged: handleProjectsChanged,
    onProjectUpdatesChanged: (data) => {
      setProjectUpdatesCount(data.project_id, data.unseen_count);
    },
    onWorkersChanged: refreshSessions,
    onSessionOrganizationChanged: refreshSessions,
    onProjectMappingsChanged: () => {
      window.dispatchEvent(new CustomEvent("project_mappings_changed"));
    },
    onSupervisorEvent: handleSupervisorEvent,
    onPrLink: handlePrLink,
    onPromptQueued: (data) => {
      logPromptSend("app_prompt_queued", {
        app_session_id: data.app_session_id,
        queued_id: data.queued_id,
        client_id: data.client_id ?? null,
        send_mode: data.send_mode,
        queue_position: data.queue_position,
        pending_queue_text: data.app_session_id in pendingQueueTextRef.current,
      });
      // The backend just confirmed the queue entry. Prefer the full
      // text we stashed at send time over the backend's truncated
      // prompt_preview, then drop the textbox-merge slot — the banner
      // now owns the canonical preview.
      const fullText = pendingQueueTextRef.current[data.app_session_id];
      const queuedImages = pendingQueueImagesRef.current[data.app_session_id];
      const queuedFiles = pendingQueueFilesRef.current[data.app_session_id];
      setQueuedForSession(data.app_session_id, {
        id: data.queued_id,
        preview: fullText ?? data.prompt_preview,
        ...(queuedImages?.length ? { images: queuedImages } : {}),
        ...(queuedFiles?.length ? { files: queuedFiles } : {}),
      });
      writePendingQueueText(data.app_session_id, null);
      // Clear stashed attachments — they've moved into queuedBySession.
      if (data.app_session_id in pendingQueueImagesRef.current) {
        const { [data.app_session_id]: _drop, ...rest } = pendingQueueImagesRef.current;
        void _drop;
        pendingQueueImagesRef.current = rest;
      }
      if (data.app_session_id in pendingQueueFilesRef.current) {
        const { [data.app_session_id]: _drop, ...rest } = pendingQueueFilesRef.current;
        void _drop;
        pendingQueueFilesRef.current = rest;
      }
      // Remove the optimistic pending message bubble — the queued banner
      // on top of the input area is the single surface for queued state.
      // The real message will appear via user_message_persisted when the
      // queue drains and the backend processes the prompt.
      if (data.client_id) {
        offlineDispatchedRef.current.delete(data.client_id);
        removeAckedOfflineAction(data.app_session_id, data.client_id);
        removePendingByClientId(data.client_id);
      }
    },
    // User-message lifecycle — map the 5 backend states onto the
    // user message's `status` field so MessageStatus renders them.
    onUserMsgLifecycle: (_appSessionId: string, event) => {
      const d = event.data as { lifecycle_msg_id?: string; client_id?: string; kind?: string; error?: string; reason?: string };
      if (!d.lifecycle_msg_id) return;
      logPromptSend("app_lifecycle", {
        app_session_id: _appSessionId,
        event: event.type,
        lifecycle_msg_id: d.lifecycle_msg_id,
        client_id: d.client_id ?? null,
        kind: d.kind ?? null,
        error: d.error ?? d.reason,
      }, event.type === "user_message_failed" ? "warn" : "info");
      switch (event.type) {
        case "user_message_queued":
          if (d.client_id) {
            offlineDispatchedRef.current.delete(d.client_id);
            removeAckedOfflineAction(_appSessionId, d.client_id);
            if (d.kind === "queued_behind") {
              removePendingByClientId(d.client_id);
            }
          }
          break;
        case "user_message_sent":
          patchMessageStatus(_appSessionId, d.lifecycle_msg_id, "sending");
          break;
        case "user_message_received":
          patchMessageStatus(_appSessionId, d.lifecycle_msg_id, "received");
          break;
        case "user_message_done":
          patchMessageStatus(_appSessionId, d.lifecycle_msg_id, undefined);
          break;
        case "user_message_failed":
          patchMessageStatus(_appSessionId, d.lifecycle_msg_id, "error", d.error ?? d.reason);
          break;
      }
    },
    onTurnStarted: (appSessionId: string) => {
      setQueuedForSession(appSessionId, null);
    },
    onQueueConsumed: (data) => {
      setQueuedForSession(data.app_session_id, null);
    },
    onAnyEvent: progressHandleWSEvent,
    clientId: clientId,
  });

  const refreshSessionInventory = useCallback(() => {
    refreshSessions();
    void sessionRegistry.bootstrap();
  }, [refreshSessions]);

  useEffect(() => {
    if (!connected) return;
    refreshSessionInventory();
  }, [connected, refreshSessionInventory]);

  useEffect(() => {
    if (!Capacitor.isNativePlatform()) return;
    const handle = CapApp.addListener("appStateChange", (state: AppState) => {
      if (state.isActive) refreshSessionInventory();
    });
    return () => {
      void handle.then((h) => h.remove());
    };
  }, [refreshSessionInventory]);

  useEffect(() => {
    if (!connected || offlineQueue.queue.length === 0) return;
    const timer = window.setInterval(() => {
      offlineDispatchedRef.current.clear();
      setOfflineRetryTick((tick) => tick + 1);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [connected, offlineQueue.queue.length]);

  // Active provider + model are read-only in the main UI — both come
  // from the provider record and only the settings dialog can change
  // them. We mirror them into local state on mount and on every
  // `provider_changed` WS broadcast so all tabs converge.
  const [model, setModel] = useState("");
  const [providers, setProviders] = useState<Provider[]>([]);
  const [defaultProviderId, setDefaultProviderId] = useState<string | null>(null);
  const currentProvider = useMemo(() => {
    const providerId = currentSession?.provider_id;
    if (!providerId) return null;
    return providers.find((p) => p.id === providerId) ?? null;
  }, [providers, currentSession?.provider_id]);
  const defaultProvider = useMemo(
    () => providers.find((p) => p.id === defaultProviderId) ?? null,
    [providers, defaultProviderId],
  );
  const currentSessionCanSteer = !!currentProvider?.supports_steering;
  const [, setProviderName] = useState("");
  const syncProvider = useCallback(async () => {
    try {
      const r = await fetch(`${API}/api/providers`);
      const pd = (await r.json()) as {
        default_provider_id: string | null;
        providers: Provider[];
      };
      cacheProviders(pd.providers, pd.default_provider_id);
      setProviders(pd.providers);
      setDefaultProviderId(pd.default_provider_id);
      const active = pd.providers.find((p) => p.id === pd.default_provider_id);
      if (active) {
        setProviderName(active.name);
        // Only set model to the active provider's default when no session
        // is selected. When a session IS selected, the model must come from
        // the session record — setting it here triggers the drift detector
        // which PATCHes the active provider's default onto sessions that
        // use a DIFFERENT provider (e.g. Gemini session + Z.AI active
        // → model gets overwritten to glm-5.1).
        if (!currentSessionRef.current) {
          setModel(active.last_model || active.default_model || "");
        }
      } else {
        setProviderName("");
        if (!currentSessionRef.current) {
          setModel("");
        }
      }
    } catch {
      // ignore — UI keeps stale label until next sync
    }
  }, []);
  useEffect(() => {
    if (authStatus === "authed" || !authStatus) {
      syncProvider();
    }
  }, [syncProvider, authStatus]);
  useProviderChanged(syncProvider);
  const [newSessionModalOpen, setNewSessionModalOpen] = useState(false);
  const [investigationCtx, setInvestigationCtx] = useState<InvestigationContext | undefined>(undefined);
  const [turnCapabilityPickerOpen, setTurnCapabilityPickerOpen] = useState(false);
  const [turnCapabilityContextsBySession, setTurnCapabilityContextsBySession] = useState<Record<string, CapabilityContext[]>>({});

  useEffect(() => {
    for (const entry of offlineQueue.queue) {
      if (entry.type === "create_session") {
        restoreOfflineSession({ ...entry.session, offline_pending: true });
      }
    }
  }, [offlineQueue.queue, restoreOfflineSession]);
  // Pending investigation auto-submit: stored when a session is created with
  // investigation context. A useEffect watches for the target session to be
  // the current session AND the WS to be connected, then submits the prompt.
  const pendingInvestigationRef = useRef<{
    sessionId: string;
    prompt: string;
    images: ImagePayload[];
    files: FilePayload[];
    model: string;
    cwd: string;
    orchestrationMode: OrchestrationMode;
    capabilityContexts: CapabilityContext[];
  } | null>(null);

  const sendInitialPromptToSession = useCallback(
    (pending: NonNullable<typeof pendingInvestigationRef.current>) => {
      const clientId = `investigate-${Date.now()}`;
      const pendingMsg: ChatMessage = {
        id: clientId,
        role: "user",
        content: pending.prompt,
        events: [],
        timestamp: new Date().toISOString(),
        isStreaming: false,
        status: "sending",
      };
      appendPendingForSession(pending.sessionId, pendingMsg);
      if (pending.images.length > 0) {
        retryPayloadsRef.current.set(pendingMsg.id, pending.images);
      }
      const sent = sendMessage(
        pending.prompt,
        pending.model,
        pending.cwd,
        null,
        pending.sessionId,
        pending.images.length > 0 ? pending.images : undefined,
        pending.orchestrationMode,
        clientId,
        undefined,
        undefined,
        pending.files.length > 0 ? pending.files : undefined,
        pending.capabilityContexts,
      );
      if (sent) return true;
      setPendingForSession(pending.sessionId, (prev) =>
        prev.filter((m) => m.id !== clientId)
      );
      retryPayloadsRef.current.delete(clientId);
      return false;
    },
    [sendMessage, setPendingForSession, appendPendingForSession],
  );

  // Reliable investigation auto-submit: waits for the WS to be connected AND
  // the target session to be the current session before sending. Retries
  // automatically on reconnect; cleared on send or if the user navigates away.
  useEffect(() => {
    const pending = pendingInvestigationRef.current;
    if (!pending) return;
    if (!connected) return;
    if (currentSession?.id !== pending.sessionId) return;
    // Session is active and WS is open — safe to send.
    pendingInvestigationRef.current = null;
    if (!sendInitialPromptToSession(pending)) {
      // WS not actually open — roll back and keep pending for next trigger.
      pendingInvestigationRef.current = pending;
    }
  }, [connected, currentSession?.id, sendInitialPromptToSession]);

  // Flush the durable offline-action backlog sequentially. Creation
  // actions use their client-generated session UUID as the backend id,
  // making retries idempotent across reconnects and reloads.
  const offlineFlushRunningRef = useRef(false);
  useEffect(() => {
    if (!connected) offlineDispatchedRef.current.clear();
  }, [connected]);
  useEffect(() => {
    if (!connected || offlineFlushRunningRef.current) return;
    offlineFlushRunningRef.current = true;
    void (async () => {
      try {
        for (const entry of offlineQueue.getAll()) {
          if (offlineDispatchedRef.current.has(entry.clientId)) continue;
          logPromptSend("offline_flush_attempt", {
            type: entry.type,
            app_session_id: entry.type === "create_session" ? entry.session.id : entry.sessionId,
            client_id: entry.clientId,
            queue_size: offlineQueue.queue.length,
          });
          if (entry.type === "create_session") {
            const queued = entry.session;
            await createSession({
              name: queued.name,
              model: queued.model,
              cwd: queued.cwd,
              orchestrationMode: queued.orchestration_mode,
              browserHarnessEnabled: queued.browser_harness_enabled,
              providerId: queued.provider_id,
              browserHarnessHeadless: queued.browser_harness_headless,
              nodeId: queued.node_id,
              reasoningEffort: queued.reasoning_effort,
              permission: queued.permission,
              clientSessionId: queued.id,
              capabilityContexts: entry.capabilityContexts,
              folderId: queued.folder_id,
            });
            const images = entry.images?.length ? entry.images : undefined;
            const offlineFiles = entry.files?.length ? entry.files : undefined;
            if (entry.prompt) {
              offlineDispatchedRef.current.add(entry.clientId);
              const sent = sendMessage(
                entry.prompt,
                queued.model,
                queued.cwd,
                null,
                queued.id,
                images,
                queued.orchestration_mode,
                entry.clientId,
                undefined,
                undefined,
                offlineFiles,
                entry.capabilityContexts,
              );
              if (!sent) {
                logPromptSend("offline_flush_ws_not_open", {
                  type: entry.type,
                  app_session_id: queued.id,
                  client_id: entry.clientId,
                }, "warn");
                offlineDispatchedRef.current.delete(entry.clientId);
                return;
              }
              logPromptSend("offline_flush_dispatched", {
                type: entry.type,
                app_session_id: queued.id,
                client_id: entry.clientId,
              });
              setPendingForSession(queued.id, (prev) =>
                prev.map((m) =>
                  m.id === entry.clientId ? { ...m, status: "sending" as const } : m
                ),
              );
            } else {
              offlineQueue.remove(entry.clientId);
            }
            continue;
          }

          const images = entry.images?.length ? entry.images : undefined;
          const offlineFiles = entry.files?.length ? entry.files : undefined;
          offlineDispatchedRef.current.add(entry.clientId);
          const sent = sendMessage(
            entry.prompt,
            entry.model,
            entry.cwd,
            null,
            entry.sessionId,
            images,
            entry.orchestrationMode,
            entry.clientId,
            entry.sendMode,
            entry.sendTarget,
            offlineFiles,
            entry.capabilityContexts,
          );
          if (!sent) {
            logPromptSend("offline_flush_ws_not_open", {
              type: entry.type,
              app_session_id: entry.sessionId,
              client_id: entry.clientId,
            }, "warn");
            offlineDispatchedRef.current.delete(entry.clientId);
            return;
          }
          logPromptSend("offline_flush_dispatched", {
            type: entry.type,
            app_session_id: entry.sessionId,
            client_id: entry.clientId,
          });
          setPendingForSession(entry.sessionId, (prev) =>
            prev.map((m) =>
              m.id === entry.clientId ? { ...m, status: "sending" as const } : m
            ),
          );
        }
      } catch (error) {
        logPromptSend("offline_flush_error", {
          error: error instanceof Error ? error.message : String(error),
        }, "error");
        // Keep the failed action and all following actions for reconnect.
      } finally {
        offlineFlushRunningRef.current = false;
      }
    })();
  }, [connected, offlineQueue, createSession, sendMessage, setPendingForSession, offlineRetryTick]);

  // Clear stale pending investigation if the user navigates to a different
  // session and the pending target is no longer the current route. This
  // prevents the base64 image data from lingering in memory indefinitely.
  useEffect(() => {
    const pending = pendingInvestigationRef.current;
    if (!pending) return;
    // Clear when we're not on the pending target session (a different
    // session, or a non-session route like /machines).
    if (route.kind !== "session" || route.sessionId !== pending.sessionId) {
      pendingInvestigationRef.current = null;
    }
  }, [route]);

  const [cwd, setCwd] = useState("");
  // Pre-send project-mismatch prompt (advisory). Resolves the deferred
  // send decision: "move" the fresh session to the suggested project,
  // "here" to send anyway, "cancel" to abort the send.
  const [projectSuggestion, setProjectSuggestion] = useState<{
    suggestion: ProjectSuggestion;
    resolve: (d: "move" | "here" | "cancel") => void;
  } | null>(null);
  const [selectedProjectPath, setSelectedProjectPath] = useState(() => {
    return localStorage.getItem("better-agent-selected-project") || "";
  });
  // Multi-machine: which node's filesystem the selected project lives
  // on. Sibling state to `selectedProjectPath` (kept separate to avoid
  // a localStorage shape migration that would discard the legacy
  // path-only key — legacy users keep their path with node_id defaulting
  // to "primary", the sentinel for the local node).
  const [selectedProjectNodeId, setSelectedProjectNodeId] = useState(() => {
    return localStorage.getItem("better-agent-selected-project-node") || "primary";
  });
  type QueuedBannerState = { id: string; preview: string; images?: PastedImage[]; imagesCount?: number; files?: FileAttachment[]; filesCount?: number };
  const [queuedBySession, setQueuedBySession] = useState<
    Record<string, QueuedBannerState>
  >({});
  const persistedQueuedPrompt = useMemo((): QueuedBannerState | null => {
    const first = currentSession?.queued_prompts?.[0];
    return first ? { id: first.id, preview: first.content, imagesCount: first.images_count, filesCount: first.files_count } : null;
  }, [currentSession?.queued_prompts]);
  const queuedPrompt = currentSession
    ? (currentSession.id in queuedBySession
        ? queuedBySession[currentSession.id] ?? null
        : persistedQueuedPrompt)
    : null;
  const setQueuedForSession = useCallback(
    (
      sessionId: string,
      value:
        | QueuedBannerState
        | null
        | ((prev: QueuedBannerState | null) => QueuedBannerState | null)
    ) => {
      setQueuedBySession((all): Record<string, QueuedBannerState> => {
        const current = all[sessionId] ?? null;
        const resolved = typeof value === "function" ? value(current) : value;
        if (!resolved) {
          if (all[sessionId] === null) return all;
          return { ...all, [sessionId]: null } as Record<string, QueuedBannerState>;
        }
        return { ...all, [sessionId]: resolved };
      });
    },
    [],
  );
  const [shortcutResponses, setShortcutResponses] = useState<string[]>([]);
  // Open-session tabs bar prefs (backend-owned). Reflected here so the
  // tabs bar can be hidden and its order chosen from Settings.
  const [sessionTabsSort, setSessionTabsSort] = useState("last_opened_at");
  const [sessionTabsStatusSort, setSessionTabsStatusSort] = useState(false);
  const [sessionTabsVisible, setSessionTabsVisible] = useState(true);
  useEffect(() => {
    const apply = (d: {
      sessions_tabs_sort?: unknown;
      sessions_tabs_status_sort?: unknown;
      sessions_tabs_visible?: unknown;
    }) => {
      if (typeof d.sessions_tabs_sort === "string") setSessionTabsSort(d.sessions_tabs_sort);
      if (typeof d.sessions_tabs_status_sort === "boolean") setSessionTabsStatusSort(d.sessions_tabs_status_sort);
      if (typeof d.sessions_tabs_visible === "boolean") setSessionTabsVisible(d.sessions_tabs_visible);
    };
    const off = eventBus.subscribe("user_prefs_changed", (p) => apply(p as Record<string, unknown>));
    return off;
  }, []);
  // When tabs status sort is on, recompute the tab order on live status
  // deltas (tabs are fully loaded, so no refetch needed — just re-rank).
  const [tabsStatusTick, setTabsStatusTick] = useState(0);
  useEffect(() => {
    if (!sessionTabsStatusSort) return;
    let timer: number | undefined;
    const bump = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setTabsStatusTick((t) => t + 1), 60);
    };
    const unsub = subscribeMany([
      ["session_monitoring_changed", bump],
      ["session_running_changed", bump],
      ["session_unread_changed", bump],
      ["session_marker_changed", bump],
    ]);
    return () => {
      unsub();
      window.clearTimeout(timer);
    };
  }, [sessionTabsStatusSort]);
  const firstRunWizardOpenedRef = useRef(false);
  // Load user prefs (language + shortcuts) from backend after auth
  useEffect(() => {
    if (authStatus !== "authed") return;
    progressTrackedFetch("userPrefs:load", `${API}/api/user-prefs`)
      .then((r) => r.json())
      .then((data) => {
        if (data.language && data.language !== i18n.language) {
          i18n.changeLanguage(data.language);
        }
        if (data.shortcut_responses) {
          setShortcutResponses(data.shortcut_responses);
        }
        if (typeof data.sessions_tabs_sort === "string") {
          setSessionTabsSort(data.sessions_tabs_sort);
        }
        if (typeof data.sessions_tabs_status_sort === "boolean") {
          setSessionTabsStatusSort(data.sessions_tabs_status_sort);
        }
        if (typeof data.sessions_tabs_visible === "boolean") {
          setSessionTabsVisible(data.sessions_tabs_visible);
        }
        if (data.first_run_wizard_done === false && !firstRunWizardOpenedRef.current) {
          firstRunWizardOpenedRef.current = true;
          navigate("/settings");
          markFirstRunWizardSeen().catch(() => {});
        }
        const appearancePrefs = data as Partial<AppearancePrefs>;
        applyAppearancePrefs(appearancePrefs);
        window.dispatchEvent(
          new CustomEvent("appearance_prefs_changed", { detail: appearancePrefs }),
        );
      })
      .catch(() => {});
  }, [authStatus, navigate]);
  useEffect(() => {
    const handler = (e: Event) => {
      applyAppearancePrefs((e as CustomEvent<AppearancePrefs>).detail);
    };
    window.addEventListener("appearance_prefs_changed", handler);
    return () => window.removeEventListener("appearance_prefs_changed", handler);
  }, []);
  // Listen for shortcut_responses changes from ShortcutSettings in SettingsPage
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (Array.isArray(detail)) setShortcutResponses(detail);
    };
    window.addEventListener("shortcut_responses_changed", handler);
    return () => window.removeEventListener("shortcut_responses_changed", handler);
  }, []);
  const [viewingFile, setViewingFile] = useState<ViewingFile | null>(null);
  const [rightPanelTab, setRightPanelTab] = useState<
    "files" | "canvas" | "notes" | "comments" | "todos" | "screen"
  >("files");
  useEffect(() => {
    if (!builtinExtensions.canvas && rightPanelTab === "canvas") {
      setRightPanelTab("files");
    }
    if (!builtinExtensions.testape && rightPanelTab === "screen") {
      setRightPanelTab("files");
    }
  }, [builtinExtensions.canvas, builtinExtensions.testape, rightPanelTab]);
  const [sessionTokenUsage, setSessionTokenUsage] =
    useState<TokenUsage | null>(null);
  const [sessionTokenUsageLast, setSessionTokenUsageLast] =
    useState<TokenUsage | null>(null);
  // pendingBySession is declared above (right before useWebSocket) so
  // the user_message_persisted callback can clear it imperatively.
  // Each session owns its own pending list so a prompt mid-flight in
  // session A does not bleed into session B's view.
  // Reuse the frozen EMPTY_MSGS so the no-pending branch returns a stable
  // reference — a fresh [] on every render needlessly churned the
  // allMessages/useMemo dependency.
  const pendingMessages = currentSession
    ? pendingBySession[currentSession.id] ?? (EMPTY_MSGS as ChatMessage[])
    : (EMPTY_MSGS as ChatMessage[]);
  useEffect(() => {
    publishBetterAgentTestApeState({
      authStatus,
      connected,
      viewport: viewport.mode,
      sessions,
      currentSession,
      openSessionIds: allOpenSessionIds(),
      pendingMessageCount: pendingMessages.length,
      queuedPromptCount: currentSession?.queued_prompts?.length ?? 0,
      rightPanelOpen: rightPanelVisible,
      rightPanelTab,
    });
  }, [
    authStatus,
    connected,
    viewport.mode,
    sessions,
    currentSession,
    allOpenSessionIds,
    pendingMessages.length,
    currentSession?.queued_prompts?.length,
    rightPanelVisible,
    rightPanelTab,
  ]);
  const retryPayloadsRef = useRef<Map<string, ImagePayload[]>>(new Map());
  const [restarting, setRestarting] = useState(false);
  const [refreshModalOpen, setRefreshModalOpen] = useState(false);
  // Persistent (non-transient) surfacing of a failed in-app refresh —
  // e.g. the backend 409 "requires the run.sh supervisor". A blocking
  // window.alert is easy to dismiss and miss; this banner stays until
  // the user closes it or a refresh succeeds.
  const [restartError, setRestartError] = useState<string | null>(null);
  const [projectSettingsCwd, setProjectSettingsCwd] = useState<string | null>(null);
  // When the sidebar's AI search is active, the SessionList computes
  // its filtered list against ALL sessions (bypassing the project
  // filter) so cross-project matches surface. We dim ProjectTabs to
  // signal that selecting a project won't narrow the results.
  const [aiSearchActive, setAiSearchActive] = useState(false);

  // Triggers the prod-mode refresh flow: POSTs /api/admin/restart (which
  // SIGTERMs the backend), polls /api/build-info until the matching
  // supervised build result is available, then hard-reloads the page
  // so the browser pulls the new HTML+JS bundle. The supervisor waits for
  // the new backend to become healthy before rebuilding the frontend. This
  // full refresh is the user's only path to picking up code changes in this
  // mode — there is no HMR and no uvicorn --reload.
  const openRefreshModal = useCallback(() => {
    if (restarting) return;
    setRefreshModalOpen(true);
  }, [restarting]);

  const handleRefreshApp = useCallback(async (mode: RefreshMode) => {
    if (restarting) return;
    setRefreshModalOpen(false);
    setRestartError(null);
    setRestarting(true);
    const requestId = uuidv4();
    saveRefreshContext(requestId);
    let accepted = false;
    try {
      const res = await fetch(`${API}/api/admin/restart`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId, mode }),
      });
      if (!res.ok) {
        clearRefreshContext();
        setRestarting(false);
        let detail = "";
        try {
          detail = ((await res.json()) as { detail?: string })?.detail ?? "";
        } catch {
          /* non-JSON body — fall back to the generic i18n message */
        }
        setRestartError(detail || t("app.refreshUnavailable"));
        return;
      }
      accepted = true;
    } catch {
      accepted = false;
    }
    const deadline = Date.now() + 120_000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 500));
      try {
        const res = await fetch(
          `${API}/api/admin/restart-status/${encodeURIComponent(requestId)}`,
          { cache: "no-store" },
        );
        const info = res.ok
          ? await res.json() as RestartStatus
          : null;
        if (!accepted && info?.accepted === false) {
          clearRefreshContext();
          setRestarting(false);
          setRestartError(t("app.refreshNotAccepted"));
          return;
        }
        if (info?.accepted) {
          accepted = true;
        }
        if (info?.refresh_result?.request_id === requestId) {
          await hardRefreshCurrentPage(requestId);
          return;
        }
      } catch {
        // Backend still down — keep polling.
      }
    }
    // Gave up waiting; surface the failure and let the user retry.
    clearRefreshContext();
    setRestarting(false);
    setRestartError(t("app.refreshTimeout"));
  }, [restarting, t]);
  // Memoized (not mapped per render) so memo(MessageGroup)'s shallow
  // compare keeps working across per-WS-frame parent re-renders; the
  // frozen module-level singleton keeps the empty case reference-stable.
  // `displayNumber` is the 1-based footnote number shown in the comments
  // panel and as the highlight's reference marker.
  const sessionInlineTags = currentSession?.inline_tags;
  const tags = useMemo(
    () =>
      sessionInlineTags?.length
        ? sessionInlineTags.map((t, i) => ({ ...t, displayNumber: i + 1 }))
        : (EMPTY_INLINE_TAGS as import("./types/inlineTag").InlineTag[]),
    [sessionInlineTags],
  );
  const [focusedCommentId, setFocusedCommentId] = useState<string | null>(null);
  // Aggressively emphasize the focused comment's highlight spans.
  // Module-level in tagHighlights so spans re-created by a later
  // highlight pass come up already focused.
  useEffect(() => {
    setFocusedTagHighlight(focusedCommentId);
  }, [focusedCommentId]);
  /** ID of a newly created tag with empty comment — auto-starts edit mode
   *  in CommentsPanel. Cleared once editing begins. */
  const [autoEditId, setAutoEditId] = useState<string | null>(null);
  /** When a comment is focused, find its highlight span in the chat,
   *  scroll it into view. Falls back to scrolling to the message container
   *  if highlight spans aren't available (e.g. user-message tags). */
  const handleFocusComment = useCallback(
    (id: string | null) => {
      setFocusedCommentId(id);
      if (!id) return;
      requestAnimationFrame(() => {
        scrollCommentTargetIntoView(id, tags);
      });
    },
    [tags],
  );

  const handleAddTag = useCallback(
    (text: string, comment: string, messageId: string) => {
      if (!currentSession) return;
      const tag = {
        id: `tag-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        messageId,
        selectedText: text,
        comment,
        timestamp: new Date().toISOString(),
      };
      applySessionMetadata(currentSession.id, (session) => ({
        inline_tags: [...(session.inline_tags ?? []), tag],
      }));
      openRightPanelWithTab("comments");
      if (!comment) setAutoEditId(tag.id);
      progressTrackedFetch(
        `tag:add:${currentSession.id}:${tag.id}`,
        `${API}/api/sessions/${currentSession.id}/tags`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...tag, client_id: clientId }),
        },
      ).catch(() => {});
    },
    [currentSession, applySessionMetadata, clientId]
  );
  const handleRemoveTag = useCallback(
    (id: string) => {
      if (!currentSession) return;
      applySessionMetadata(currentSession.id, (session) => ({
        inline_tags: (session.inline_tags ?? []).filter((t) => t.id !== id),
      }));
      setFocusedCommentId((prev) => (prev === id ? null : prev));
      progressTrackedFetch(
        `tag:remove:${currentSession.id}:${id}`,
        `${API}/api/sessions/${currentSession.id}/tags/${id}` +
          `?client_id=${encodeURIComponent(clientId)}`,
        { method: "DELETE" },
      ).catch(() => {});
    },
    [currentSession, applySessionMetadata, clientId]
  );

  const handleUpdateTag = useCallback(
    (id: string, updates: { comment?: string }) => {
      if (!currentSession) return;
      applySessionMetadata(currentSession.id, (session) => ({
        inline_tags: (session.inline_tags ?? []).map((t) =>
          t.id === id ? { ...t, ...updates } : t,
        ),
      }));
      progressTrackedFetch(
        `tag:update:${currentSession.id}:${id}`,
        `${API}/api/sessions/${currentSession.id}/tags/${id}` +
          `?client_id=${encodeURIComponent(clientId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(updates),
        },
      ).catch(() => {});
    },
    [currentSession, applySessionMetadata, clientId]
  );

  /** Kick off an adversarial-sync ping-pong for the selected text.
   * Anchored to the parent root (currentTree), NOT the focused fork
   * — overlays attach to messages that live on the displayed tree,
   * and the message_id is the root's. The backend spawns the two
   * forks + driver task; WS pushes drive the rest. */
  const handleAdvSync = useCallback(
    (text: string, messageId: string) => {
      if (!currentTree) return;
      progressTrackedFetch(
        `advSync:start:${currentTree.id}:${messageId}`,
        `${API}/api/sessions/${currentTree.id}/adv_sync`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message_id: messageId,
            selected_text: text,
          }),
        },
      ).catch((e) => {
        alert(t("app.adversarialSyncFailed", "Adversarial sync failed to start: ") + (e?.message ?? String(e)));
      });
    },
    [currentTree]
  );

  /** Click handler on a converged adversarial-sync agreed-text span.
   * Opens a dedicated browser window rendered by AdvSyncWindow that
   * loads the parent tree, finds the overlay, and displays the two
   * forks side-by-side. The main app session view stays linear —
   * adv-sync panes never appear inline. */
  const handleAdvSyncClick = useCallback(
    (overlay: import("./types").AdvSyncOverlay) => {
      if (!currentTree) return;
      const url =
        `${window.location.origin}${window.location.pathname}` +
        `?adv_sync_overlay=${encodeURIComponent(overlay.id)}` +
        `&parent=${encodeURIComponent(currentTree.id)}`;
      window.open(
        url,
        `adv-sync-${overlay.id}`,
        "width=1400,height=900,resizable=yes,scrollbars=yes",
      );
    },
    [currentTree]
  );

  // Live editor handles for the open file panels, keyed by path
  // (the stable panel identity). Populated by FilePanels via each
  // FileViewer's onEditorReady. Read at prompt-send time to snapshot
  // the user's current viewport/selection — never persisted.
  const openFileEditorsRef = useRef<Map<string, FileEditorHandle>>(
    new Map(),
  );
  const registerEditor = useCallback(
    (path: string, handle: FileEditorHandle | null) => {
      if (handle) openFileEditorsRef.current.set(path, handle);
      else openFileEditorsRef.current.delete(path);
    },
    [],
  );

  /** Open a file as a backend-owned panel (tabbed/split viewer).
   * Persistent state → optimistic applySessionMetadata + REST
   * round-trip + WS convergence, exactly mirroring inline tags. The
   * client mints the id and sends it so the optimistic + persisted
   * ids match; de-dupe by path matches the backend. */
  const handleOpenFilePanel = useCallback(
    (path: string, focus?: FileFocus | null, selection?: FileFocus | null) => {
      if (!currentSession) return;
      // Local user-initiated open: force the panel open + switch to
      // Files tab so the new panel is visible immediately.
      if (isMobile) {
        setMobileRightOpen(true);
        setMobileSidebarOpen(false);
      } else {
        patchRightPanel(currentSession.id, { open: true, tab: "files", addAutoReason: "files" });
      }
      setRightPanelTab("files");
      const cwd = currentSession.cwd;
      const resolved =
        isAbsolutePath(path) || !cwd
          ? path
          : `${cwd.replace(/\/$/, "")}/${path}`;
      const panels = currentSession.open_file_panels ?? [];
      const existing = panels.find((p) => p.path === resolved);
      const id =
        existing?.id ??
        `fp-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const panel = {
        id,
        path: resolved,
        focus: focus ?? null,
        selection: selection ?? null,
      };
      const next = existing
        ? panels.map((p) => (p.path === resolved ? panel : p))
        : [...panels, panel];
      applySessionMetadata(currentSession.id, { open_file_panels: next });
      progressTrackedFetch(
        `filePanel:add:${currentSession.id}:${id}`,
        `${API}/api/sessions/${currentSession.id}/file-panels`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...panel, client_id: clientId }),
        },
      ).catch(() => {});
    },
    [currentSession, applySessionMetadata, clientId, isMobile, patchRightPanel],
  );

  const handleCloseFilePanel = useCallback(
    (id: string) => {
      if (!currentSession) return;
      const next = (currentSession.open_file_panels ?? []).filter(
        (p) => p.id !== id,
      );
      applySessionMetadata(currentSession.id, { open_file_panels: next });
      progressTrackedFetch(
        `filePanel:remove:${currentSession.id}:${id}`,
        `${API}/api/sessions/${currentSession.id}/file-panels/${id}` +
          `?client_id=${encodeURIComponent(clientId)}`,
        { method: "DELETE" },
      ).catch(() => {});
    },
    [currentSession, applySessionMetadata, clientId],
  );

  /** Pop a provider-config-sync capability panel into the right side
   *  panel from an inline `open_config_panel` tool widget's button.
   *  Mirrors handleOpenFilePanel: optimistic applySessionMetadata +
   *  REST persist, backend-owned list broadcast to every tab. */
  const handleOpenConfigPanel = useCallback(
    (panel: {
      capability_id: string;
      scope: "global" | "project";
      cwd: string;
    }) => {
      if (!currentSession) return;
      if (isMobile) {
        setMobileRightOpen(true);
        setMobileSidebarOpen(false);
      } else {
        patchRightPanel(currentSession.id, { open: true, tab: "files", addAutoReason: "files" });
      }
      setRightPanelTab("files");
      const panels = currentSession.open_config_panels ?? [];
      const existing = panels.find(
        (p) =>
          p.capability_id === panel.capability_id &&
          p.scope === panel.scope &&
          p.cwd === panel.cwd,
      );
      const id =
        existing?.id ??
        `cp-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const nextPanel = { id, ...panel };
      const next = existing
        ? panels.map((p) => (p.id === existing.id ? nextPanel : p))
        : [...panels, nextPanel];
      applySessionMetadata(currentSession.id, { open_config_panels: next });
      progressTrackedFetch(
        `configPanel:add:${currentSession.id}:${id}`,
        `${API}/api/sessions/${currentSession.id}/config-panels`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...nextPanel, client_id: clientId }),
        },
      ).catch(() => {});
    },
    [currentSession, applySessionMetadata, clientId, isMobile, patchRightPanel],
  );

  const handleCloseConfigPanel = useCallback(
    (id: string) => {
      if (!currentSession) return;
      const next = (currentSession.open_config_panels ?? []).filter(
        (p) => p.id !== id,
      );
      applySessionMetadata(currentSession.id, { open_config_panels: next });
      progressTrackedFetch(
        `configPanel:remove:${currentSession.id}:${id}`,
        `${API}/api/sessions/${currentSession.id}/config-panels/${id}` +
          `?client_id=${encodeURIComponent(clientId)}`,
        { method: "DELETE" },
      ).catch(() => {});
    },
    [currentSession, applySessionMetadata, clientId],
  );

  /** Inline "one-live-panel" registry: only the most recently mounted
   *  inline `open_config_panel` widget stays expanded; older ones collapse
   *  to a "closed" marker. Order = mount order, so the last-mounted (latest
   *  in the message stream) is active. */
  const inlineConfigOrderRef = useRef<string[]>([]);
  const [activeInlineConfigId, setActiveInlineConfigId] = useState<
    string | null
  >(null);
  const claimInlineConfigPanel = useCallback((id: string) => {
    inlineConfigOrderRef.current = [
      ...inlineConfigOrderRef.current.filter((x) => x !== id),
      id,
    ];
    setActiveInlineConfigId(
      inlineConfigOrderRef.current[inlineConfigOrderRef.current.length - 1] ??
        null,
    );
  }, []);
  const releaseInlineConfigPanel = useCallback((id: string) => {
    inlineConfigOrderRef.current = inlineConfigOrderRef.current.filter(
      (x) => x !== id,
    );
    setActiveInlineConfigId(
      inlineConfigOrderRef.current[inlineConfigOrderRef.current.length - 1] ??
        null,
    );
  }, []);

  /** File-anchored tag handler — used by the prompt-engineering
   * overlay's FileEditor and the right-panel FileViewer. Tag
   * carries a `fileAnchor` (with optional line:col) instead of a
   * `selectedText` span anchored to a message id. The synthetic
   * messageId keeps the persistence path uniform across flavors. */
  const handleAddFileAnchoredTag = useCallback(
    async (anchor: {
      filePath: string;
      comment: string;
      selectedText?: string;
      startLine?: number;
      endLine?: number;
      startCol?: number;
      endCol?: number;
    }) => {
      if (!currentSession) return;
      const fileAnchor: FileAnchor = {
        filePath: anchor.filePath,
      };
      if (
        anchor.startLine !== undefined &&
        anchor.endLine !== undefined &&
        anchor.startCol !== undefined &&
        anchor.endCol !== undefined
      ) {
        fileAnchor.startLine = anchor.startLine;
        fileAnchor.endLine = anchor.endLine;
        fileAnchor.startCol = anchor.startCol;
        fileAnchor.endCol = anchor.endCol;
      }
      const tag = {
        id: `tag-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        messageId: `__file__${anchor.filePath}`,
        selectedText: anchor.selectedText ?? "",
        comment: anchor.comment,
        timestamp: new Date().toISOString(),
        fileAnchor,
      };
      applySessionMetadata(currentSession.id, (session) => ({
        inline_tags: [...(session.inline_tags ?? []), tag],
      }));
      await progressTrackedFetch(
        `tag:add:${currentSession.id}:${tag.id}`,
        `${API}/api/sessions/${currentSession.id}/tags`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...tag, client_id: clientId }),
        },
      ).catch(() => {});
    },
    [currentSession, applySessionMetadata, clientId]
  );

  const handleStartFileDiscussion = useCallback(
    async (filePath: string, line: number): Promise<FileDiscussion> => {
      if (!currentSession) throw new Error("No active session");
      const response = await progressTrackedFetch(
        `file-discussion:start:${currentSession.id}:${filePath}:${line}`,
        `${API}/api/file-editor/${currentSession.id}/discussions`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ file_path: filePath, line, client_id: clientId }),
        },
      );
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const data = (await response.json()) as { discussion: FileDiscussion };
      applySessionMetadata(currentSession.id, (session) => {
        return {
          working_mode_meta: upsertFileDiscussionMeta(
            session.working_mode_meta,
            data.discussion,
          ),
        };
      });
      return data.discussion;
    },
    [currentSession, clientId, applySessionMetadata],
  );

  const handlePatchFileDiscussion = useCallback(
    async (discussionId: string, patch: Partial<FileDiscussion>) => {
      if (!currentSession) return;
      const response = await progressTrackedFetch(
        `file-discussion:patch:${currentSession.id}:${discussionId}`,
        `${API}/api/file-editor/${currentSession.id}/discussions/${discussionId}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...patch, client_id: clientId }),
        },
      );
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const data = (await response.json()) as { discussion: FileDiscussion };
      applySessionMetadata(currentSession.id, (session) => {
        return {
          working_mode_meta: patchFileDiscussionMeta(
            session.working_mode_meta,
            discussionId,
            data.discussion,
          ),
        };
      });
    },
    [currentSession, clientId, applySessionMetadata],
  );

  const handleSendFileDiscussionMessage = useCallback(
    async (discussionId: string, prompt: string, promptClientId: string) => {
      if (!currentSession) return;
      await progressTrackedFetch(
        `file-discussion:send:${currentSession.id}:${discussionId}:${promptClientId}`,
        `${API}/api/file-editor/${currentSession.id}/discussions/${discussionId}/messages`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ prompt, client_id: promptClientId }),
        },
      );
    },
    [currentSession],
  );

  // Per-session debounce timer for draft updates. Tracked so:
  // 1. WS guard can skip draft fields while user is typing.
  // 2. Session delete can cancel the timer to avoid wasted PATCHes.
  // 3. Session switch mid-debounce still flushes the right session's
  // pending value — switching does not implicitly cancel.
  const draftDebounceRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(
    new Map()
  );
  // On unmount, cancel every pending timer so a stranded callback
  // can't fire a PATCH against a nonexistent App.
  useEffect(() => {
    const timers = draftDebounceRef.current;
    return () => {
      for (const t of timers.values()) clearTimeout(t);
      timers.clear();
    };
  }, []);
  const flushDraftPatch = useCallback((sessionId: string, value: string, images?: import("./components/InputArea").PastedImage[]) => {
    // One monotonic seq, sent as client_seq AND stamped locally so a later
    // stale WS echo of an earlier draft can be ordered-out by the guard.
    const seq = nextDraftSeq(Date.now());
    applySessionMetadata(sessionId, { draft_input_seq: seq });
    const body: Record<string, unknown> = {
      draft_input: value,
      client_seq: seq,
      client_id: clientId,
    };
    if (images !== undefined) body.draft_images = images;
    progressTrackedFetch(
      `draft:save:${sessionId}`,
      `${API}/api/sessions/${sessionId}/draft`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
      { silent: true },
    ).catch(() => {});
  }, [clientId, applySessionMetadata]);
  const handleDraftChange = useCallback(
    (sessionId: string, value: string) => {
      const timers = draftDebounceRef.current;
      const existing = timers.get(sessionId);
      if (existing) clearTimeout(existing);
      const timer = setTimeout(() => {
        timers.delete(sessionId);
        // Apply the draft to parent state (debounced — not every keystroke).
        applySessionMetadata(sessionId, { draft_input: value });
        // Carry the current attachments so the draft save is a complete
        // snapshot. Without this, a higher-seq text-only PATCH wins the
        // stale-write guard and a slower image PATCH gets seq-dropped,
        // losing the attachments. Only the focused node owns a draft, so
        // read its latest (optimistic) images via the ref.
        const cur = currentSessionRef.current;
        const images =
          cur && cur.id === sessionId ? cur.draft_images : undefined;
        flushDraftPatch(sessionId, value, images);
      }, 300);
      timers.set(sessionId, timer);
    },
    [applySessionMetadata, flushDraftPatch]
  );
  const handleImagesChange = useCallback(
    (sessionId: string, images: import("./components/InputArea").PastedImage[], text?: string) => {
      applySessionMetadata(sessionId, { draft_images: images });
      flushDraftPatch(sessionId, text ?? currentSession?.draft_input ?? "", images);
    },
    [applySessionMetadata, flushDraftPatch]
  );

  // ── OS share-sheet ingestion ─────────────────────────────────────
  // Transient ack-bridge state (cleared the moment the user picks a
  // destination): screenshot(s) handed in by the native share sheet,
  // awaiting a session to attach to.
  const [sharedImages, setSharedImages] = useState<PastedImage[]>([]);

  // MERGE the shared image(s) into a target session's draft_images
  // (never overwrites) and persist, preserving the TARGET session's own
  // draft_input. applySessionMetadata runs synchronously so the patch is
  // visible before any navigate. Shared by both the direct-attach path
  // (open session) and the SharePicker path (chosen destination).
  const mergeImagesIntoSession = useCallback(
    (targetId: string, images: PastedImage[]) => {
      const target = sessions.find((s) => s.id === targetId);
      const { draft_input, draft_images } = buildShareDraftPatch(target, images);
      applySessionMetadata(targetId, { draft_images });
      flushDraftPatch(targetId, draft_input, draft_images);
    },
    [sessions, applySessionMetadata, flushDraftPatch]
  );

  const handleSharedImages = useCallback(
    (incoming: PastedImage[]) => {
      const open = currentSessionRef.current;
      if (open) {
        // A session is already focused — attach the screenshot(s)
        // straight to its composer instead of routing through the share
        // picker. InputArea reconciles the externally-injected
        // draft_images into its local state without a remount.
        mergeImagesIntoSession(open.id, incoming);
        return;
      }
      setSharedImages(incoming);
      navigate("/share");
    },
    [mergeImagesIntoSession, navigate]
  );
  useShareTarget(handleSharedImages);

  // Attach the shared image(s) to a chosen session's composer and open
  // it (the SharePicker callback). applySessionMetadata runs before
  // navigate so the optimistic select stub (useSession.selectSession)
  // carries the merged images into InputArea at the sessionId-change
  // mount.
  const attachImagesToSession = useCallback(
    (targetId: string) => {
      mergeImagesIntoSession(targetId, sharedImages);
      setSharedImages([]);
      navigate(sessionPath(targetId));
    },
    [mergeImagesIntoSession, sharedImages, navigate]
  );

  const cancelShare = useCallback(() => {
    setSharedImages([]);
    navigate("/");
  }, [navigate]);

  // ── Notes ─────────────────────────────────────────────────────
  const appliedNoteIdsBySessionRef = useRef<Map<string, Set<string>>>(
    new Map(),
  );
  const clearSessionInlineTags = useCallback(
    (sessionId: string) => {
      applySessionMetadata(sessionId, { inline_tags: [] });
      progressTrackedFetch(
        `tag:clearAll:${sessionId}`,
        `${API}/api/sessions/${sessionId}/tags` +
          `?client_id=${encodeURIComponent(clientId)}`,
        { method: "DELETE" },
      ).catch(() => {});
    },
    [applySessionMetadata, clientId],
  );

  const handleAddNote = useCallback(
    async (sessionId: string, text: string) => {
      try {
        const res = await fetch(`${API}/api/sessions/${sessionId}/notes`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text, client_id: clientId }),
        });
        if (res.ok) {
          const data = await res.json();
          applySessionMetadata(sessionId, { notes: data.notes });
          // Only clear draft after backend confirms the note was saved
          handleDraftChange(sessionId, "");
          // Switch to Notes tab and open the right panel
          openRightPanelWithTab("notes");
        }
      } catch { /* WS broadcast will converge */ }
    },
    [applySessionMetadata, clientId, handleDraftChange, isMobile],
  );

  const handleRemoveNote = useCallback(
    async (sessionId: string, noteId: string) => {
      try {
        await fetch(`${API}/api/sessions/${sessionId}/notes/${noteId}?client_id=${clientId}`, {
          method: "DELETE",
        });
        const notes = currentSession?.notes ?? [];
        const nextNotes = notes.filter((n) => n.id !== noteId);
        const appliedNotes = appliedNoteIdsBySessionRef.current.get(sessionId);
        const removedAppliedNote = Boolean(appliedNotes?.delete(noteId));
        if (appliedNotes && appliedNotes.size === 0) {
          appliedNoteIdsBySessionRef.current.delete(sessionId);
        }
        const patch: Partial<Session> = { notes: nextNotes };
        if (
          removedAppliedNote &&
          nextNotes.length === 0 &&
          (currentSession?.inline_tags?.length ?? 0) > 0
        ) {
          patch.inline_tags = [];
        }
        // Remove locally after backend confirms — avoid optimistic drift
        applySessionMetadata(sessionId, patch);
        if (patch.inline_tags) clearSessionInlineTags(sessionId);
      } catch { /* WS broadcast will converge */ }
    },
    [applySessionMetadata, clearSessionInlineTags, clientId, currentSession],
  );

  const handleUpdateNote = useCallback(
    async (sessionId: string, noteId: string, text: string) => {
      try {
        const res = await fetch(`${API}/api/sessions/${sessionId}/notes/${noteId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text, client_id: clientId }),
        });
        if (res.ok) {
          const data = await res.json();
          applySessionMetadata(sessionId, { notes: data.notes });
        }
      } catch { /* WS broadcast will converge */ }
    },
    [applySessionMetadata, clientId],
  );

  const handleSendNoteToPrompt = useCallback(
    (noteId: string, noteText: string) => {
      if (!currentSession) return;
      const appliedNotes =
        appliedNoteIdsBySessionRef.current.get(currentSession.id) ??
        new Set<string>();
      appliedNotes.add(noteId);
      appliedNoteIdsBySessionRef.current.set(currentSession.id, appliedNotes);
      const existing = currentSession.draft_input ?? "";
      const next = existing ? `${existing}\n${noteText}` : noteText;
      handleDraftChange(currentSession.id, next);
      // Focus the input
      const textarea = document.querySelector<HTMLTextAreaElement>('[data-testid="input-textarea"]');
      textarea?.focus();
    },
    [currentSession, handleDraftChange],
  );


  const [sessionToDelete, setSessionToDelete] = useState<string | null>(null);
  const [detailsSessionId, setDetailsSessionId] = useState<string | null>(null);

  const handleDeleteSession = useCallback((id: string) => {
    setSessionToDelete(id);
  }, []);

  const confirmDeleteSession = useCallback(async () => {
    if (!sessionToDelete) return;
    const id = sessionToDelete;
    setSessionToDelete(null);

    // Drop the per-session debounce timer so a typing-PATCH for a
    // now-deleted session doesn't fire a wasted 404 request after
    // unmount.
    const timers = draftDebounceRef.current;
    const existing = timers.get(id);
    if (existing) {
      clearTimeout(existing);
      timers.delete(id);
    }
    await deleteSession(id);
  }, [sessionToDelete, deleteSession]);

  const sessionBeingDeleted = useMemo(() => {
    if (!sessionToDelete) return null;
    return sessions.find((s) => s.id === sessionToDelete) || getNode(sessionToDelete);
  }, [sessionToDelete, sessions, getNode]);

  const handleDraftClearImmediate = useCallback(
    (sessionId: string) => {
      applySessionMetadata(sessionId, { draft_input: "", draft_images: [] });
      const timers = draftDebounceRef.current;
      const existing = timers.get(sessionId);
      if (existing) {
        clearTimeout(existing);
        timers.delete(sessionId);
      }
      flushDraftPatch(sessionId, "", []);
    },
    [applySessionMetadata, flushDraftPatch]
  );

  // Projects (persisted backend-side at ~/.better-claude/projects.json)
  const [projects, setProjects] = useState<Project[]>([]);
  const projectNameForCwd = useCallback(
    (path: string): string => {
      const p = projects.find((proj) => proj.path === path);
      return (
        p?.name ||
        path.replace(/\/+$/, "").split("/").pop() ||
        path
      );
    },
    [projects],
  );
  const [dirPickerOpen, setDirPickerOpen] = useState(false);
  const [fileChooserOpen, setFileChooserOpen] = useState(false);

  const refreshProjects = useCallback(async () => {
    try {
      const res = await progressTrackedFetch("project:list", `${API}/api/projects`);
      const data = await res.json();
      setProjects(data.projects || []);
      // Hydrate project update counts (fixes badge showing 0 until next WS event)
      if (builtinExtensions.projectStructure && data.projects?.length) {
        const cwds = data.projects.map((p: Project) => p.path);
        try {
          const countsRes = await fetch(`${extBackendBase("projectStructure")}/project-updates/counts-batch`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ cwds }),
          });
          const counts = await countsRes.json();
          setProjectUpdatesCounts(counts);
        } catch {
          // Non-critical — badge will hydrate on next WS event
        }
      } else {
        setProjectUpdatesCounts({});
      }
    } catch {
      // ignore
    }
  }, [builtinExtensions.projectStructure]);

  useEffect(() => {
    refreshProjectsRef.current = refreshProjects;
  }, [refreshProjects]);

  useEffect(() => {
    if (authStatus !== "authed") return;
    refreshProjects();
  }, [refreshProjects, authStatus]);

  // Project list refetch on backend `projects_changed` is wired
  // directly through the WS handler (`onProjectsChanged` option above);
  // no buffer-scan effect needed.

  const handleSelectProject = useCallback(
    async (path: string, nodeId: string = "primary") => {
      setCwd(path);
      setSelectedProjectPath(path);
      setSelectedProjectNodeId(nodeId);
      const target = pickSessionForProject(
        sessions,
        path,
        nodeId,
        getRememberedSessionId(path, nodeId),
      );
      skipSidebarCloseOnNavRef.current = true;
      // No session for this (machine, project) → show the empty-project
      // surface instead of falling back to the Ask singleton. Ask is
      // reachable only via its explicit button.
      navigate(target ? sessionPath(target.id) : "/empty-project");
      try {
        await progressTrackedFetch(
          `project:touch:${path}`,
          `${API}/api/projects/touch`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path, node_id: nodeId }),
          },
        );
        refreshProjects();
      } catch {
        // ignore
      }
    },
    [refreshProjects, sessions, navigate]
  );

  const handleAddProject = useCallback(
    async (path: string, nodeId: string = "primary") => {
      try {
        await progressTrackedFetch("project:add", `${API}/api/projects`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path, node_id: nodeId }),
        });
        await refreshProjects();
        setCwd(path);
        setSelectedProjectPath(path);
        setSelectedProjectNodeId(nodeId);
      } catch {
        // ignore
      } finally {
        setDirPickerOpen(false);
      }
    },
    [refreshProjects]
  );

  const handleRemoveProject = useCallback(
    async (path: string, nodeId: string = "primary") => {
      try {
        await progressTrackedFetch(
          `project:remove:${nodeId}::${path}`,
          `${API}/api/projects?path=${encodeURIComponent(path)}&node_id=${encodeURIComponent(nodeId)}`,
          { method: "DELETE" },
        );
        refreshProjects();
      } catch {
        // ignore
      }
    },
    [refreshProjects]
  );

  // Resizable panels — all three sizes persist to localStorage.
  // On mobile/tablet the resizers are inert (CSS hides them, hook
  // gates onMouseDown). The persisted size is still kept so it
  // restores on resize back to desktop.
  const sidebar = useResizable({
    storageKey: "better-agent-sidebar-width",
    defaultSize: 280,
    min: 200,
    max: 600,
    axis: "x",
    enabled: !isMobile,
  });
  const [sidebarMinimized, setSidebarMinimized] = useLocalStorage(
    "better-agent-sidebar-minimized",
    false,
  );
  const [sidebarTab, setSidebarTab] = useState<"sessions" | "workers">(
    "sessions",
  );
  // DOM slot above the sidebar tabs where SessionList portals the pinned
  // selected-session anchor. Lives above the tabs and only has content
  // while SessionList is mounted (i.e. not on the Workers tab).
  const [selectedAnchorEl, setSelectedAnchorEl] = useState<HTMLDivElement | null>(null);
  const SIDEBAR_MINIMIZED_WIDTH = 52;
  const sidebarWidthForSizing = !isMobile && sidebarMinimized
    ? SIDEBAR_MINIMIZED_WIDTH
    : sidebar.size;
  const rightPanel = useResizable({
    storageKey: "better-agent-right-panel-width",
    defaultSize: 450,
    min: 280,
    max: Math.max(280, viewport.width - sidebarWidthForSizing - 360),
    axis: "x",
    direction: "reverse",
    enabled: !isMobile,
  });
  const mobileRightPanel = useResizable({
    storageKey: "better-agent-mobile-right-panel-height",
    defaultSize: Math.round(viewport.height * 0.5),
    min: 160,
    max: Math.max(160, viewport.height - 260),
    axis: "y",
    enabled: isMobile && isPortrait && mobileRightOpen && !mobileRightFullscreen,
  });

  useEffect(() => {
    localStorage.setItem("better-agent-selected-project", selectedProjectPath);
  }, [selectedProjectPath]);
  useEffect(() => {
    localStorage.setItem("better-agent-selected-project-node", selectedProjectNodeId);
  }, [selectedProjectNodeId]);

  // Persist the last-viewed session per project so re-entering a project
  // reopens it (handleSelectProject reads this on switch). Guarded so a
  // session from another project — or a non-listable singleton — is never
  // recorded under the current project during the switch gap.
  useEffect(() => {
    if (!currentSession || !selectedProjectPath) return;
    if (
      currentSession.id === ASK_SINGLETON_ID ||
      currentSession.id === editSingletonId()
    ) {
      return;
    }
    if (currentSession.cwd !== selectedProjectPath) return;
    if ((currentSession.node_id || "primary") !== selectedProjectNodeId) return;
    if (currentSession.archived) return;
    setRememberedSessionId(
      selectedProjectPath,
      selectedProjectNodeId,
      currentSession.id,
    );
  }, [
    currentSession?.id,
    currentSession?.cwd,
    currentSession?.node_id,
    currentSession?.archived,
    selectedProjectPath,
    selectedProjectNodeId,
  ]);

  const [openSessionRecords, setOpenSessionRecords] = useState<Record<string, Session>>({});
  const [missingOpenSessionIds, setMissingOpenSessionIds] = useState<Record<string, true>>({});

  // -------------------------------------------------------------------
  // Route ↔ session sync
  //
  // - Route is `machines`: keep `currentSession` cleared so re-entering
  //   a session view doesn't show a stale tree on first paint, and so
  //   WS subscription state doesn't pin to an unviewed session.
  // - Route is `session:<id>`: pre-check the id against the
  //   already-loaded sessions list. Unknown id → navigate back to `/`
  //   (the Ask entry view). Known id but not the active tree →
  //   `selectSession(id)`.
  // - selectSession is internally de-duped via selectRequestIdRef so
  //   guarding here only protects against the redundant REST round-
  //   trip; correctness is unaffected.
  // -------------------------------------------------------------------
  useEffect(() => {
    if (route.kind !== "session") {
      if (currentTree) clearCurrentSession();
      return;
    }
    if (!sessionsLoaded) return;
    // The Ask singleton is intentionally hidden from `/api/sessions`
    // (its `working_mode` excludes it from the list), so it never
    // appears in `sessions`. Exempt it from the existence gate — the
    // session-view auto-detects the id and mounts Ask extension slots.
    // `sessions` is the SEARCH-FILTERED list — a row absent from it may
    // simply not match the active query, not be deleted. The currently
    // loaded tree is authoritative proof the session exists; a genuine
    // delete nulls `currentTree` via the `session_deleted` WS handler.
    // Without this guard, typing a search that excludes the open session
    // ejects to `/`, and the Ask auto-select effect jumps into the top
    // search result.
    const exists =
      route.sessionId === ASK_SINGLETON_ID ||
      route.sessionId === editSingletonId() ||
      route.sessionId === currentTree?.id ||
      sessions.some((s) => s.id === route.sessionId) ||
      openSessionRecords[route.sessionId];
    if (!exists) {
      navigate("/");
      return;
    }
    if (route.sessionId !== currentTree?.id) {
      selectSession(route.sessionId);
    }
  }, [
    route,
    sessionsLoaded,
    sessions,
    openSessionRecords,
    currentTree,
    clearCurrentSession,
    navigate,
    selectSession,
  ]);

  // Auto-select a session instead of sitting on the empty Ask "home".
  // When the route resolves to the Ask singleton (the default no-session
  // state) and the current project has sessions, redirect to the
  // remembered session (or the first non-archived one). `handleAsk` sets
  // `intentionalAskRef` so a deliberate Ask navigation is preserved; the
  // flag is held until the route leaves Ask, then cleared so a later
  // default landing on Ask auto-redirects again.
  const intentionalAskRef = useRef(false);
  useEffect(() => {
    if (!sessionsLoaded) return;
    if (route.kind !== "session" || route.sessionId !== ASK_SINGLETON_ID) {
      intentionalAskRef.current = false;
      return;
    }
    if (intentionalAskRef.current) return;
    const remembered = selectedProjectPath
      ? getRememberedSessionId(selectedProjectPath, selectedProjectNodeId)
      : null;
    let target = selectedProjectPath
      ? pickSessionForProject(
          sessions,
          selectedProjectPath,
          selectedProjectNodeId,
          remembered,
        )
      : null;
    if (!target) target = sessions.find((s) => !s.archived) ?? null;
    if (target) navigate(sessionPath(target.id));
  }, [
    route,
    sessionsLoaded,
    sessions,
    selectedProjectPath,
    selectedProjectNodeId,
    navigate,
  ]);

  // Force-open-on-navigate: every transition into a session with
  // existing comments OR notes pushes `right_panel_open=true`. This
  // overrides the per-session persisted close from a prior visit
  // (user-confirmed: (b) override-on-navigate). Mobile uses the
  // local-transient drawer flag instead. Singletons (Ask, Edit)
  // are excluded — their UI doesn't use the right-panel drawer, so
  // tapping the Ask CTA should NOT pop the drawer open on mobile.
  const lastNavigatedSidRef = useRef<string | null>(null);
  useEffect(() => {
    if (!currentSession) {
      lastNavigatedSidRef.current = null;
      return;
    }
    if (lastNavigatedSidRef.current === currentSession.id) return;
    lastNavigatedSidRef.current = currentSession.id;
    if (
      currentSession.id === ASK_SINGLETON_ID ||
      currentSession.id === editSingletonId()
    ) {
      return;
    }
    const hasComments = (currentSession.inline_tags?.length ?? 0) > 0;
    const hasNotes = (currentSession.notes?.length ?? 0) > 0;
    if (!hasComments && !hasNotes) return;
    if (isMobile) {
      setMobileRightOpen(true);
      return;
    }
    if (localRightPanelStates[currentSession.id]?.open === true) return;
    patchRightPanel(currentSession.id, { open: true, addAutoReason: "navigate" });
  }, [currentSession?.id, isMobile, patchRightPanel, localRightPanelStates]); // eslint-disable-line react-hooks/exhaustive-deps

  // Sync local `rightPanelTab` to the session's persisted active tab
  // on every session switch. When local storage tab is null
  // (default-on-read for sessions that never had an explicit pick),
  // fall back to the first tab with content in this priority:
  // files > notes > comments > "files".
  const lastTabSyncedSidRef = useRef<string | null>(null);
  useEffect(() => {
    if (!currentSession) return;
    if (lastTabSyncedSidRef.current === currentSession.id) return;
    lastTabSyncedSidRef.current = currentSession.id;
    const persisted = localRightPanelStates[currentSession.id]?.tab;
    if (
      persisted &&
      (persisted !== "canvas" || builtinExtensions.canvas) &&
      (persisted !== "screen" || builtinExtensions.testape)
    ) {
      setRightPanelTab(persisted);
      return;
    }
    if ((currentSession.open_file_panels?.length ?? 0) > 0) {
      setRightPanelTab("files");
    } else if (
      (currentSession.current_todos?.length ?? 0) > 0 ||
      (currentSession.current_tasks?.length ?? 0) > 0
    ) {
      setRightPanelTab("todos");
    } else if ((currentSession.notes?.length ?? 0) > 0) {
      setRightPanelTab("notes");
    } else if ((currentSession.inline_tags?.length ?? 0) > 0) {
      setRightPanelTab("comments");
    } else {
      setRightPanelTab("files");
    }
  }, [currentSession?.id, localRightPanelStates, builtinExtensions.canvas]); // eslint-disable-line react-hooks/exhaustive-deps

  const [openSessionIds, setOpenSessionIds] = useState<string[]>(() => {
    try {
      const saved = localStorage.getItem("better-agent-open-session-ids");
      return saved ? JSON.parse(saved) : [];
    } catch {
      return [];
    }
  });

  useEffect(() => {
    localStorage.setItem(
      "better-agent-open-session-ids",
      JSON.stringify(openSessionIds)
    );
  }, [openSessionIds]);

  useEffect(() => {
    if (!sessionsLoaded) return;
    const loadedIds = new Set(sessions.map((session) => session.id));
    const idsToFetch = openSessionIds.filter(
      (id) => !loadedIds.has(id) && !openSessionRecords[id] && !missingOpenSessionIds[id],
    );
    if (idsToFetch.length === 0) return;

    let cancelled = false;
    for (const id of idsToFetch) {
      fetch(`${API}/api/sessions/${encodeURIComponent(id)}`, {
        credentials: "include",
      })
        .then((res) => {
          if (res.status === 404) {
            setMissingOpenSessionIds((prev) => ({ ...prev, [id]: true }));
            return null;
          }
          if (!res.ok) return null;
          return res.json();
        })
        .then((session: Session | null) => {
          if (cancelled || !session?.id) return;
          setOpenSessionRecords((prev) => ({ ...prev, [session.id]: session }));
        })
        .catch(() => {});
    }

    return () => {
      cancelled = true;
    };
  }, [
    missingOpenSessionIds,
    openSessionIds,
    openSessionRecords,
    sessions,
    sessionsLoaded,
  ]);

  // When a session is selected, add it to the open tabs (LRU order).
  // Evict the least-recently-used tab when exceeding the available space.
  const maxOpenTabs = useMemo(() => {
    const rightPanelOpenForSizing =
      !promptEngState &&
      !fileEditingState &&
      (isMobile
        ? mobileRightOpen
        : (localRightPanelStates[currentSession?.id ?? ""]?.open ?? false) && !!currentSession);
    const occupied = isMobile ? 0 : sidebar.size + (rightPanelOpenForSizing ? rightPanel.size : 0);
    const panelWidth = Math.max(200, viewport.width - occupied);
    return Math.min(MAX_TAB_CAP, Math.max(1, Math.floor(panelWidth / MIN_TAB_WIDTH_PX)));
  }, [
    currentSession,
    fileEditingState,
    isMobile,
    localRightPanelStates,
    mobileRightOpen,
    promptEngState,
    rightPanel.size,
    sidebar.size,
    viewport.width,
  ]);

  useEffect(() => {
    if (currentTree?.id) {
      setOpenSessionIds((prev) => {
        const id = currentTree.id;
        const idx = prev.indexOf(id);
        if (idx >= 0 && idx === prev.length - 1) return prev; // already most recent
        let next: string[];
        if (idx >= 0) {
          next = [...prev.slice(0, idx), ...prev.slice(idx + 1), id];
        } else {
          next = [...prev, id];
        }
        while (next.length > maxOpenTabs) next.shift();
        return next;
      });
    }
  }, [currentTree?.id, maxOpenTabs]);

  // Re-evict tabs when viewport shrinks (sidebar drag, window resize, right panel toggle).
  useEffect(() => {
    setOpenSessionIds((prev) => {
      if (prev.length <= maxOpenTabs) return prev;
      return prev.slice(prev.length - maxOpenTabs);
    });
  }, [maxOpenTabs]);

  useEffect(() => {
    if (sessionsLoaded) {
      setOpenSessionIds((prev) => {
        const valid = prev.filter((id) => !missingOpenSessionIds[id]);
        if (valid.length !== prev.length) return valid;
        return prev;
      });
    }
  }, [missingOpenSessionIds, sessionsLoaded]);

  const handleCloseTab = useCallback(
    (id: string) => {
      setOpenSessionIds((prev) => {
        const next = prev.filter((tid) => tid !== id);
        // If we closed the active session, navigate to the next available tab
        // or the Ask entry view (/) if no tabs remain.
        if (id === currentTree?.id) {
          if (next.length > 0) {
            navigate(sessionPath(next[next.length - 1]));
          } else {
            navigate("/");
          }
        }
        return next;
      });
    },
    [currentTree?.id, navigate]
  );

  const findOpenSessionRecord = useCallback(
    (id: string) => sessions.find((s) => s.id === id) || openSessionRecords[id],
    [openSessionRecords, sessions],
  );

  // Open-session tabs, ordered by the `sessions_tabs_sort` pref (descending
  // on the chosen timestamp). Open-order (newest-opened first) is the stable
  // tie-break for sessions sharing/lacking a timestamp.
  const sortedOpenSessions = useMemo(() => {
    void tabsStatusTick; // recompute when live status changes (status sort on)
    const openOrder = openSessionIds.slice().reverse();
    const records = openOrder
      .map((id) => findOpenSessionRecord(id))
      .filter((s): s is Session => !!s);
    const tsOf = (s: Session) => {
      const v = (s as unknown as Record<string, unknown>)[sessionTabsSort];
      const ms = typeof v === "string" && v ? Date.parse(v) : NaN;
      return Number.isNaN(ms) ? -Infinity : ms;
    };
    return records
      .map((s, i) => ({ s, i }))
      .sort((a, b) => {
        // Tabs are fully loaded → rank straight off the live registry. Status
        // is the strongest key; timestamp the tie-break; open-order last.
        if (sessionTabsStatusSort) {
          const r = statusRankForRow(b.s) - statusRankForRow(a.s);
          if (r !== 0) return r;
        }
        const d = tsOf(b.s) - tsOf(a.s);
        return d !== 0 ? d : a.i - b.i; // stable: keep open-order on ties
      })
      .map((e) => e.s);
  }, [
    openSessionIds,
    findOpenSessionRecord,
    sessionTabsSort,
    sessionTabsStatusSort,
    tabsStatusTick,
  ]);

  const navigateToCreatedSession = useCallback(
    (session: Session) => {
      setOpenSessionRecords((prev) => ({ ...prev, [session.id]: session }));
      navigate(sessionPath(session.id));
    },
    [navigate],
  );

  const handleSelectTab = useCallback(
    (id: string) => {
      const session = findOpenSessionRecord(id);
      if (session) {
        setSelectedProjectPath(session.cwd);
        setSelectedProjectNodeId(session.node_id || "primary");
      }
      navigate(sessionPath(id));
    },
    [findOpenSessionRecord, navigate],
  );

  // Sync user-editable state (model, cwd) from the session record only
  // when the user switches to a different session — NOT on every refetch
  // of the same session. Otherwise the post-turn refetch clobbers any
  // change the user made to the selectors while a turn was running.
  // Informational state (token usage) refreshes unconditionally.
  const lastSyncedSessionIdRef = useRef<string | null>(null);
  // When a session switch syncs the model via setModel, the drift detector
  // below sees the *stale* model on the same render (React batches the
  // state update). This flag tells it to skip one cycle.
  const skipDriftRef = useRef(false);
  useEffect(() => {
    if (!currentSession) {
      lastSyncedSessionIdRef.current = null;
      return;
    }
    setSessionTokenUsage(currentSession.token_usage_total || null);
    setSessionTokenUsageLast(currentSession.token_usage_last || null);
    if (currentSession.id !== lastSyncedSessionIdRef.current) {
      lastSyncedSessionIdRef.current = currentSession.id;
      // Re-establish the global selector from the focused session
      // UNCONDITIONALLY on every switch, and always arm the skip. Arming
      // lastSynced without overwriting `model` left a leaked active-provider
      // default (e.g. glm-5.2 after switching the default provider) sitting in
      // `model`, which the drift detector then PATCHed onto a different-provider
      // session — corrupting its model while provider_id stayed put.
      setModel(currentSession.model || "");
      skipDriftRef.current = true;
      if (currentSession.cwd) setCwd(currentSession.cwd);
    }
  }, [currentSession]);

  // Persist `model` changes to the current session record. cwd is NOT
  // patched (immutable after creation) and orchestration_mode is NOT
  // patched (frozen at creation; the selector is a global preference for
  // *new* sessions). Skip the very first sync after a session switch so
  // we don't echo values we just READ from the session back as writes.
  useEffect(() => {
    if (!currentSession) return;
    if (skipDriftRef.current) {
      skipDriftRef.current = false;
      return;
    }
    if (currentSession.id !== lastSyncedSessionIdRef.current) return;
    // Never persist a model that leaked from the active/default-provider
    // mirror onto a session whose own provider differs — that write would
    // corrupt the session's model (and now 400s at the backend, spamming).
    if (isLeakedProviderMirror(model, currentProvider, defaultProvider)) return;
    // Gate on `model` being non-empty. Until the active provider's
    // default_model is pulled from /api/providers, local `model` is "" —
    // comparing against the session's stored model would always look
    // like drift and fire a spurious PATCH that echoes empty back.
    const drift =
      model && currentSession.model && currentSession.model !== model;
    if (!drift) return;
    progressTrackedFetch(
      `selectors:save:${currentSession.id}`,
      `${API}/api/sessions/${currentSession.id}/selectors`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        // `client_id` plumbs through to the WS `session_metadata_updated`
        // frame's `originated_by` so this tab skips its own echo and
        // doesn't fight the in-flight optimistic selector value (DIV-4).
        body: JSON.stringify({ model, client_id: clientId }),
      },
      { silent: true },
    ).then(() => refreshSessions()).catch(() => {});
  }, [model, currentSession, refreshSessions, clientId, defaultProvider, currentProvider]);

  // user_message_persisted ack is now handled imperatively by
  // `handleUserMessagePersisted` (passed to useWebSocket above) —
  // dispatched directly from `onmessage` so it can't be lost to a
  // subsequent `setEvents([])` in the same React commit cycle.

  // Session auto-rename on first prompt is wired through the WS
  // handler (`onSessionRenamed` option above); no buffer-scan effect.

  // Backstop pending-clear: treat any persisted user message whose
  // client_id appears in our optimistic pending list as an ack. The
  // primary clear path is the `user_message_persisted` event handler
  // above; this effect catches the case where that event was lost in
  // transit (briefly-dead WS, missed frame) but the message arrived
  // via `messages_replay`/`messages_delta`. Backend persistence is
  // the canonical signal; replay is just another delivery channel
  // for the same fact.
  useEffect(() => {
    if (!currentSession) return;
    const sessionId = currentSession.id;
    const ackedClientIds = new Set<string>();
    for (const m of currentSession.messages || []) {
      if (m.role === "user" && m.client_id) {
        ackedClientIds.add(m.client_id);
      }
    }
    if (ackedClientIds.size === 0) return;
    for (const cid of ackedClientIds) {
      ackedClientIdsRef.current.add(cid);
      offlineDispatchedRef.current.delete(cid);
      removeAckedOfflineAction(sessionId, cid);
    }
    setPendingBySession((all) => {
      const prev = all[sessionId] ?? [];
      const next = prev.filter((m) => !ackedClientIds.has(m.id));
      if (next.length === prev.length) return all;
      if (next.length === 0) {
        const { [sessionId]: _drop, ...rest } = all;
        void _drop;
        return rest;
      }
      return { ...all, [sessionId]: next };
    });
  }, [currentSession, removeAckedOfflineAction]);

  // When a turn ends, refresh the sidebar (timestamps + token totals)
  // and surface any final error onto the in-flight pending entry if
  // it somehow survived the user_message_persisted ack. Pending
  // success-removal is owned by the client_id matcher in the
  // user_message_persisted handler — replay / messages_delta keep
  // the canonical message list converged on their own.
  useEffect(() => {
    if (!isStreaming && lastResult && streamingAppSessionId) {
      if (lastResult.success === false) {
        const errorText =
          typeof lastResult.error === "string"
            ? lastResult.error
            : t("app.somethingWentWrong");
        const failedClientId =
          typeof lastResult.client_id === "string" ? lastResult.client_id : null;
        setPendingForSession(streamingAppSessionId, (prev) => {
          if (prev.length === 0) return prev;
          if (failedClientId) {
            return prev.map((m) =>
              m.id === failedClientId
                ? { ...m, status: "error" as const, errorText }
                : m
            );
          }
          return [
            { ...prev[0], status: "error" as const, errorText },
            ...prev.slice(1),
          ];
        });
      }
      refreshSessions();
    }
  }, [isStreaming, lastResult, streamingAppSessionId, setPendingForSession, refreshSessions]);

  // (re)connect hydration is now handled by the WS replay protocol:
  // every subscribe sends `since_seq` and the backend responds with
  // `messages_replay` carrying everything we missed (including the
  // live in-flight assistant message). No REST refetch needed here.

  const sendPrompt = useCallback(
    async (
      prompt: string,
      images: import("./components/InputArea").PastedImage[],
      files: import("./components/InputArea").FileAttachment[],
      sendMode: SendMode,
    ): Promise<boolean> => {
      if (!currentSession) return false;

      // Pre-send project check: on a FRESH session (no turns yet),
      // ask the backend whether this prompt looks like it belongs to a
      // different project. If so, offer to move the session before the
      // first turn spawns a CLI in the wrong cwd. Advisory only — any
      // failure falls through to a normal send.
      let effectiveCwd = cwd || currentSession.cwd;
      const isFreshSession = (currentSession.messages?.length ?? 0) === 0;
      if (isFreshSession && !currentSession.offline_pending) {
        try {
          // Advisory check — never let it stall the send. Cap at 1.5s.
          const ctrl = new AbortController();
          const timer = setTimeout(() => ctrl.abort(), 1500);
          const res = await progressTrackedFetch(
            `project:suggest:${currentSession.id}`,
            `${API}/api/sessions/${currentSession.id}/project-suggestion`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ prompt }),
              signal: ctrl.signal,
            },
          ).finally(() => clearTimeout(timer));
          const sugg: ProjectSuggestion | null = res.ok
            ? (await res.json()).suggestion
            : null;
          if (sugg && sugg.target_cwd !== effectiveCwd) {
            const decision = await new Promise<"move" | "here" | "cancel">(
              (resolve) => setProjectSuggestion({ suggestion: sugg, resolve }),
            );
            setProjectSuggestion(null);
            if (decision === "cancel") return false;
            if (decision === "move") {
              await progressTrackedFetch(
                `selectors:cwd:${currentSession.id}`,
                `${API}/api/sessions/${currentSession.id}/selectors`,
                {
                  method: "PATCH",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    cwd: sugg.target_cwd,
                    client_id: clientId,
                  }),
                },
              );
              effectiveCwd = sugg.target_cwd;
              applySessionMetadata(currentSession.id, { cwd: sugg.target_cwd });
              setCwd(sugg.target_cwd);
              setSelectedProjectPath(sugg.target_cwd);
              setSelectedProjectNodeId(
                projects.find((p) => p.path === sugg.target_cwd)?.node_id ||
                  currentSession.node_id ||
                  selectedProjectNodeId,
              );
              skipSidebarCloseOnNavRef.current = true;
              navigate(sessionPath(currentSession.id));
            }
          }
        } catch {
          // Advisory check failed — proceed with the normal send.
        }
      }

      // Effective attachments — when a queued prompt is merged into, these
      // grow to include the previously-queued prompt's attachments so the
      // merge re-dispatch doesn't drop them (see queue branch below).
      let effImages = images;
      let effFiles = files;
      const toImagePayload = (img: PastedImage): ImagePayload => ({
        data: img.base64,
        media_type: img.mediaType,
      });
      const toFilePayload = (f: FileAttachment): FilePayload => ({
        name: f.name,
        data: f.base64,
        media_type: f.mediaType,
        size: f.size,
      });
      let imagePayloads: ImagePayload[] = effImages.map(toImagePayload);
      let filePayloads: FilePayload[] = effFiles.map(toFilePayload);

      const sessionTags = currentSession.inline_tags ?? [];
      const withTags = mergeTagsIntoPrompt(prompt, sessionTags);

      const openFileSnapshots = rightPanelVisible
        ? (currentSession.open_file_panels ?? []).map((p) => {
            const h = openFileEditorsRef.current.get(p.path);
            return {
              path: p.path,
              visible: h?.getVisibleRange() ?? null,
              caret: h?.getCaretPosition() ?? null,
              selection: h?.getSelection() ?? null,
            };
          })
        : [];
      const openFilesPreamble = buildOpenFilesPreamble(openFileSnapshots);
      const finalPrompt = openFilesPreamble
        ? `${openFilesPreamble}\n${withTags}`
        : withTags;

      let sendForm = buildSendPromptForm({ finalPrompt, sendMode });
      if (sendMode === "queue") {
        // Queue merging: backend decides queued vs immediate via
        // `coordinator.has_active_turn(app_session_id)`. The frontend
        // does NOT guess — it always sends with `send_mode=queue` and
        // lets the backend route. For merge UX, read the canonical
        // text from either source: `queuedBySession` (backend-confirmed
        // queue entry) or `pendingQueueTextBySession` (the in-flight
        // textbox-merge slot for a send that hasn't been acked yet).
        const existingQueued = queuedBySession[currentSession.id];
        const existingPendingText =
          pendingQueueTextBySession[currentSession.id];
        sendForm = buildSendPromptForm({
          finalPrompt,
          sendMode,
          existingQueuedPreview: existingQueued?.preview,
          existingPendingText,
        });
        if (sendForm.replacedQueuedPrompt) {
          // The merge cancels the previously-queued backend entry and
          // re-dispatches a single merged prompt. Carry the previous
          // prompt's attachments forward (mirroring the text merge) so the
          // re-dispatch doesn't drop them. Source priority matches the text
          // merge: backend-confirmed queue entry first, else the not-yet-
          // acked stash slot. Both hold PastedImage/FileAttachment shapes.
          const prevImages =
            (existingQueued?.images as PastedImage[] | undefined) ??
            pendingQueueImagesRef.current[currentSession.id];
          const prevFiles =
            (existingQueued?.files as FileAttachment[] | undefined) ??
            pendingQueueFilesRef.current[currentSession.id];
          const merged = mergeQueuedAttachments(
            prevImages,
            prevFiles,
            images,
            files,
          );
          effImages = merged.images;
          effFiles = merged.files;
          imagePayloads = effImages.map(toImagePayload);
          filePayloads = effFiles.map(toFilePayload);
          // Drop the backend-confirmed entry (if any) so we re-submit a
          // single merged prompt. If only the textbox-merge slot is
          // populated (no backend ack yet), there's nothing to cancel on
          // the backend — the slot is cleared synchronously below.
          if (existingQueued) {
            sendCancelQueued(currentSession.id);
          }
          // Remove any pending bubbles for the previous prompt — the merged
          // bubble replaces them. Without this, the old bubble leaks when
          // prompt_queued arrives for the OLD client_id but the merged prompt
          // carries a NEW client_id.
          setPendingBySession((all) => {
            const prev = all[currentSession.id];
            if (!prev || prev.length === 0) return all;
            const { [currentSession.id]: _drop, ...rest } = all;
            void _drop;
            return rest;
          });
        }

        // Synchronously seed the textbox-merge slot with the full text so
        // a fast double-send can merge against it before the backend ack
        // arrives. `writePendingQueueText` updates BOTH the ref and the
        // state in a single tick — closes the race where `prompt_queued`
        // arrives before React commits and `onPromptQueued` would read
        // a stale ref. Cleared on `prompt_queued` (text moves into
        // `queuedBySession.preview`) or `user_message_persisted`.
        writePendingQueueText(currentSession.id, sendForm.prompt);
        // Stash attachments for the banner alongside the merge-slot text.
        pendingQueueImagesRef.current = {
          ...pendingQueueImagesRef.current,
          [currentSession.id]: effImages.length > 0 ? effImages : [],
        };
        pendingQueueFilesRef.current = {
          ...pendingQueueFilesRef.current,
          [currentSession.id]: effFiles.length > 0 ? effFiles : [],
        };
      }

      // client_id so the backend can echo it back when the queued message
      // is eventually processed (or immediately for non-queued sends).
      const clientIdForMsg = `pending-${Date.now()}`;

      const sessionId = currentSession.id;
      const capabilityContexts = turnCapabilityContextsBySession[sessionId] ?? [];
      logPromptSend("app_send_prepare", {
        app_session_id: sessionId,
        client_id: clientIdForMsg,
        send_mode: sendMode,
        send_target: currentSession?.supervisor_enabled ? sendTarget : null,
        orchestration_mode: currentSession?.orchestration_mode ?? null,
        connected,
        offline_pending: Boolean(currentSession.offline_pending),
        prompt_length: sendForm.prompt.length,
        image_count: imagePayloads.length,
        file_count: filePayloads.length,
        capability_context_count: capabilityContexts.length,
        replaced_queued_prompt: sendForm.replacedQueuedPrompt,
      });
      // Always add an optimistic user bubble. Backend will either
      // echo `user_message_persisted` (immediate dispatch) which
      // clears the bubble in favor of the real msg, or emit
      // `prompt_queued` (queued behind another turn) — in the
      // queued case the bubble lingers as "sending" until the
      // queue dispatches and the ack arrives.
      const pendingMsg: ChatMessage = {
        id: clientIdForMsg,
        role: "user",
        content: sendForm.prompt,
        events: [],
        timestamp: new Date().toISOString(),
        isStreaming: false,
        status: "sending",
        ...(effImages.length > 0
          ? {
              images: effImages.map((img) => ({
                media_type: img.mediaType,
                dataUrl: img.dataUrl,
              })),
            }
          : {}),
        ...(filePayloads.length > 0
          ? {
              files: filePayloads.map((file) => ({
                name: file.name,
                media_type: file.media_type,
                size: file.size,
              })),
            }
          : {}),
        ...(currentSession?.supervisor_enabled && sendTarget === "supervisor"
          ? { source: "supervisor" as const }
          : {}),
      };
      appendPendingForSession(sessionId, pendingMsg);

      // Store image payloads for potential retry
      if (imagePayloads.length > 0) {
        retryPayloadsRef.current.set(pendingMsg.id, imagePayloads);
      }

      const offlineEntry = {
        sessionId,
        clientId: clientIdForMsg,
        prompt: sendForm.prompt,
        model,
        cwd: effectiveCwd,
        images: imagePayloads.length > 0 ? imagePayloads : undefined,
        files: filePayloads.length > 0 ? filePayloads : undefined,
        orchestrationMode: currentSession?.orchestration_mode ?? undefined,
        sendMode,
        sendTarget: currentSession?.supervisor_enabled ? sendTarget : undefined,
        capabilityContexts,
      };
      if (sendMode === "queue") {
        offlineQueue.replaceBySession(sessionId, offlineEntry);
      } else {
        offlineQueue.enqueue(offlineEntry);
      }

      if (currentSession.offline_pending) {
        logPromptSend("app_offline_pending_session", {
          app_session_id: sessionId,
          client_id: clientIdForMsg,
          queue_size: offlineQueue.queue.length,
        }, "warn");
        setPendingForSession(sessionId, (prev) =>
          prev.map((m) =>
            m.id === clientIdForMsg ? { ...m, status: "offline" as const } : m
          )
        );
        handleDraftClearImmediate(sessionId);
        if (capabilityContexts.length > 0) {
          setTurnCapabilityContextsBySession((prev) => {
            const { [sessionId]: _drop, ...rest } = prev;
            void _drop;
            return rest;
          });
        }
        return true;
      }

      const sent = sendMessage(
        sendForm.prompt,
        model,
        effectiveCwd,
        null, // claude_session_id no longer needed — orchestrator manages it
        sessionId,
        imagePayloads.length > 0 ? imagePayloads : undefined,
        currentSession?.orchestration_mode ?? undefined,
        clientIdForMsg, // client_id — backend echoes on user_msg
        sendMode,
        currentSession?.supervisor_enabled ? sendTarget : undefined,
        filePayloads.length > 0 ? filePayloads : undefined,
        capabilityContexts,
      );

      // Gap 1: WS not open — keep the durable localStorage action for
      // offline delivery. The optimistic bubble stays visible with
      // status "offline" and is promoted to "sending" on reconnect.
      if (!sent) {
        logPromptSend("app_ws_send_failed_offline", {
          app_session_id: sessionId,
          client_id: clientIdForMsg,
          connected,
          queue_size: offlineQueue.queue.length,
        }, "warn");
        setPendingForSession(sessionId, (prev) =>
          prev.map((m) =>
            m.id === clientIdForMsg ? { ...m, status: "offline" as const } : m
          )
        );
        // Fall through to tag/draft clearing — those are local actions.
      } else {
        logPromptSend("app_ws_send_dispatched", {
          app_session_id: sessionId,
          client_id: clientIdForMsg,
          queue_size: offlineQueue.queue.length,
        });
        offlineDispatchedRef.current.add(clientIdForMsg);
      }

      if (sessionTags.length > 0) {
        clearSessionInlineTags(sessionId);
      }
      // Clear the persisted draft (immediate, not debounced) so other
      // tabs see the textarea empty without waiting on the timer.
      handleDraftClearImmediate(sessionId);
      if (capabilityContexts.length > 0) {
        setTurnCapabilityContextsBySession((prev) => {
          const { [sessionId]: _drop, ...rest } = prev;
          void _drop;
          return rest;
        });
      }

      return true;
    },
    [currentSession, model, cwd, sendMessage, applySessionMetadata, setPendingForSession, appendPendingForSession, handleDraftClearImmediate, clearSessionInlineTags, sendCancelQueued, queuedBySession, pendingQueueTextBySession, writePendingQueueText, offlineQueue, sendTarget, rightPanelVisible, turnCapabilityContextsBySession, projects, selectedProjectNodeId, navigate]
  );

  // One-time bypass-permission warning on the first prompt send. The user
  // either changes it in Settings (don't send) or sends anyway — sending
  // acknowledges so the dialog never reappears. Pure UI ack (no backend state).
  const [bypassPermAck, setBypassPermAck] = useState<boolean>(
    () => localStorage.getItem("ba_bypass_perm_ack") === "1",
  );
  const [bypassPermPending, setBypassPermPending] = useState<{
    prompt: string;
    images: import("./components/InputArea").PastedImage[];
    files: import("./components/InputArea").FileAttachment[];
    // Resolves the Promise handleSend returned to InputArea.submitDraft, so
    // submitDraft stays the single authority that clears the draft/images/
    // files (on confirm) or restores them (on cancel/dismiss).
    resolve: (sent: boolean) => void;
  } | null>(null);

  const handleSend = useCallback(
    (prompt: string, images: import("./components/InputArea").PastedImage[], files: import("./components/InputArea").FileAttachment[]) => {
      if (
        !bypassPermAck &&
        currentSession &&
        currentProvider &&
        sessionIsBypass(currentProvider.kind, currentSession.permission, currentProvider.default_permission)
      ) {
        return new Promise<boolean>((resolve) => {
          setBypassPermPending({ prompt, images, files, resolve });
        });
      }
      return sendPrompt(prompt, images, files, "queue");
    },
    [sendPrompt, bypassPermAck, currentSession, currentProvider],
  );

  const confirmBypassAndSend = useCallback(async () => {
    const pending = bypassPermPending;
    if (!pending) return;
    localStorage.setItem("ba_bypass_perm_ack", "1");
    setBypassPermAck(true);
    setBypassPermPending(null);
    const sent = await sendPrompt(pending.prompt, pending.images, pending.files, "queue");
    pending.resolve(sent === true);
  }, [bypassPermPending, sendPrompt]);

  const dismissBypassPending = useCallback(() => {
    setBypassPermPending((pending) => {
      pending?.resolve(false);
      return null;
    });
  }, []);

  const bypassGoToSettings = useCallback(() => {
    dismissBypassPending();
    navigate("/settings");
  }, [dismissBypassPending, navigate]);

  const handleSteer = useCallback(
    (prompt: string, images: import("./components/InputArea").PastedImage[], files: import("./components/InputArea").FileAttachment[]) =>
      sendPrompt(prompt, images, files, "steer"),
    [sendPrompt],
  );

  const handleInterrupt = useCallback(
    (prompt: string, images: import("./components/InputArea").PastedImage[], files: import("./components/InputArea").FileAttachment[]) =>
      sendPrompt(prompt, images, files, "interrupt"),
    [sendPrompt],
  );

  const handleAlterUserMessage = useCallback(
    (message: ChatMessage, content: string): boolean => {
      void message;
      if (!currentSession) return false;
      const prompt = content.trim();
      if (!prompt) return false;
      const sessionId = currentSession.id;
      const clientIdForMsg = `pending-${Date.now()}`;
      const pendingMsg: ChatMessage = {
        id: clientIdForMsg,
        role: "user",
        content: prompt,
        events: [],
        timestamp: new Date().toISOString(),
        isStreaming: false,
        status: "sending",
      };
      appendPendingForSession(sessionId, pendingMsg);
      const sent = sendMessage(
        prompt,
        model,
        cwd || currentSession.cwd,
        null,
        sessionId,
        undefined,
        currentSession.orchestration_mode ?? undefined,
        clientIdForMsg,
        "alter",
        currentSession.supervisor_enabled ? sendTarget : undefined,
      );
      if (!sent) {
        setPendingForSession(sessionId, (prev) =>
          prev.filter((m) => m.id !== clientIdForMsg)
        );
      }
      return sent;
    },
    [currentSession, model, cwd, sendMessage, setPendingForSession, appendPendingForSession, sendTarget],
  );

  const handleVoiceNewSession = useCallback(async () => {
    const provider = currentProvider ?? defaultProvider;
    const nextModel = currentSession?.model || model || provider?.last_model || provider?.default_model || "";
    const nextProviderId = currentSession?.provider_id ?? provider?.id;
    const nextCwd = selectedProjectPath || currentSession?.cwd || cwd;
    const nextMode: OrchestrationMode =
      currentSession?.orchestration_mode ?? (provider?.supports_manager_mode ? "team" : "native");
    if (!connected || !nextModel || !nextCwd) return;

    const session = await createSession({
      name: "",
      model: nextModel,
      cwd: nextCwd,
      orchestrationMode: nextMode,
      browserHarnessEnabled: currentSession?.browser_harness_enabled ?? true,
      providerId: nextProviderId,
      browserHarnessHeadless: currentSession?.browser_harness_headless ?? true,
      nodeId: currentSession?.node_id ?? "primary",
      reasoningEffort: currentSession?.reasoning_effort || provider?.last_reasoning_effort || provider?.default_reasoning_effort || undefined,
    });
    if (session?.id) {
      navigateToCreatedSession(session);
    }
  }, [defaultProvider, connected, createSession, currentProvider, currentSession, cwd, model, navigateToCreatedSession, selectedProjectPath]);

  useEffect(() => {
    const onVoiceNewSession = () => {
      void handleVoiceNewSession();
    };
    const onVoiceOpenPrompt = () => {
      document.querySelector<HTMLTextAreaElement>('[data-testid="input-textarea"]')?.focus();
    };
    const onVoiceAppendDraft = (event: Event) => {
      if (!currentSession) return;
      const { text } = (event as CustomEvent<VoicePromptEventDetail>).detail;
      const clean = text.trim();
      if (!clean) return;

      const existing = currentSession.draft_input ?? "";
      const next = existing ? `${existing} ${clean}` : clean;
      handleDraftChange(currentSession.id, next);
      document.querySelector<HTMLTextAreaElement>('[data-testid="input-textarea"]')?.focus();
    };
    const onVoiceSendPrompt = (event: Event) => {
      const { text } = (event as CustomEvent<VoicePromptEventDetail>).detail;
      const clean = text.trim();
      if (!clean) return;
      void handleSend(clean, [], []);
    };

    window.addEventListener(VOICE_APPEND_DRAFT_EVENT, onVoiceAppendDraft);
    window.addEventListener(VOICE_NEW_SESSION_EVENT, onVoiceNewSession);
    window.addEventListener(VOICE_OPEN_PROMPT_EVENT, onVoiceOpenPrompt);
    window.addEventListener(VOICE_SEND_PROMPT_EVENT, onVoiceSendPrompt);
    return () => {
      window.removeEventListener(VOICE_APPEND_DRAFT_EVENT, onVoiceAppendDraft);
      window.removeEventListener(VOICE_NEW_SESSION_EVENT, onVoiceNewSession);
      window.removeEventListener(VOICE_OPEN_PROMPT_EVENT, onVoiceOpenPrompt);
      window.removeEventListener(VOICE_SEND_PROMPT_EVENT, onVoiceSendPrompt);
    };
  }, [currentSession, handleDraftChange, handleSend, handleVoiceNewSession]);

  const handleRetry = useCallback(
    (message: ChatMessage) => {
      if (!currentSession) return;
      const sessionId = currentSession.id;
      const images = retryPayloadsRef.current.get(message.id) ?? [];

      // Replace failed message with a fresh "sending" one
      const newPendingMsg: ChatMessage = {
        ...message,
        id: `pending-${Date.now()}`,
        status: "sending",
        errorText: undefined,
      };

      if (images.length > 0) {
        retryPayloadsRef.current.delete(message.id);
        retryPayloadsRef.current.set(newPendingMsg.id, images);
      }

      setPendingForSession(sessionId, (prev) =>
        prev.map((m) => (m.id === message.id ? newPendingMsg : m))
      );

      const sent = sendMessage(
        message.content,
        model,
        cwd || currentSession.cwd,
        null,
        sessionId,
        images.length > 0 ? images : undefined,
        currentSession?.orchestration_mode ?? undefined,
        newPendingMsg.id // client_id — backend echoes on user_msg
      );

      if (!sent) {
        setPendingForSession(sessionId, (prev) =>
          prev.map((m) =>
            m.id === newPendingMsg.id
              ? { ...m, status: "error" as const, errorText: t("app.notConnectedError") }
              : m
          )
        );
      }
    },
    [currentSession, model, cwd, sendMessage, setPendingForSession]
  );

  const handleStop = useCallback(() => {
    if (!currentSession) return;
    // Stop only cancels the active turn. Any queued prompt stays
    // queued — the user explicitly opted to keep it; cancelling the
    // queue is a separate action via the queue banner's own controls.
    stopStreaming(currentSession.id);
  }, [currentSession, stopStreaming]);

  const handlePromoteQueued = useCallback((action: "interrupt" | "steer" = "interrupt") => {
    if (!currentSession) return;
    const sent = sendPromoteQueued(currentSession.id, action);
    if (!sent) return;
    if (action === "steer") return;
    setQueuedForSession(currentSession.id, null);
    // Drop the textbox-merge slot too — once promoted, there's no
    // pre-ack text to merge against on a future send.
    writePendingQueueText(currentSession.id, null);
    delete pendingQueueImagesRef.current[currentSession.id];
    delete pendingQueueFilesRef.current[currentSession.id];
  }, [currentSession, sendPromoteQueued, setQueuedForSession, writePendingQueueText]);

  const handleCancelQueued = useCallback(() => {
    if (!currentSession) return;
    const sent = sendCancelQueued(currentSession.id);
    if (!sent) return;
    setQueuedForSession(currentSession.id, null);
    writePendingQueueText(currentSession.id, null);
    delete pendingQueueImagesRef.current[currentSession.id];
    delete pendingQueueFilesRef.current[currentSession.id];
  }, [currentSession, sendCancelQueued, setQueuedForSession, writePendingQueueText]);

  const handleQueuedTextEdit = useCallback(
    (text: string) => {
      if (!currentSession) return;
      const existing = queuedBySession[currentSession.id];
      if (!existing) return;
      const sent = sendUpdateQueued(currentSession.id, existing.id, text);
      if (!sent) return;
      setQueuedForSession(currentSession.id, {
        id: existing.id,
        preview: text,
      });
    },
    [currentSession, queuedBySession, setQueuedForSession, sendUpdateQueued]
  );

  /** Rewind past a stopped/failed assistant turn and immediately re-send
   * the prior user prompt as a fresh turn. The backend rewinds the
   * session to before the failed user message (removing the failed
   * user+assistant pair and broadcasting rewind_complete) and returns the
   * prompt to retry; we then re-send via the existing WS send path so the
   * retry yields one user message, not a duplicate. */
  const handleRetryStopped = useCallback(
    async (assistantMessage: ChatMessage) => {
      if (!currentSession) return;
      const sessionId = currentSession.id;
      try {
        const res = await progressTrackPromise(
          `session:rewindAndRetry:${sessionId}`,
          () =>
            fetch(`${API}/api/sessions/${sessionId}/rewind_and_retry`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ assistant_message_id: assistantMessage.id }),
            }),
        ).promise;
        if (!res.ok) {
          let detail = await res.text();
          try {
            detail = JSON.parse(detail).detail ?? detail;
          } catch {
            // body wasn't JSON; keep raw text.
          }
          console.error("rewind_and_retry failed:", res.status, detail);
          // Surface a user-visible error rather than silently failing.
          // Retry is a foreground action — the user clicked it and
          // expects to know why nothing happened.
          alert(t("app.retryFailedStatus", { status: res.status }) + detail);
          return;
        }
        const data = (await res.json()) as {
          retry_prompt?: string;
          retry_model?: string;
          retry_cwd?: string;
          retry_orchestration_mode?: import("./types").OrchestrationMode;
        };
        const prompt = data.retry_prompt ?? "";
        if (!prompt) return;

        const pendingMsg: ChatMessage = {
          id: `pending-${Date.now()}`,
          role: "user",
          content: prompt,
          events: [],
          timestamp: new Date().toISOString(),
          isStreaming: false,
          status: "sending",
        };
        appendPendingForSession(sessionId, pendingMsg);

        const sent = sendMessage(
          prompt,
          data.retry_model ?? model,
          data.retry_cwd ?? cwd ?? currentSession.cwd,
          null,
          sessionId,
          undefined,
          data.retry_orchestration_mode ?? currentSession?.orchestration_mode ?? undefined,
          pendingMsg.id
        );
        if (!sent) {
          setPendingForSession(sessionId, (prev) =>
            prev.filter((m) => m.id !== pendingMsg.id)
          );
        }
      } catch (e) {
        console.error("rewind_and_retry error:", e);
        alert(t("app.retryFailedError") + (e instanceof Error ? e.message : String(e)));
      }
    },
    [currentSession, model, cwd, sendMessage, setPendingForSession]
  );

  /** Sidebar ⚙ badge handler. Re-enters the engineering overlay for an
   * existing eng session whose parent is `parentSessionId`. Same critical
   * ordering as the fresh-start flow: swap currentSession FIRST so the
   * non-destructive-exit effect can't see a mismatched promptEngState. */
  const handleResumeEng = useCallback(
    async (parentSessionId: string) => {
      try {
        const r = await progressTrackPromise(
          `session:resumeEng:${parentSessionId}`,
          () => fetch(`${extBackendBase("promptEngineer")}/sessions/${parentSessionId}/prompt-engineer`),
        ).promise;
        if (!r.ok) {
          // Stale badge (eng was cleaned up by a sibling tab). Refresh
          // the sidebar so the badge disappears on next render.
          refreshSessions();
          return;
        }
        const data = (await r.json()) as {
          eng_session_id: string;
        };
        // navigate via the URL so the route-sync effect drives
        // selectSession (and a future refresh on this URL restores
        // the eng overlay). Calling selectSession directly would
        // race the Home-clear path in the route-sync effect.
        navigate(sessionPath(data.eng_session_id));
      } catch {
        refreshSessions();
      }
    },
    [refreshSessions, navigate],
  );

  const queueLocalFirstSession = useCallback(
    (
      config: SessionConfig,
      initialPrompt: string,
      images: ImagePayload[],
      files: FilePayload[],
      pendingStatus: ChatMessage["status"] = "offline",
    ) => {
      if (config.fileEditEnabled) {
        window.alert(t("app.fileEditOfflineQueue", "File-editing sessions cannot be queued offline."));
        return;
      }
      const id = uuidv4();
      const now = new Date().toISOString();
      const clientId = `offline-create-${id}`;
      const localName = initialPrompt
        ? initialPrompt.split("\n")[0].slice(0, 80)
        : "New Session";
      const localSession: Session = {
        id,
        name: localName,
        model: config.main.model,
        reasoning_effort: config.main.reasoningEffort,
        permission: config.main.permission,
        cwd: config.cwd,
        orchestration_mode: config.orchestrationMode,
        provider_id: config.main.providerId,
        browser_harness_enabled: config.browserHarnessEnabled,
        browser_harness_headless: config.browserHarnessHeadless,
        node_id: config.nodeId,
        created_at: now,
        updated_at: now,
        messages: [],
        offline_pending: true,
        capability_contexts: config.capabilityContexts,
        folder_id: config.folderId ?? null,
      };
      addOfflineSession(localSession);
      offlineQueue.enqueue({
        type: "create_session",
        clientId,
        session: localSession,
        prompt: initialPrompt,
        images: images.length ? images : undefined,
        files: files.length ? files : undefined,
        capabilityContexts: config.capabilityContexts,
      });
      if (initialPrompt) {
        setPendingForSession(id, () => [{
          id: clientId,
          role: "user",
          content: initialPrompt,
          events: [],
          timestamp: now,
          isStreaming: false,
          status: pendingStatus,
        }]);
      }
      setNewSessionModalOpen(false);
      setInvestigationCtx(undefined);
      navigate(sessionPath(id));
    },
    [addOfflineSession, offlineQueue, navigate, setPendingForSession],
  );

  const queueInitialPromptForSession = useCallback(
    (
      sessionId: string,
      config: SessionConfig,
      initialPrompt: string,
      images: ImagePayload[],
      files: FilePayload[],
    ) => {
      const clientId = `investigate-${Date.now()}`;
      offlineQueue.enqueue({
        sessionId,
        clientId,
        prompt: initialPrompt,
        model: config.main.model,
        cwd: config.cwd,
        images: images.length > 0 ? images : undefined,
        files: files.length > 0 ? files : undefined,
        orchestrationMode: config.orchestrationMode,
        sendMode: "queue",
        capabilityContexts: config.capabilityContexts,
      });
      setPendingForSession(sessionId, (prev) => [
        ...prev,
        {
          id: clientId,
          role: "user",
          content: initialPrompt,
          events: [],
          timestamp: new Date().toISOString(),
          isStreaming: false,
          status: "offline",
        },
      ]);
    },
    [offlineQueue, setPendingForSession],
  );

  const handleCreateSessionFromModal = useCallback(
    async (config: SessionConfig, investigation?: InvestigationContext) => {
      const initialPrompt = (investigation?.prompt ?? config.initialPrompt).trim();
      const images: ImagePayload[] = (investigation?.images ?? config.initialImages).map((img) => ({
        data: img.base64,
        media_type: img.mediaType,
      }));
      const files: FilePayload[] = (investigation?.files ?? config.initialFiles).map((file) => ({
        name: file.name,
        data: file.base64,
        media_type: file.mediaType,
        size: file.size,
      }));
      if (!config.fileEditEnabled) {
        try {
          const session = await createSession({
            name: "",
            model: config.main.model,
            cwd: config.cwd,
            orchestrationMode: config.orchestrationMode,
            browserHarnessEnabled: config.browserHarnessEnabled,
            providerId: config.main.providerId,
            browserHarnessHeadless: config.browserHarnessHeadless,
            nodeId: config.nodeId,
            reasoningEffort: config.main.reasoningEffort,
            permission: config.main.permission,
            capabilityContexts: config.capabilityContexts,
            folderId: config.folderId,
          });
          setNewSessionModalOpen(false);
          setInvestigationCtx(undefined);
          if (session?.id) {
            navigateToCreatedSession(session);
            if (initialPrompt) {
              const pending = {
                sessionId: session.id,
                prompt: initialPrompt,
                images,
                files,
                model: config.main.model,
                cwd: config.cwd,
                orchestrationMode: config.orchestrationMode,
                capabilityContexts: config.capabilityContexts,
              };
              if (!sendInitialPromptToSession(pending)) {
                queueInitialPromptForSession(session.id, config, initialPrompt, images, files);
              }
            }
          }
        } catch (e) {
          if (isRetryableOfflineError(e)) {
            queueLocalFirstSession(
              config,
              initialPrompt,
              images,
              files,
              connected ? "sending" : "offline",
            );
            return;
          }
          const msg = e instanceof Error ? e.message : String(e);
          window.alert(msg);
        }
        return;
      }
      if (!connected) {
        queueLocalFirstSession(config, initialPrompt, images, files);
        return;
      }
      try {

        const session = await createSession({
          name: "",
          model: config.main.model,
          cwd: config.cwd,
          orchestrationMode: config.orchestrationMode,
          browserHarnessEnabled: config.browserHarnessEnabled,
          providerId: config.main.providerId,
          browserHarnessHeadless: config.browserHarnessHeadless,
          fileEditEnabled: true,
          fileEditPath: config.fileEditPath,
          nodeId: config.nodeId,
          reasoningEffort: config.main.reasoningEffort,
          permission: config.main.permission,
          capabilityContexts: config.capabilityContexts,
          folderId: config.folderId,
        });
        setNewSessionModalOpen(false);
        setInvestigationCtx(undefined);
        if (session?.id) {
          navigateToCreatedSession(session);
          if (initialPrompt) {
            const pending = {
              sessionId: session.id,
              prompt: initialPrompt,
              images,
              files,
              model: config.main.model,
              cwd: config.cwd,
              orchestrationMode: config.orchestrationMode,
              capabilityContexts: config.capabilityContexts,
            };
            if (!sendInitialPromptToSession(pending)) {
              queueInitialPromptForSession(session.id, config, initialPrompt, images, files);
            }
          }
        }
      } catch (e) {
        if (isRetryableOfflineError(e)) {
          queueLocalFirstSession(config, initialPrompt, images, files);
          return;
        }
        const msg = e instanceof Error ? e.message : String(e);
        window.alert(msg);
      }
    },
    [connected, createSession, queueInitialPromptForSession, queueLocalFirstSession, navigateToCreatedSession, sendInitialPromptToSession],
  );

  const handleInvestigate = useCallback((data: InvestigationData) => {
    setInvestigationCtx({ prompt: data.prompt, images: data.images });
    setNewSessionModalOpen(true);
  }, []);

  /** Ask entry. Ensure the singleton exists backend-side, then route
   * to its session view. The view auto-detects the singleton id and
   * mounts Ask extension slots. */
  const handleAsk = useCallback(async () => {
    // Mark this Ask navigation as intentional so the auto-select effect
    // doesn't immediately redirect away from the Ask view.
    intentionalAskRef.current = true;
    try {
      await fetch(`${API}/api/extensions/ofek-dev.ask/backend/ask/ensure`, { method: "POST" });
    } catch (e) {
      // Ensure is best-effort: the singleton may already exist (race
      // with another tab) or the WS path will lazy-create on first
      // send. Don't block navigation on the REST round-trip.
      console.warn("ask/ensure failed", e);
    }
    navigate(sessionPath(ASK_SINGLETON_ID));
  }, [navigate]);
  const askExtensionContext = useMemo(
    () => ({
      openAsk: handleAsk,
      askSessionId: ASK_SINGLETON_ID,
      askSessionPath: sessionPath(ASK_SINGLETON_ID),
    }),
    [handleAsk],
  );

  /** Shared navigate/cwd context for manifest-declared extension UI hooks
   *  (quick buttons + page icons). */
  const hookActionContext = useMemo(
    () => ({
      navigate,
      cwd: selectedProjectPath || cwd || "",
      openAsk: handleAsk,
      askSessionPath: sessionPath(ASK_SINGLETON_ID),
    }),
    [navigate, selectedProjectPath, cwd, handleAsk],
  );
  const teamSidebarContext = useMemo(
    () => ({
      sessionId: currentSession?.id ?? "",
      cwd,
      model,
      providerId: currentSession?.provider_id ?? "",
      reasoningEffort: currentSession?.reasoning_effort ?? "",
      nodeId: currentSession?.node_id ?? "primary",
      workerCreationPolicy: currentSession?.worker_creation_policy ?? "ask",
      sessions,
      events,
    }),
    [
      currentSession?.id,
      currentSession?.provider_id,
      currentSession?.reasoning_effort,
      currentSession?.node_id,
      currentSession?.worker_creation_policy,
      cwd,
      model,
      sessions,
      events,
    ],
  );
  const machinePageContext = useMemo(
    () => ({
      activePage: "machines",
      onBack: () => {
        if (window.history.length > 1) window.history.back();
        else navigate("/");
      },
    }),
    [navigate],
  );
  /** Navigate to the project structure edit singleton. The backend owns
   *  queuing the maintainer review so the browser cannot duplicate-send it. */
  const handleProjectStructureEdit = useCallback(async () => {
    try {
      const projectCwd = selectedProjectPath || cwd;
      const res = await fetch(`${extBackendBase("projectStructure")}/project-structure-edit/ensure`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cwd: projectCwd }),
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        const message = data?.detail || data?.error || `project-structure-edit/ensure failed: ${res.status}`;
        window.alert(message);
        return;
      }
      const sessionId = data.session_id || editSingletonId();
      navigate(sessionPath(sessionId));
    } catch (e) {
      console.warn("project-structure-edit/ensure failed", e);
      const msg = e instanceof Error ? e.message : String(e);
      window.alert(msg);
    }
  }, [navigate, selectedProjectPath, cwd]);
  handleProjectStructureEditRef.current = handleProjectStructureEdit;

  /** Fetch one MessageImage's bytes from the singleton's image store and
   * convert to a PastedImage (dataUrl + raw base64). Used by both
   * `handleAskChoose` and `handleAskCreateNew` so an Ask prompt's
   * attachments survive the hand-off to the picked / new session. Lives
   * here because both handlers also produce
   * ImagePayload[] for `pendingInvestigationRef`; centralising the
   * fetch keeps the conversion one place. */
  const fetchAskImage = useCallback(
    async (img: import("./types").MessageImage): Promise<import("./components/InputArea").PastedImage> => {
      if (!img.filename) {
        throw new Error("ask image missing filename");
      }
      const res = await fetch(
        `${API}/api/sessions/${ASK_SINGLETON_ID}/images/${encodeURIComponent(img.filename)}`,
      );
      if (!res.ok) {
        throw new Error(`ask image fetch failed: ${res.status}`);
      }
      const blob = await res.blob();
      const dataUrl = await new Promise<string>((resolve, reject) => {
        const r = new FileReader();
        r.onload = () => resolve(r.result as string);
        r.onerror = () => reject(r.error);
        r.readAsDataURL(blob);
      });
      // dataUrl format: `data:<media>;base64,<payload>`. Split on the
      // comma; the suffix IS the raw base64 the ImagePayload contract
      // expects (no `data:` prefix).
      const base64 = dataUrl.split(",", 2)[1] ?? "";
      return { dataUrl, base64, mediaType: img.media_type };
    },
    [],
  );

  /** Picker → View. Jump to the session to look at it ONLY — no commit,
   * the decision stays open and the picker keeps its state. Pure
   * navigation; nothing is sent to the session and no choice is recorded. */
  const handleAskView = useCallback(
    (picked: Session) => {
      navigate(sessionPath(picked.id));
    },
    [navigate],
  );

  /** Session-bridge delegate-approval picker → resolve the pending
   * delegation. `picked` confirms the target (unblocks the waiting
   * `delegate_to_session` tool); `cancel` aborts it. */
  const resolveDelegation = useCallback(
    (delegationId: string, chosenSessionId: string | null) => {
      void fetch(
        `${SESSION_BRIDGE_API}/delegate/${delegationId}/resolve`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chosen_session_id: chosenSessionId }),
        },
      );
    },
    [],
  );

  /** Picker → Choose. The actual decision: record the pick on the
   * producing turn (so the chosen row stays highlighted across reloads /
   * tabs / previous turns), then navigate to the chosen session and
   * auto-submit the original raw query + attached images via the same
   * `pendingInvestigationRef` pattern the Investigate flow uses. The
   * picked session's own model/cwd/mode wins. */
  const handleAskChoose = useCallback(
    async (
      picked: Session,
      prompt: string,
      imageRefs: import("./types").MessageImage[],
      msgId: string,
    ) => {
      const trimmed = prompt.trim();
      if (!trimmed) return;
      // Persist the choice first so the highlight survives even if the
      // user navigates away before the prompt auto-submits.
      void fetch(
        `${API}/api/sessions/${ASK_SINGLETON_ID}/messages/${msgId}/ask-choice`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chosen_session_id: picked.id }),
        },
      ).catch((e) => console.warn("ask choose: persist failed", e));
      let images: ImagePayload[] = [];
      if (imageRefs.length > 0) {
        try {
          const fetched = await Promise.all(imageRefs.map(fetchAskImage));
          images = fetched.map((p) => ({ data: p.base64, media_type: p.mediaType }));
        } catch (e) {
          // Surface but don't block — user keeps their text prompt even
          // if the image hand-off failed.
          console.warn("ask choose: image hand-off failed", e);
        }
      }
      pendingInvestigationRef.current = {
        sessionId: picked.id,
        prompt: trimmed,
        images,
        files: [],
        model: picked.model,
        cwd: picked.cwd,
        orchestrationMode:
          (picked.orchestration_mode as OrchestrationMode) ?? "team",
        capabilityContexts: [],
      };
      navigate(sessionPath(picked.id));
    },
    [navigate, fetchAskImage],
  );

  const handleAskDismiss = useCallback((msgId: string) => {
    void fetch(
      `${API}/api/sessions/${ASK_SINGLETON_ID}/messages/${msgId}/ask-choice`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chosen_session_id: "__dismissed__" }),
      },
    ).catch((e) => console.warn("ask dismiss: persist failed", e));
  }, []);

  /** Optional project (path + node_id) the Ask agent proposed via the
   * `propose_sessions` MCP tool. Threaded into NewSessionModal as
   * `initialProjectPath` + `initialNodeId` to pre-select the project
   * AND its owning machine in the picker. Both cleared on modal close. */
  const [askProposedProjectPath, setAskProposedProjectPath] = useState<
    string | undefined
  >(undefined);
  const [askProposedProjectNodeId, setAskProposedProjectNodeId] = useState<
    string | undefined
  >(undefined);

  /** Picker → Create new anyway. Pre-fills the NewSessionModal with
   * the original raw query (rendered through the existing
   * `investigation` prop path so we don't grow a second auto-submit
   * channel) AND the Ask agent's project suggestion (pre-fills the
   * project + machine pickers; user can change). The modal's create
   * handler routes to `handleCreateSessionFromModal`, which seeds
   * `pendingInvestigationRef` with the new session id — and the
   * existing effect at App.tsx:861 fires the prompt once the WS lands
   * on the new session. */
  const handleAskCreateNew = useCallback(
    async (
      prompt: string,
      imageRefs: import("./types").MessageImage[],
      proposedProjectPath?: string,
      proposedProjectNodeId?: string,
      msgId?: string,
    ) => {
      // No early return on an empty prompt: the agent-initiated
      // `propose_sessions` shape carries no prompt at all (no user
      // message, no prompt_preview), so the seed text is legitimately
      // empty. Open the modal anyway and let the user type — the
      // button must never be a dead no-op.
      const trimmed = prompt.trim();
      if (msgId) {
        void fetch(
          `${API}/api/sessions/${ASK_SINGLETON_ID}/messages/${msgId}/ask-choice`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ chosen_session_id: "__new__" }),
          },
        ).catch((e) => console.warn("ask create-new: persist failed", e));
      }
      let images: import("./components/InputArea").PastedImage[] = [];
      if (imageRefs.length > 0) {
        try {
          images = await Promise.all(imageRefs.map(fetchAskImage));
        } catch (e) {
          // Image hand-off failed — preserve the text prompt and let
          // the user reattach in the modal if needed.
          console.warn("ask create-new: image hand-off failed", e);
        }
      }
      setInvestigationCtx({ prompt: trimmed, images });
      setAskProposedProjectPath(proposedProjectPath || undefined);
      setAskProposedProjectNodeId(proposedProjectNodeId || undefined);
      setNewSessionModalOpen(true);
    },
    [fetchAskImage],
  );

  /** Fork-and-send: forks the FOCUSED pane (root or any nested fork)
   * and submits the typed prompt to the new child via the existing
   * coordinator path. The new fork is appended under the focused
   * pane's `forks` array — supports arbitrarily deep nesting. The
   * `session_forked` WS event populates the tree on every viewing
   * tab (`appendFork` resolves the parent in-tree by id). */
  const handleForkAndSend = useCallback(
    async (
      prompt: string,
      images: import("./components/InputArea").PastedImage[]
    ): Promise<boolean> => {
      if (!currentTree || !currentSession) return false;
      const trimmed = prompt.trim();
      if (!trimmed) return false;
      const imagePayloads: ImagePayload[] = images.map((img) => ({
        data: img.base64,
        media_type: img.mediaType,
      }));
      const parentId = currentSession.id;
      const pendingId = `pending-${Date.now()}`;
      try {
        const handle = progressTrackPromise(
          `session:forkAndSend:${parentId}`,
          () =>
            fetch(`${API}/api/sessions/${parentId}/fork_and_send`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                prompt: trimmed,
                model,
                cwd: cwd || currentSession.cwd,
                orchestration_mode: currentSession?.orchestration_mode ?? undefined,
                images: imagePayloads.length > 0 ? imagePayloads : undefined,
                client_id: pendingId,
              }),
            }),
        );
        const res = await handle.promise;
        if (!res.ok) {
          const text = await res.text();
          alert(t("app.forkFailed") + text);
          return false;
        }
        const data = (await res.json()) as { child: Session };
        // Keep the op in-flight until the new child's first
        // turn_start arrives — the REST returned ChildId immediately
        // but the prompt runs async in the coordinator.
        if (data.child?.id) {
          const childId = data.child.id;
          handle.armWSExtender(
            makeSessionExtender(childId, "turn_start", "turn_complete"),
          );
        }
        // The session_forked WS event will populate the tree. Set
        // focus immediately so the new pane is the active target as
        // soon as it appears.
        if (data.child?.id) {
          setFocusedForkId(data.child.id);
          // Optimistically insert (in case our own session_forked
          // arrives after we re-render). appendFork de-dupes by id.
          appendFork(data.child, parentId);
        }
        // Add an optimistic pending bubble on the new fork so the
        // user sees their prompt immediately.
        if (data.child?.id) {
          const childId = data.child.id;
          const pendingMsg: ChatMessage = {
            id: pendingId,
            role: "user",
            content: trimmed,
            events: [],
            timestamp: new Date().toISOString(),
            isStreaming: false,
            status: "sending",
          };
          appendPendingForSession(childId, pendingMsg);
        }
        return true;
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        alert(t("app.forkFailed") + msg);
        return false;
      }
    },
    [
      currentTree,
      currentSession,
      model,
      cwd,
      appendFork,
      setPendingForSession,
      appendPendingForSession,
    ]
  );

  /** Close a fork pane — backend persists `fork_closed=true`, frontend
   * applies the flip optimistically (the WS echo from
   * session_metadata_updated converges other tabs). The "focus is now
   * on a closed pane" case (whether we closed it or another tab did)
   * is handled by the effect below. */
  const handleCloseFork = useCallback(
    async (forkSessionId: string) => {
      applySessionMetadata(forkSessionId, { fork_closed: true });
      try {
        await progressTrackedFetch(
          `session:closeFork:${forkSessionId}`,
          `${API}/api/sessions/${forkSessionId}/close_fork`,
          { method: "POST" },
        );
      } catch {
        // ignore — the next session_metadata_updated echo will heal.
      }
    },
    [applySessionMetadata]
  );

  /** Reopen a previously-closed fork — flips `fork_closed` back to
   * false; the pane becomes focusable again. */
  const handleReopenFork = useCallback(
    async (forkSessionId: string) => {
      applySessionMetadata(forkSessionId, { fork_closed: false });
      try {
        await progressTrackedFetch(
          `session:reopenFork:${forkSessionId}`,
          `${API}/api/sessions/${forkSessionId}/reopen_fork`,
          { method: "POST" },
        );
      } catch {
        // ignore — WS echo heals state.
      }
    },
    [applySessionMetadata]
  );

  /** Switch focus to a different pane in the split view. Validated:
   * cannot focus a closed fork. */
  const handleSetForkFocus = useCallback(
    (forkSessionId: string) => {
      const node = getNode(forkSessionId);
      if (!node) return;
      if (node.fork_closed) return;
      setFocusedForkId(forkSessionId);
    },
    [getNode]
  );

  /** Whenever the focused pane becomes closed — whether we closed it,
   * another tab did, or the WS echo just landed — pick the nearest
   * still-open pane (depth-first: root → fork1 → fork2 …). Falls back
   * to the root id even if the root is somehow closed; a closed
   * focused pane is gated downstream by `Send`/`Fork` button disable. */
  useEffect(() => {
    if (!currentTree || !focusedForkId) return;
    const focused = getNode(focusedForkId);
    if (!focused || focused.fork_closed) {
      const collect = (node: Session, acc: Session[]) => {
        if ((node.kind ?? "user") !== "user") return acc;
        if (!node.fork_closed) acc.push(node);
        for (const f of node.forks ?? []) collect(f, acc);
        return acc;
      };
      const open = collect(currentTree, []);
      const next = open[0]?.id ?? currentTree.id;
      if (next !== focusedForkId) setFocusedForkId(next);
    }
  }, [currentTree, focusedForkId, getNode]);

  const handleFileClick = useCallback(
    (path: string, focus?: FileFocus) => {
      handleOpenFilePanel(path, focus ?? null);
    },
    [handleOpenFilePanel],
  );

  const handleViewDiff = useCallback(async (path: string, oldStr: string, newStr: string) => {
    // Local user-initiated diff view — force the panel open on the
    // active session so the FileViewer slot is visible immediately.
    if (isMobile) {
      setMobileRightOpen(true);
      setMobileSidebarOpen(false);
    } else if (currentSession) {
      patchRightPanel(currentSession.id, { open: true, tab: "files", clearAutoReasons: true });
    }
    setRightPanelTab("files");
    try {
      const resp = await progressTrackedFetch(
        `file:beforeEdit:${path}`,
        `${API}/api/file-before-edit`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ file_path: path, old_string: oldStr, new_string: newStr }),
        },
      );
      const data = await resp.json();
      setViewingFile({ path, diffBefore: data.before_content, diffAfter: data.after_content });
    } catch {
      handleOpenFilePanel(path);
    }
  }, [handleOpenFilePanel, isMobile, currentSession, patchRightPanel]);

  // Trim the outer sidebar while the file-edit overlay is active so the
  // chat + file panels get more room. The user's persisted preference
  // (sidebar.size) is left untouched so it restores on exit. 200px matches
  // useResizable's `min` so contents that already fit at the minimum
  // still render correctly.
  const FILE_EDIT_SIDEBAR_WIDTH = 200;
  const effectiveSidebarWidth = !isMobile && sidebarMinimized
    ? SIDEBAR_MINIMIZED_WIDTH
    : fileEditingState
    ? FILE_EDIT_SIDEBAR_WIDTH
    : sidebar.size;

  // Inline width is desktop-only. On mobile/tablet the CSS overrides
  // width via the drawer rules (using `!important`), but skipping the
  // inline style here avoids fighting CSS specificity and keeps DOM
  // diff cleaner.
  const sidebarStyle = isMobile
    ? undefined
    : { width: effectiveSidebarWidth, minWidth: effectiveSidebarWidth };
  const rightPanelStyle = isMobile
    ? undefined
    : { width: rightPanel.size, minWidth: rightPanel.size };
  const mobileRightPanelStyle =
    isMobile && isPortrait && rightPanelVisible && !mobileRightFullscreen
      ? { height: mobileRightPanel.size, minHeight: mobileRightPanel.size }
      : undefined;

  const sessionsForProject = useMemo(
    () =>
      selectedProjectPath
        ? sessions.filter(
            (s) =>
              s.cwd === selectedProjectPath
              && (s.node_id || "primary") === selectedProjectNodeId,
          )
        : machines.length > 1
          ? sessions.filter((s) => (s.node_id || "primary") === selectedProjectNodeId)
          : sessions,
    [sessions, selectedProjectPath, selectedProjectNodeId, machines.length]
  );

  // When multiple machines exist, filter project tabs to the selected node.
  const projectsForMachine = useMemo(
    () =>
      machines.length > 1
        ? projects.filter((p) => (p.node_id || "primary") === selectedProjectNodeId)
        : projects,
    [projects, machines.length, selectedProjectNodeId]
  );

  const handleSelectMachine = useCallback(
    (nodeId: string) => {
      setSelectedProjectNodeId(nodeId);
    },
    [],
  );
  const machineTabsContext = useMemo(
    () => ({
      activeScope: "machines",
      machines,
      selectedNodeId: selectedProjectNodeId,
      onSelect: handleSelectMachine,
    }),
    [machines, selectedProjectNodeId, handleSelectMachine],
  );

  // The team-orchestration worker panel is surfaced as a Workers tab next to
  // the Sessions list whenever the extension + its sidebar module are present.
  const workersTabAvailable = !!(
    builtinExtensions.team &&
    cwd &&
    teamSidebarModules.length > 0
  );

  return (
    <MobileActionSheetProvider>
    <InvestigateContextMenu onInvestigate={handleInvestigate} activeSessionId={currentSession?.id} activeSessionCwd={currentSession?.cwd}>
    <>
      {(!sessionsLoaded || authStatus === "loading") && (
        <div className="app-splash-overlay">
          <div className="app-splash-content">
            <div className="app-splash-logo" aria-label="Better Agent">
              <BetterAgentBrandMark className="app-splash-brand-mark" />
              <span>Better Agent</span>
            </div>
            <div className="app-splash-spinner"></div>
            <div className="app-splash-status">
              {authStatus === "loading" ? "Authenticating..." : "Loading sessions..."}
            </div>
          </div>
        </div>
      )}
      <StartupTasksBanner />
      {authStatus === "authed" &&
        sessionDragOverlayModules.map((module) => (
          <ExtensionModuleSlot
            key={`${module.extension_id}:${module.id}`}
            module={module}
            className="extension-module-slot--overlay"
            context={{
              draggingSessionId: draggingSession?.id ?? null,
              draggingSessionName: draggingSession?.name ?? null,
              sessionDragMime: SESSION_DRAG_MIME,
            }}
          />
        ))}
      {builtinExtensions.machineNodes &&
        globalApprovalModules.map((module) => (
          <ExtensionModuleSlot
            key={`${module.extension_id}:${module.id}`}
            module={module}
            className="extension-module-slot--overlay"
            context={{ activeApproval: "machine-node", authStatus }}
          />
        ))}
      <DonationWelcomeModal
        open={donationWelcomeMilestone !== null}
        milestone={donationWelcomeMilestone}
        onClose={() => {
          if (donationWelcomeMilestone !== null) {
            donationWelcomeStorage.dismissMilestone(donationWelcomeMilestone);
          }
          setDonationWelcomeMilestone(null);
        }}
      />
      {!connected && offlineQueue.queue.length > 0 && (
        <div className="offline-banner">
          <span className="offline-banner-dot" />
          Offline — {offlineQueue.queue.length} action{offlineQueue.queue.length !== 1 ? "s" : ""} queued
        </div>
      )}
      {restartError && (
        <div className="restart-error-banner" role="alert">
          <span className="restart-error-banner-text">{restartError}</span>
          <button
            className="restart-error-banner-close"
            onClick={() => setRestartError(null)}
            aria-label={t("startup_tasks.dismiss")}
            title={t("startup_tasks.dismiss")}
          >
            <Icon name="x" size={18} />
          </button>
        </div>
      )}
      {authStatus === "authed" &&
        route.kind === "machines" &&
        builtinExtensions.machineNodes &&
        routePageModules.map((module) => (
          <ExtensionModuleSlot
            key={`${module.extension_id}:${module.id}`}
            module={module}
            context={machinePageContext}
          />
        ))}
      {authStatus === "authed" && route.kind === "analytics" && (
        <Suspense fallback={<LazySurfaceFallback />}>
          <AnalyticsPage
            onBack={() => {
              if (window.history.length > 1) window.history.back();
              else navigate("/");
            }}
          />
        </Suspense>
      )}
      {authStatus === "authed" && route.kind === "settings" && (
        <SettingsPage
          onClose={() => {
            if (window.history.length > 1) window.history.back();
            else navigate("/");
          }}
          onRefreshApp={openRefreshModal}
          refreshAppDisabled={restarting}
          teamEnabled={builtinExtensions.team}
          credentialBrokerEnabled={builtinExtensions.credentialBroker}
          providerConfigSyncEnabled={builtinExtensions.providerConfigSync}
          onOpenProviderConfigSync={
            builtinExtensions.providerConfigSync
              ? () => openProviderConfigSyncPage(API)
              : undefined
          }
        />
      )}
      {authStatus === "authed" && route.kind === "share" && (
        <SharePicker
          images={sharedImages}
          projects={projects}
          sessions={sessions}
          onPick={attachImagesToSession}
          onCancel={cancelShare}
        />
      )}
      {authStatus === "authed" && route.kind === "providerConfigSync" && builtinExtensions.providerConfigSync && (
        <Suspense fallback={<LazySurfaceFallback />}>
          <ProviderConfigSyncPage
            open
            cwd={currentSession?.cwd ?? null}
            onClose={() => navigate("/")}
            client={providerConfigSyncClient}
            subscribeExternalChanges={(cb) => {
              const offProvider = eventBus.subscribe("provider_config_sync_changed", () => cb());
              const offExtensions = eventBus.subscribe("extensions_changed", () => cb());
              return () => {
                offProvider();
                offExtensions();
              };
            }}
          />
        </Suspense>
      )}
      {authStatus === "authed" &&
        (route.kind === "session" || route.kind === "emptyProject") && (
    <div className="app">
      {isMobile && (
        <header className="mobile-topbar">
          <button
            className={
              "mobile-topbar-btn" + (mobileSidebarOpen ? " active" : "")
            }
            onClick={() => {
              setMobileSidebarOpen((v) => !v);
              setMobileRightOpen(false);
            }}
            aria-label={t("app.toggleSidebar")}
            title={t("app.toggleSidebar")}
          >
            <Icon name="menu" size={20} />
          </button>
          <span className="mobile-topbar-title">
            {currentSession?.name ?? t("app.title")}
          </span>
          <ExtensionQuickButtons context={hookActionContext} variant="topbar" />
          {builtinExtensions.ask &&
            currentSession?.id !== ASK_SINGLETON_ID &&
            mobileSessionTopbarModules.map((module) => (
              <ExtensionModuleSlot
                key={`${module.extension_id}:${module.id}`}
                module={module}
                className="extension-module-slot--topbar"
                context={askExtensionContext}
              />
            ))}
          <button
            className="mobile-topbar-btn"
            onClick={() => setNewSessionModalOpen(true)}
            aria-label={t("newSession.title")}
            title={t("newSession.title")}
          >
            +
          </button>
        </header>
      )}

      {isMobile && (mobileSidebarOpen || (mobileRightOpen && !isPortrait)) && (
        <div
          className="mobile-backdrop"
          onClick={() => {
            setMobileSidebarOpen(false);
            closeMobileRightPanel();
          }}
        />
      )}

      {/* Left Sidebar — width driven by useResizable on desktop;
          drawer on mobile/tablet. Mobile drawer carries dialog
          semantics so screen readers announce a modal context and
          Escape (wired above) is interpreted as "close drawer". */}
      <div
        className={
          "sidebar"
            + (isMobile && mobileSidebarOpen ? " mobile-drawer-open" : "")
            + (!isMobile && sidebarMinimized ? " sidebar-minimized" : "")
        }
        style={sidebarStyle}
        role={isMobile ? "dialog" : undefined}
        aria-modal={isMobile && mobileSidebarOpen ? true : undefined}
        aria-label={isMobile ? t("sidebar.drawerLabel") : undefined}
        aria-hidden={isMobile && !mobileSidebarOpen ? true : undefined}
      >
        {!isMobile && sidebarMinimized ? (
          <div className="sidebar-minimized-rail">
            <button
              className="setup-btn sidebar-minimize-btn"
              onClick={() => setSidebarMinimized(false)}
              title={t("sidebar.expand")}
              aria-label={t("sidebar.expand")}
            >
              <Icon name="chevron-right" size={18} />
            </button>
          </div>
        ) : (
        <>
        <div className="sidebar-top">
          <div className="sidebar-header-row">
            {!isMobile && (
              <button
                className="setup-btn sidebar-minimize-btn"
                onClick={() => setSidebarMinimized(true)}
                title={t("sidebar.minimize")}
                aria-label={t("sidebar.minimize")}
              >
                <Icon name="chevron-left" size={18} />
              </button>
            )}
            <div className="app-title-brand">
              <BetterAgentBrandMark className="sidebar-brand-mark" />
              <h1 className="app-title">{t("app.title")}</h1>
            </div>
            {Object.keys(processingByRoot).length > 0 && (
              <span
                className="reconciling-chip"
                title={t("app.reconcilingTitle")}
              >
                {t("app.reconciling")}
              </span>
            )}
            {(() => {
              const closeMenu = () => setMobileHeaderMenuOpen(false);
              const secondary = (
                <>
                  {showMachinesLink && (
                    <button
                      className="setup-btn"
                      onClick={() => {
                        navigate("/machines");
                        closeMenu();
                      }}
                      title={t("sidebar.machinesLink")}
                      aria-label={t("sidebar.machinesLink")}
                    >
                      <Icon name="server" size={18} />
                    </button>
                  )}
                  <button
                    className="setup-btn"
                    onClick={() => {
                      navigate("/analytics");
                      closeMenu();
                    }}
                    title={t("analytics.title")}
                    aria-label={t("analytics.title")}
                  >
                    <Icon name="chart" size={18} />
                  </button>
                  <button
                    className="setup-btn"
                    onClick={() => {
                      openRefreshModal();
                      closeMenu();
                    }}
                    disabled={restarting}
                    title={t("app.refreshButtonTitle")}
                    aria-label={t("app.refreshButtonTitle")}
                  >
                    {restarting ? "…" : <Icon name="refresh" size={18} />}
                  </button>
                  <ExtensionPageIcons context={hookActionContext} />
                </>
              );
              return isMobile ? (
                <div
                  className="header-overflow-wrapper"
                  ref={mobileHeaderMenuRef}
                >
                  <button
                    className="setup-btn header-overflow-trigger"
                    onClick={() => setMobileHeaderMenuOpen((v) => !v)}
                    aria-label={t("app.moreActions")}
                    aria-expanded={mobileHeaderMenuOpen}
                    title={t("app.moreActions")}
                  >
                    <Icon name="more-vertical" size={18} />
                  </button>
                  {mobileHeaderMenuOpen && (
                    <div
                      className="header-overflow-menu"
                      onClick={() => setMobileHeaderMenuOpen(false)}
                    >
                      {secondary}
                    </div>
                  )}
                </div>
              ) : (
                secondary
              );
            })()}
            <button
              className="setup-btn"
              onClick={() => navigate("/settings")}
              title={t("app.settingsButtonTitle")}
              aria-label={t("app.settingsButtonTitle")}
            >
              <Icon name="settings" size={18} />
            </button>
          </div>
          {builtinExtensions.machineNodes && machines.length > 1 && (
            <>
              <div className="sidebar-tab-group-title">{t("machines.title")}</div>
              {sidebarScopeModules.map((module) => (
                <ExtensionModuleSlot
                  key={`${module.extension_id}:${module.id}`}
                  module={module}
                  context={machineTabsContext}
                  className="extension-module-slot--inline"
                />
              ))}
            </>
          )}
          <div className="sidebar-tab-group-title">{t("projects.header")}</div>
          <ProjectTabs
            projects={projectsForMachine}
            currentPath={selectedProjectPath || cwd}
            currentNodeId={selectedProjectNodeId}
            onSelect={handleSelectProject}
            onAdd={() => setDirPickerOpen(true)}
            onRemove={handleRemoveProject}
            onOpenSettings={(path) => setProjectSettingsCwd(path)}
            projectUpdatesCounts={projectUpdatesCounts}
            disabled={aiSearchActive}
          />
        </div>

        {selectedProjectPath && (
          <div className="project-title-bar">
            <span className="project-title-name">
              {projects.find(
                (p) =>
                  p.path === selectedProjectPath &&
                  (p.node_id || "primary") === selectedProjectNodeId,
              )?.name ||
                selectedProjectPath.replace(/\/+$/, "").split("/").pop() ||
                selectedProjectPath}
            </span>
            <ProjectGitStatus
              cwd={selectedProjectPath}
              nodeId={selectedProjectNodeId}
            />
          </div>
        )}

        <div ref={setSelectedAnchorEl} className="sidebar-selected-anchor" />

        {workersTabAvailable ? (
          <div className="sidebar-tabs" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={sidebarTab === "sessions"}
              className={`sidebar-tab${sidebarTab === "sessions" ? " active" : ""}`}
              onClick={() => setSidebarTab("sessions")}
            >
              {t("sidebar.sessionsTab")}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={sidebarTab === "workers"}
              className={`sidebar-tab${sidebarTab === "workers" ? " active" : ""}`}
              onClick={() => setSidebarTab("workers")}
            >
              {t("sidebar.workersTab")}
            </button>
          </div>
        ) : null}

        <div
          className="session-list-wrapper"
          style={isMobile ? undefined : { flex: "1 1 auto", minHeight: 0 }}
        >
          {workersTabAvailable && sidebarTab === "workers" ? (
            <div className="sidebar-workers-panel">
              {teamSidebarModules.map((module) => (
                <ExtensionModuleSlot
                  key={`${module.extension_id}:${module.id}`}
                  module={module}
                  context={teamSidebarContext}
                />
              ))}
            </div>
          ) : (
            <SessionList
              sessions={sessionsForProject}
              allSessions={sessions}
              currentSessionId={currentSession?.id}
              selectedSession={currentSession}
              selectedAnchorContainer={selectedAnchorEl}
              providers={providers}
              onSelect={(id) => {
                const s = sessions.find((s) => s.id === id);
                if (s) {
                  setSelectedProjectPath(s.cwd);
                  setSelectedProjectNodeId(s.node_id || "primary");
                }
                navigate(sessionPath(id));
                if (isMobile) setMobileSidebarOpen(false);
              }}
              onDelete={handleDeleteSession}
              onRename={renameSession}
              onPin={togglePin}
              onUnpinOthers={unpinOtherSessions}
              onArchive={archiveSession}
              onWorkerEligible={toggleWorkerEligible}
              onDetails={setDetailsSessionId}
              onResumeEng={handleResumeEng}
              onAiSearch={searchSessions}
              onAiActiveChange={setAiSearchActive}
              backendProjectPath={selectedProjectPath}
              onBackendFiltersChange={setSessionListFilters}
              onCreate={() => setNewSessionModalOpen(true)}
              hasMore={sessionsHasMore}
              searching={sessionsSearching}
              loadingMore={sessionsLoadingMore}
              onLoadMore={loadMoreSessions}
            />
          )}
        </div>

        <div className="sidebar-bottom">
          {cwd && (
            <button
              className="sidebar-tools-btn"
              onClick={() => setFileChooserOpen(true)}
              title={t("sidebar.toolsTitle")}
              aria-label={t("sidebar.toolsTitle")}
            >
              <Icon name="folder" size={14} /> {t("sidebar.tools")}
            </button>
          )}
          <TokenUsageDisplay
            usage={sessionTokenUsage}
            usageLast={sessionTokenUsageLast}
            rearrangerStats={currentSession?.rearranger_stats ?? null}
            connected={connected}
            contextWindow={currentSession?.context_window ?? null}
            collapsible={isMobile}
          />
          <div className="sidebar-user-row" title={authedUser?.username}>
            <span className="sidebar-user-name">{authedUser?.username}</span>
            <button
              className="sidebar-logout-btn"
              onClick={onLogout}
              title={t("login.logout")}
            >
              {t("login.logout")}
            </button>
          </div>
        </div>
        </>
        )}
      </div>

      {/* Sidebar / main-panel divider — drag disabled while file-edit overlay
          overrides the sidebar width. Hidden on mobile (drawer mode). */}
      {!fileEditingState && !isMobile && !sidebarMinimized && (
        <div className="sidebar-resizer" onMouseDown={sidebar.onMouseDown} />
      )}

      {/* Center Panel */}
      <div className="main-panel">
        {(() => {
          // Empty-project surface: the selected (machine, project) has no
          // sessions. Shown instead of falling back to Ask. The New
          // session button opens the modal pre-filled with this project.
          if (route.kind === "emptyProject") {
            const project = projects.find(
              (p) =>
                p.path === selectedProjectPath &&
                (p.node_id || "primary") === selectedProjectNodeId,
            );
            const projectLabel =
              project?.name ||
              selectedProjectPath.replace(/\/+$/, "").split("/").pop() ||
              selectedProjectPath;
            const machineLabel =
              machines.length > 1
                ? selectedProjectNodeId === "primary"
                  ? t("dirPicker.thisMachine")
                  : selectedProjectNodeId
                : null;
            return (
              <div className="empty-project">
                <div className="empty-project-card">
                  <div className="empty-project-project">{projectLabel}</div>
                  {machineLabel && (
                    <div className="empty-project-machine">{machineLabel}</div>
                  )}
                  <div className="empty-project-body">
                    {t("emptyProject.body")}
                  </div>
                  <button
                    className="empty-project-new-btn"
                    onClick={() => setNewSessionModalOpen(true)}
                  >
                    {t("session.newButton")}
                  </button>
                </div>
              </div>
            );
          }
          const streamBelongsToCurrentSession =
            !!streamingAppSessionId &&
            currentSession?.id === streamingAppSessionId;
          // While the prompt-eng overlay is up, currentSession IS the eng
          // session (we selectSession(engSessionId) right after start), so
          // this Chat element renders the eng-session's chat. The overlay
          // wraps it on the left and adds a FileViewer on the right.
          const supervisorBannerElement = supervisorBanner ? (
            <div
              className={`supervisor-banner supervisor-banner-${supervisorBanner.kind}`}
              role="status"
            >
              <span className="supervisor-banner-text">{supervisorBanner.message}</span>
              <button
                className="supervisor-banner-close"
                onClick={() => setSupervisorBanner(null)}
                aria-label={t("app.supervisorBannerDismiss")}
              >
                ×
              </button>
            </div>
          ) : null;
          const prToastElement = prToast ? (
            <div className="pr-toast" role="status">
              <svg
                className="pr-toast-icon"
                width="16"
                height="16"
                viewBox="0 0 16 16"
                fill="currentColor"
                aria-hidden="true"
              >
                <path d="M3.25 1A2.25 2.25 0 0 0 2.5 5.372V10.628a2.25 2.25 0 1 0 1.5 0V5.372A2.25 2.25 0 0 0 3.25 1Zm0 1.5a.75.75 0 1 1 0 1.5.75.75 0 0 1 0-1.5Zm0 9.25a.75.75 0 1 1 0 1.5.75.75 0 0 1 0-1.5ZM12.75 3a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm-2.25.75a2.25 2.25 0 1 1 3 2.122v4.756a2.25 2.25 0 1 1-1.5 0V5.872A2.25 2.25 0 0 1 10.5 3.75Zm2.25 8a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Z" />
              </svg>
              <a
                className="pr-toast-link"
                href={prToast.prUrl}
                target="_blank"
                rel="noopener noreferrer"
                title={prToast.prUrl}
              >
                <span className="pr-toast-title">
                  {prToast.prNumber
                    ? `Pull request #${prToast.prNumber} created`
                    : "Pull request created"}
                </span>
                {prToast.prRepository && (
                  <span className="pr-toast-repo">{prToast.prRepository}</span>
                )}
              </a>
              <button
                className="pr-toast-close"
                onClick={() => setPrToast(null)}
                aria-label="Dismiss"
              >
                ×
              </button>
            </div>
          ) : null;
          // Ask-singleton view: the regular <Chat> rendered for the
          // singleton. The greeting box is injected as the chat's header
          // slot; the inline session picker is injected PER TURN via
          // `renderGroupFooter` (each ask turn carries its own
          // `ask_result` on its assistant message), so previous turns keep
          // their picker + chosen highlight. The user's RAW prompt is the
          // persisted user_msg.content (the index+contract wrapper goes to
          // the model via `cli_prompt`, never persisted).
          const isAskView = currentSession?.id === ASK_SINGLETON_ID;
          const fullMessages =
            currentSession?.messages ?? (EMPTY_MSGS as ChatMessage[]);
          const chatMessages = fileEditingState
            ? fullMessages.filter((m) => !m.file_discussion_id)
            : fullMessages;
          const chatPendingMessages = fileEditingState
            ? pendingMessages.filter((m) => !m.file_discussion_id)
            : pendingMessages;
          const askHasPendingPrompt =
            isAskView && (pendingBySession[ASK_SINGLETON_ID]?.length ?? 0) > 0;
          const askHasUnresolvedResult =
            isAskView &&
            fullMessages.some((m) => {
              const ar = m.ask_result;
              if (!ar || ar.purpose === "delegate_approval") return false;
              return !ar.resolved && !m.chosen_session_id;
            });
          const askIsEmpty =
            isAskView && fullMessages.length === 0 && !askHasPendingPrompt;
          const askGreetingSlots = askGreetingModules.map((module) => (
            <ExtensionModuleSlot
              key={`${module.extension_id}:${module.id}`}
              module={module}
            />
          ));
          // Greeting renders at the top of the scroll area (headerNode).
          // When Ask is empty it's wrapped so it vertically centers as a hero;
          // once there's history it sits as a compact card above the messages.
          const askDescriptionNode =
            isAskView && !askHasPendingPrompt && !askHasUnresolvedResult
              ? askIsEmpty
                ? <div className="ask-hero-wrap">{askGreetingSlots}</div>
                : askGreetingSlots
              : undefined;
          const chatElement = (
            <ConfigPanelContext.Provider
              value={{
                client: providerConfigSyncClient,
                subscribeExternalChanges: (cb) => {
                  const offProvider = eventBus.subscribe("provider_config_sync_changed", () => cb());
                  const offExtensions = eventBus.subscribe("extensions_changed", () => cb());
                  return () => {
                    offProvider();
                    offExtensions();
                  };
                },
                open: handleOpenConfigPanel,
                activeInlineId: activeInlineConfigId,
                claimInline: claimInlineConfigPanel,
                releaseInline: releaseInlineConfigPanel,
              }}
            >
            <Chat
              headerNode={askDescriptionNode}
              getGroupClassName={(g) => {
                if (!isAskView) return undefined;
                const ar = g.assistantMessage?.ask_result;
                const isResolved = ar?.resolved || g.assistantMessage?.chosen_session_id;
                return isResolved ? "ask-group ask-group--resolved" : "ask-group";
              }}
              renderGroupFooter={(g) => {
                const ar = g.assistantMessage?.ask_result;
                if (!ar || !g.assistantMessage) return null;
                // Delegate-approval picker: renders in ANY session when a
                // session-bridge delegation is awaiting the user's pick.
                if (ar.purpose === "delegate_approval") {
                  const delegationId = ar.delegation_id;
                  // Cleared once resolved (chosen/cancelled/expired) so the
                  // footer disappears in every open tab.
                  if (!delegationId || ar.resolved) return null;
                  return (
                    askSessionPickerModules.map((module) => (
                      <ExtensionModuleSlot
                        key={`${module.extension_id}:${module.id}`}
                        module={module}
                        context={{
                          askResult: ar,
                          chosenSessionId: g.assistantMessage?.chosen_session_id ?? null,
                          allSessions: sessions,
                          onView: handleAskView,
                          onChoose: (picked: Session) => resolveDelegation(delegationId, picked.id),
                          onApproveNew: () => resolveDelegation(delegationId, "__new__"),
                          onCreateNew: () => resolveDelegation(delegationId, null),
                          createLabel: ar.create_new ? "Cancel" : undefined,
                        }}
                      />
                    ))
                  );
                }
                // Ask flow picker (singleton only).
                if (!isAskView) return null;
                return (
                  askSessionPickerModules.map((module) => (
                    <ExtensionModuleSlot
                      key={`${module.extension_id}:${module.id}`}
                      module={module}
                      context={{
                        askResult: ar,
                        chosenSessionId: g.assistantMessage?.chosen_session_id ?? null,
                        allSessions: sessions,
                        onView: handleAskView,
                        onChoose: (picked: Session) =>
                          handleAskChoose(
                            picked,
                            resolveAskPrompt(g.userMessage.content, ar.prompt_preview),
                            g.userMessage.images ?? [],
                            g.assistantMessage!.id,
                          ),
                        onCreateNew: () =>
                          handleAskCreateNew(
                            resolveAskPrompt(g.userMessage.content, ar.prompt_preview),
                            g.userMessage.images ?? [],
                            ar.proposed_project_path || undefined,
                            ar.proposed_project_node_id || undefined,
                            g.assistantMessage!.id,
                          ),
                        onDismiss: () => handleAskDismiss(g.assistantMessage!.id),
                      }}
                    />
                  ))
                );
              }}
              openSessions={
                isAskView ? [] : sortedOpenSessions
              }
              sessionTabsVisible={sessionTabsVisible}
              sessionTabsSort={sessionTabsSort}
              providers={providers}
              onCloseTab={handleCloseTab}
              onSelectTab={handleSelectTab}
              messages={chatMessages}
              pendingMessages={chatPendingMessages}
              runs={
                (currentSession
                  ? runStateBySession[currentSession.id]
                  : undefined) ?? (EMPTY_RUNS_PROP as import("./types").RunInfo[])
              }
              streamingEvents={
                streamBelongsToCurrentSession
                  ? events
                  : (EMPTY_EVENTS as import("./types").WSEvent[])
              }
              traceSteps={
                streamBelongsToCurrentSession
                  ? traceSteps
                  : (EMPTY_EVENTS as import("./types").WSEvent[])
              }
              isStreaming={streamBelongsToCurrentSession ? isStreaming : false}
              isStopping={streamBelongsToCurrentSession ? isStopping : false}
              streamingLoadPhase={streamBelongsToCurrentSession ? streamingLoadPhase : null}
              onSend={handleSend}
              onSteer={currentSessionCanSteer ? handleSteer : undefined}
              onInterrupt={handleInterrupt}
              onAlterUserMessage={handleAlterUserMessage}
              canSteer={currentSessionCanSteer}
              onStop={handleStop}
              onRetry={handleRetry}
              onRetryStopped={handleRetryStopped}
              onFileClick={handleFileClick}
              onViewDiff={handleViewDiff}
              disabled={!currentSession}
              session={currentSession}
              onToggleRearranger={
                builtinExtensions.rearranger
                  ? (enabled) => {
                      if (!currentSession) return;
                      updateRearranger(currentSession.id, { enabled });
                      void fetch(`${rearrangerApi()}/toggle`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                          app_session_id: currentSession.id,
                          enabled,
                        }),
                      });
                    }
                  : undefined
              }
              onToggleSupervisor={
                builtinExtensions.supervisor
                  ? (enabled) => {
                      if (!currentSession) return;
                      if (enabled) {
                        setSupervisorPromptModalMode("enable");
                        setSupervisorPromptModalOpen(true);
                      } else {
                        applySessionMetadata(currentSession.id, { supervisor_enabled: false });
                        void progressTrackedFetch(
                          `session:supervisorToggle:${currentSession.id}`,
                          `${supervisorApi()}/sessions/${currentSession.id}/supervisor-toggle`,
                          {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ enabled: false }),
                          },
                        );
                      }
                    }
                  : undefined
              }
              onEditSupervisorPrompt={
                builtinExtensions.supervisor
                  ? () => {
                      if (!currentSession) return;
                      setSupervisorPromptModalMode("edit");
                      setSupervisorPromptModalOpen(true);
                    }
                  : undefined
              }
              onSeparateSupervisor={
                builtinExtensions.supervisor
                  ? async () => {
                      if (!currentSession) return;
                      try {
                        const res = await progressTrackedFetch(
                          `session:separateSupervisor:${currentSession.id}`,
                          `${supervisorApi()}/sessions/${currentSession.id}/separate_supervisor`,
                          { method: "POST" },
                        );
                        if (!res.ok) {
                          const msg = res.status === 409
                            ? t("supervisor.separateFailedBusy")
                            : t("supervisor.separateFailed");
                          console.warn("separate_supervisor failed", res.status, msg);
                          return;
                        }
                        const data = await res.json();
                        const newId: string | undefined = data?.new_session_id;
                        if (newId) {
                          refreshSessions();
                          navigate(sessionPath(newId));
                        }
                      } catch (err) {
                        console.warn("separate_supervisor threw", err);
                      }
                    }
                  : undefined
              }
              onAddCapabilityToNextTurn={
                builtinExtensions.providerConfigSync
                  ? () => setTurnCapabilityPickerOpen(true)
                  : undefined
              }
              nextTurnCapabilities={
                currentSession
                  ? turnCapabilityContextsBySession[currentSession.id] ?? []
                  : []
              }
              onRemoveNextTurnCapability={(sourceId) => {
                if (!currentSession) return;
                setTurnCapabilityContextsBySession((prev) => ({
                  ...prev,
                  [currentSession.id]: (prev[currentSession.id] ?? []).filter(
                    (item) => item.source_id !== sourceId,
                  ),
                }));
              }}
              tags={tags}
              onAddTag={handleAddTag}
              onAdvSync={handleAdvSync}
              onAdvSyncClick={handleAdvSyncClick}
              onRemoveTag={handleRemoveTag}
              onRename={renameSession}
              draft={currentSession?.draft_input ?? ""}
              onDraftChange={(value) => {
                if (!currentSession) return;
                handleDraftChange(currentSession.id, value);
              }}
              draftImages={currentSession?.draft_images}
              onImagesChange={(images, text) => {
                if (!currentSession) return;
                handleImagesChange(currentSession.id, images, text);
              }}
              // Suppress the ⚙ button while we're already in eng mode —
              // nesting eng sessions is meaningless and confusing.
              onEngineer={
                promptEngState || !builtinExtensions.promptEngineer
                  ? undefined
                  : (draft) => {
                      if (!currentSession) return;
                      setPromptEngStartError("");
                      setPromptEngModalDraft(draft);
                    }
              }
              tree={currentTree}
              pendingBySession={pendingBySession}
              focusedSessionId={focusedForkId ?? currentTree?.id}
              onSetForkFocus={handleSetForkFocus}
              onCloseFork={handleCloseFork}
              onReopenFork={handleReopenFork}
              onDeleteFork={handleDeleteSession}
              runStateBySession={runStateBySession}
              onForkAndSend={handleForkAndSend}
              queuedPrompt={queuedPrompt}
              onPromoteQueued={() => handlePromoteQueued("interrupt")}
              onSteerQueued={
                currentSessionCanSteer
                  ? () => handlePromoteQueued("steer")
                  : undefined
              }
              onCancelQueued={handleCancelQueued}
              onQueuedTextEdit={handleQueuedTextEdit}
              onReviewLastWork={
                builtinExtensions.supervisor &&
                currentSession?.supervisor_enabled &&
                (currentSession.messages?.length ?? 0) > 0
                  ? () => {
                      void progressTrackedFetch(
                        `session:supervisorReview:${currentSession.id}`,
                        `${supervisorApi()}/sessions/${currentSession.id}/review-last-work`,
                        { method: "POST" },
                      );
                    }
                  : undefined
              }
              sendTarget={
                builtinExtensions.supervisor && currentSession?.supervisor_enabled
                  ? sendTarget
                  : undefined
              }
              onSendTargetChange={
                builtinExtensions.supervisor && currentSession?.supervisor_enabled
                  ? setSendTarget
                  : undefined
              }
              onLoadOlderMessages={
                loadOlderMessages
                  ? (sessionId, beforeSeq) => loadOlderMessages(sessionId, beforeSeq)
                  : undefined
              }
              hasOlderMessages={currentSession?.pagination?.has_older}
              sessionLoading={sessionLoading}
              onAddNote={
                currentSession
                  ? (text) => handleAddNote(currentSession.id, text)
                  : undefined
              }
              onQueuedToNote={
                currentSession
                  ? (text) => {
                      handleAddNote(currentSession.id, text);
                      handleCancelQueued();
                    }
                  : undefined
              }
              onShowNotes={() => openRightPanelWithTab("notes")}
              onShowComments={() => openRightPanelWithTab("comments")}
              toolbarActionsNode={
                <>
                  <ExtensionQuickButtons context={hookActionContext} variant="toolbar" />
                  {builtinExtensions.ask && !isAskView && !isMobile
                    ? sessionToolbarModules.map((module) => (
                        <ExtensionModuleSlot
                          key={`${module.extension_id}:${module.id}`}
                          module={module}
                          className="extension-module-slot--toolbar"
                          context={askExtensionContext}
                        />
                      ))
                    : null}
                </>
              }
              onToggleRightPanel={handleToggleRightPanel}
              rightPanelOpen={rightPanelVisible}
              shortcutResponses={shortcutResponses}
              projects={projects}
              sessions={sessions}
              currentNodeId={selectedProjectNodeId}
              machines={machines}
            />
            </ConfigPanelContext.Provider>
          );

          if (!promptEngState && !fileEditingState) {
            return (
              <>
                {supervisorBannerElement}
                {chatElement}
                {prToastElement}
              </>
            );
          }

          // ── File editing overlay ─────────────────────────────────
          if (fileEditingState) {
            return (
              <FileEditorOverlay
                state={fileEditingState}
                persistent={fileEditingPersistent}
                onDone={handleFileEditorDone}
                onCancel={handleFileEditorCancel}
                chatSlot={chatElement}
                fileViewerSlot={
                  <Suspense fallback={<LazySurfaceFallback />}>
                    <MultiFileEditor
                      filePaths={fileEditingState.filePaths}
                      originalContents={fileEditingState.originalContents}
                      fileDiscussions={fileEditingState.fileDiscussions}
                      sessionMessages={fullMessages}
                      pendingTagCount={
                        (currentSession?.inline_tags ?? []).filter(
                          (t) => t.fileAnchor,
                        ).length
                      }
                      onSubmitComment={async (anchor: FileAnchorComment) => {
                        await handleAddFileAnchoredTag({
                          filePath: anchor.filePath,
                          startLine: anchor.startLine,
                          endLine: anchor.endLine,
                          startCol: anchor.startCol,
                          endCol: anchor.endCol,
                          comment: anchor.comment,
                        });
                      }}
                      onStartDiscussion={handleStartFileDiscussion}
                      onPatchDiscussion={handlePatchFileDiscussion}
                      onSendDiscussionMessage={handleSendFileDiscussionMessage}
                    />
                  </Suspense>
                }
              />
            );
          }

          const onPromptEngineerSend = async () => {
            const engId = promptEngState!.engSessionId;
            const parentId = promptEngState!.parentSessionId;
            let content = "";
            try {
              const r = await progressTrackPromise(
                `promptEng:fetchResult:${engId}`,
                () => fetch(`${extBackendBase("promptEngineer")}/sessions/${engId}/prompt-eng-result`),
              ).promise;
              if (!r.ok) {
                throw new Error(
                  (await r.text()) || `result fetch failed (${r.status})`
                );
              }
              const data = (await r.json()) as { content?: string };
              content = (data.content ?? "").trim();
            } catch (e) {
              alert(t("app.readPromptFailed") + (e instanceof Error ? e.message : String(e)));
            }
            if (!content) {
              alert(t("app.refinedPromptEmpty"));
              return;
            }
            type PromptEngineerParentNode = {
              id?: string;
              model?: string;
              cwd?: string;
              orchestration_mode?: OrchestrationMode;
              forks?: PromptEngineerParentNode[];
            };
            let parentRecord: PromptEngineerParentNode | null = null;
            try {
              const pr = await progressTrackPromise(
                `session:fetch:${parentId}`,
                () => fetch(`${API}/api/sessions/${parentId}`),
              ).promise;
              if (pr.ok) {
                const tree = (await pr.json()) as PromptEngineerParentNode | null;
                const findNode = (node: PromptEngineerParentNode | null): PromptEngineerParentNode | null => {
                  if (!node) return null;
                  if (node.id === parentId) return node;
                  for (const f of node.forks ?? []) {
                    const hit = findNode(f);
                    if (hit) return hit;
                  }
                  return null;
                };
                parentRecord = findNode(tree) ?? tree;
              }
            } catch {
              // Parent selectors fall back to the loaded session list below.
            }
            const fallback = sessions.find((s) => s.id === parentId);
            const sendModel = parentRecord?.model || fallback?.model || model;
            const sendCwd = parentRecord?.cwd || fallback?.cwd || cwd || "";
            const sendOrch =
              parentRecord?.orchestration_mode ??
              (fallback?.orchestration_mode as OrchestrationMode | undefined);
            await selectSession(parentId);
            sendMessage(
              content,
              sendModel,
              sendCwd,
              null,
              parentId,
              undefined,
              sendOrch,
              clientId
            );
            try {
              await progressTrackedFetch(
                `promptEng:cancel:${engId}`,
                `${extBackendBase("promptEngineer")}/sessions/${engId}/prompt-engineer`,
                { method: "DELETE" },
              );
            } catch {
              // The cleanup is idempotent and the session may already be gone.
            }
            refreshSessions();
          };

          const onPromptEngineerCancel = async () => {
            const engId = promptEngState!.engSessionId;
            const parentId = promptEngState!.parentSessionId;
            try {
              await progressTrackedFetch(
                `promptEng:cancel:${engId}`,
                `${extBackendBase("promptEngineer")}/sessions/${engId}/prompt-engineer`,
                { method: "DELETE" },
              );
            } catch {
              // The cleanup is idempotent and the session may already be gone.
            }
            await selectSession(parentId);
            refreshSessions();
          };

          const promptEngineerFileViewerSlot = (
            <Suspense fallback={<LazySurfaceFallback />}>
              <FileEditor
                tempFilePath={promptEngState!.tempFilePath}
                originalContent={promptEngState!.originalContent}
                pendingTagCount={
                  (currentSession?.inline_tags ?? []).filter(
                    (tag) => tag.fileAnchor,
                  ).length
                }
                onSubmitComment={async (anchor: FileAnchorComment) => {
                  await handleAddFileAnchoredTag({
                    filePath: anchor.filePath,
                    startLine: anchor.startLine,
                    endLine: anchor.endLine,
                    startCol: anchor.startCol,
                    endCol: anchor.endCol,
                    comment: anchor.comment,
                  });
                }}
              />
            </Suspense>
          );

          return (
            <>
              {sessionWorkspaceOverlayModules.map((module) => (
                <ExtensionModuleSlot
                  key={`${module.extension_id}:${module.id}`}
                  module={module}
                  context={{
                    activeOverlay: "prompt-engineer",
                    state: promptEngState,
                    parentSessionName:
                      sessions.find(
                        (s) => s.id === promptEngState!.parentSessionId
                      )?.name ?? null,
                    onSend: onPromptEngineerSend,
                    onCancel: onPromptEngineerCancel,
                    chatSlot: chatElement,
                    fileViewerSlot: promptEngineerFileViewerSlot,
                  }}
                />
              ))}
            </>
          );
        })()}
      </div>

      {/* Right Panel — desktop: always in the flex layout (collapsed
          via CSS when hidden) to prevent main-panel width reflow.
          Mobile: overlay drawer, conditional render is fine. */}
      {!isMobile && (
        <div
          className="right-panel-resizer"
          style={!rightPanelVisible ? { display: "none" } : undefined}
          onMouseDown={rightPanel.onMouseDown}
        />
      )}
      {isMobile && isPortrait && rightPanelVisible && !mobileRightFullscreen && (
        <div
          className="mobile-right-panel-resizer"
          onPointerDown={mobileRightPanel.onPointerDown}
          role="separator"
          aria-orientation="horizontal"
        />
      )}
      <div
        className={
          "right-panel" +
          (isMobile && mobileRightOpen ? " mobile-drawer-open" : "") +
          (isMobile && mobileRightFullscreen ? " mobile-fullscreen" : "") +
          (!isMobile && !rightPanelVisible ? " right-panel-collapsed" : "")
        }
        style={
          isMobile
            ? (rightPanelVisible ? mobileRightPanelStyle : { display: "none" })
            : rightPanelVisible
              ? rightPanelStyle
              : { width: 0, minWidth: 0 }
        }
        role={isMobile ? "dialog" : undefined}
        aria-modal={isMobile && mobileRightOpen ? true : undefined}
        aria-label={isMobile ? t("app.filesDrawerLabel") : undefined}
        aria-hidden={isMobile && !mobileRightOpen ? true : undefined}
      >
        {rightPanelVisible && (
          <>
            {isMobile && (
              <>
                <button
                  className="right-panel-fullscreen-toggle"
                  onClick={() => setMobileRightFullscreen((v) => !v)}
                  aria-label={
                    mobileRightFullscreen
                      ? t("app.restoreFilesPanel")
                      : t("app.fullscreenFilesPanel")
                  }
                  title={
                    mobileRightFullscreen
                      ? t("app.restoreFilesPanel")
                      : t("app.fullscreenFilesPanel")
                  }
                  aria-pressed={mobileRightFullscreen}
                >
                  <Icon name={mobileRightFullscreen ? "chevron-down" : "expand"} size={18} />
                </button>
                <button
                  className="right-panel-close"
                  onClick={closeMobileRightPanel}
                  aria-label={t("app.closeFiles")}
                  title={t("app.closeFiles")}
                >
                  <Icon name="x" size={18} />
                </button>
              </>
            )}
            <div className="right-panel-tabs">
              <button
                className={`right-panel-tab ${rightPanelTab === "files" ? "active" : ""}`}
                onClick={() => {
                  setRightPanelTab("files");
                  if (currentSession && !isMobile)
                    patchRightPanel(currentSession.id, { tab: "files", clearAutoReasons: true });
                }}
              >
                {(currentSession?.open_file_panels?.length ?? 0) > 0
                  ? `${t("rightPanel.files")} (${currentSession?.open_file_panels?.length})`
                  : t("rightPanel.files")}
              </button>
              <button
                className={`right-panel-tab ${rightPanelTab === "todos" ? "active" : ""}`}
                onClick={() => {
                  setRightPanelTab("todos");
                  if (currentSession && !isMobile)
                    patchRightPanel(currentSession.id, { tab: "todos", clearAutoReasons: true });
                }}
              >
                {visibleTodoCount(currentSession?.current_todos ?? []) +
                  visibleTodoCount(currentSession?.current_tasks ?? []) >
                0
                  ? `${t("rightPanel.todos")} (${
                      visibleTodoCount(currentSession?.current_todos ?? []) +
                      visibleTodoCount(currentSession?.current_tasks ?? [])
                    })`
                  : t("rightPanel.todos")}
              </button>
              <button
                className={`right-panel-tab ${rightPanelTab === "notes" ? "active" : ""}`}
                onClick={() => {
                  setRightPanelTab("notes");
                  if (currentSession && !isMobile)
                    patchRightPanel(currentSession.id, { tab: "notes", clearAutoReasons: true });
                }}
              >
                {(currentSession?.notes?.length ?? 0) > 0
                  ? `${t("rightPanel.notes")} (${currentSession?.notes?.length})`
                  : t("rightPanel.notes")}
              </button>
              {builtinExtensions.canvas && (
                <button
                  className={`right-panel-tab ${rightPanelTab === "canvas" ? "active" : ""}`}
                  onClick={() => {
                    setRightPanelTab("canvas");
                    if (currentSession && !isMobile)
                      patchRightPanel(currentSession.id, { tab: "canvas", clearAutoReasons: true });
                  }}
                >
                  {t("rightPanel.canvas")}
                </button>
              )}
              {builtinExtensions.testape && screenPanelModules.length > 0 && (
                <button
                  className={`right-panel-tab ${rightPanelTab === "screen" ? "active" : ""}`}
                  onClick={() => {
                    setRightPanelTab("screen");
                    if (currentSession && !isMobile)
                      patchRightPanel(currentSession.id, { tab: "screen", clearAutoReasons: true });
                  }}
                >
                  {t("rightPanel.screen", "Screen")}
                </button>
              )}
              <button
                className={`right-panel-tab ${rightPanelTab === "comments" ? "active" : ""}`}
                onClick={() => {
                  setRightPanelTab("comments");
                  if (currentSession && !isMobile)
                    patchRightPanel(currentSession.id, { tab: "comments", clearAutoReasons: true });
                }}
              >
                {tags.length > 0
                  ? `${t("rightPanel.comments")} (${tags.length})`
                  : t("rightPanel.comments")}
              </button>
            </div>
            {rightPanelTab === "comments" ? (
              <CommentsPanel
                tags={tags}
                onRemove={handleRemoveTag}
                onUpdate={handleUpdateTag}
                focusedCommentId={focusedCommentId}
                onFocusComment={handleFocusComment}
                autoEditId={autoEditId}
                onAutoEditConsumed={() => setAutoEditId(null)}
              />
            ) : builtinExtensions.canvas && rightPanelTab === "canvas" ? (
              currentSession ? (
                canvasPanelModules.map((module) => (
                  <ExtensionModuleSlot
                    key={`${module.extension_id}:${module.id}`}
                    module={module}
                    context={{ sessionId: currentSession.id }}
                  />
                ))
              ) : (
                <div className="canvas-panel-loading">{t("rightPanel.selectASession")}</div>
              )
            ) : builtinExtensions.testape && rightPanelTab === "screen" ? (
              currentSession ? (
                screenPanelModules.map((module) => (
                  <ExtensionModuleSlot
                    key={`${module.extension_id}:${module.id}`}
                    module={module}
                    context={{ sessionId: currentSession.id }}
                  />
                ))
              ) : (
                <div className="canvas-panel-loading">{t("rightPanel.selectASession")}</div>
              )
            ) : rightPanelTab === "todos" ? (
              <TodosPanel
                todos={currentSession?.current_todos ?? []}
                tasks={currentSession?.current_tasks ?? []}
              />
            ) : rightPanelTab === "notes" ? (
              currentSession ? (
                <NotesPanel
                  notes={currentSession.notes ?? []}
                  onRemove={(noteId) => handleRemoveNote(currentSession.id, noteId)}
                  onEdit={(noteId, text) => handleUpdateNote(currentSession.id, noteId, text)}
                  onSendToPrompt={handleSendNoteToPrompt}
                />
              ) : (
                <div className="canvas-panel-loading">{t("rightPanel.selectASession")}</div>
              )
            ) : viewingFile ? (
              /* Transient before/after diff view (handleViewDiff) —
                 NOT a backend-owned panel, by design. */
              <Suspense fallback={<LazySurfaceFallback />}>
                <FileViewer
                  filePath={viewingFile.path}
                  diffBefore={viewingFile.diffBefore}
                  diffAfter={viewingFile.diffAfter}
                  focus={viewingFile.focus}
                  nodeId={currentSession?.node_id ?? "primary"}
                  onClose={() => setViewingFile(null)}
                  onAddFileTag={handleAddFileAnchoredTag}
                  onStartDiscussion={
                    fileEditingState ? handleStartFileDiscussion : undefined
                  }
                  pendingTagCount={
                    (currentSession?.inline_tags ?? []).filter(
                      (t) => t.fileAnchor?.filePath === viewingFile.path,
                    ).length
                  }
                />
              </Suspense>
            ) : (
              <Suspense fallback={<LazySurfaceFallback />}>
                <ConfigPanels
                  panels={currentSession?.open_config_panels ?? []}
                  client={providerConfigSyncClient}
                  subscribeExternalChanges={(cb) => {
                    const offProvider = eventBus.subscribe("provider_config_sync_changed", () => cb());
                    const offExtensions = eventBus.subscribe("extensions_changed", () => cb());
                    return () => {
                      offProvider();
                      offExtensions();
                    };
                  }}
                  onClosePanel={handleCloseConfigPanel}
                />
                <FilePanels
                  panels={currentSession?.open_file_panels ?? []}
                  nodeId={currentSession?.node_id ?? "primary"}
                  onClosePanel={handleCloseFilePanel}
                  registerEditor={registerEditor}
                  onAddFileTag={handleAddFileAnchoredTag}
                  onStartDiscussion={
                    fileEditingState ? handleStartFileDiscussion : undefined
                  }
                  pendingTagCountFor={(path) =>
                    (currentSession?.inline_tags ?? []).filter(
                      (tg) => tg.fileAnchor?.filePath === path,
                    ).length
                  }
                />
              </Suspense>
            )}
          </>
        )}
      </div>
    </div>
    )}

      {/* Modals live OUTSIDE the route branch so they're reachable
          from both Home and SessionView (e.g. NewSessionModal opens
          via the Home "+ New session" CTA AND via the in-session
          "New" button). */}
      {newSessionModalOpen && (
        <Suspense fallback={<LazySurfaceFallback />}>
          <NewSessionModal
            open={newSessionModalOpen}
            onClose={() => {
              setNewSessionModalOpen(false);
              setInvestigationCtx(undefined);
              setAskProposedProjectPath(undefined);
              setAskProposedProjectNodeId(undefined);
            }}
            onCreate={handleCreateSessionFromModal}
            defaultCwd={selectedProjectPath || cwd}
            projects={projects}
            initialProjectPath={askProposedProjectPath}
            initialNodeId={askProposedProjectNodeId}
            investigation={investigationCtx}
            capabilityPickerClient={providerConfigSyncClient}
            teamEnabled={builtinExtensions.team}
            machineNodesEnabled={builtinExtensions.machineNodes}
            browserHarnessEnabled={builtinExtensions.browserHarness}
          />
        </Suspense>
      )}
      {turnCapabilityPickerOpen && currentSession && builtinExtensions.providerConfigSync && (
        <div className="modal-overlay capability-picker-overlay" onClick={() => setTurnCapabilityPickerOpen(false)}>
          <div className="modal-content capability-picker-modal" onClick={(e) => e.stopPropagation()}>
            <Suspense fallback={<LazySurfaceFallback />}>
              <ProviderCapabilityPicker
                open
                cwd={currentSession.cwd || selectedProjectPath || cwd}
                client={providerConfigSyncClient}
                onClose={() => setTurnCapabilityPickerOpen(false)}
                onSelect={(source, output) => {
                  const next = capabilityContextFromPickerSource(source, output);
                  if (next.outputs.length === 0) return;
                  setTurnCapabilityContextsBySession((prev) => ({
                    ...prev,
                    [currentSession.id]: [
                      next,
                      ...(prev[currentSession.id] ?? []).filter(
                        (item) => item.source_id !== next.source_id,
                      ),
                    ],
                  }));
                  setTurnCapabilityPickerOpen(false);
                }}
              />
            </Suspense>
          </div>
        </div>
      )}
      {projectSettingsCwd && (
        <ProjectSettings
          cwd={projectSettingsCwd}
          onFileClick={(path) => {
            setProjectSettingsCwd(null);
            handleOpenFilePanel(path);
          }}
          onEngineerFile={async (path, _content) => {
            setProjectSettingsCwd(null);
            await startFileEditor(path);
          }}
          onClose={() => setProjectSettingsCwd(null)}
        />
      )}
      {dirPickerOpen && (
        <DirPickerModal
          open={dirPickerOpen}
          initialPath={cwd}
          initialNodeId={selectedProjectNodeId}
          onCancel={() => setDirPickerOpen(false)}
          onPick={handleAddProject}
        />
      )}
      {cwd && (
        <FileChooserModal
          open={fileChooserOpen}
          cwd={cwd}
          nodeId={currentSession?.node_id ?? selectedProjectNodeId}
          onFileClick={handleFileClick}
          onEngineerFile={startFileEditor}
          onClose={() => setFileChooserOpen(false)}
        />
      )}
      {promptEngModalDraft !== null &&
        sessionActionModalModules.map((module) => (
          <ExtensionModuleSlot
            key={`${module.extension_id}:${module.id}`}
            module={module}
            context={{
              activeModal: "prompt-engineer-start",
              open: promptEngModalDraft !== null,
              parentName: currentSession?.name ?? "",
              parentHasClaudeSid: !!(
                currentSession?.manager_agent_session_id ||
                currentSession?.native_agent_session_id
              ),
              onCancel: () => {
                setPromptEngModalDraft(null);
                setPromptEngStartError("");
              },
              onPick: async (mode: "fork" | "new") => {
                if (!currentSession || promptEngModalDraft === null) return;
                setPromptEngStartError("");
                try {
                  const parentId = currentSession.id;
                  const handle = progressTrackPromise(
                    `promptEng:start:${parentId}`,
                    async () => {
                      const r = await fetch(
                        `${extBackendBase("promptEngineer")}/sessions/${parentId}/prompt-engineer`,
                        {
                          method: "POST",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({
                            draft: promptEngModalDraft,
                            mode,
                            client_id: clientId,
                          }),
                        },
                      );
                      if (!r.ok) {
                        const text = await r.text();
                        throw new Error(text || `start failed (${r.status})`);
                      }
                      return (await r.json()) as {
                        eng_session_id: string;
                        resumed?: boolean;
                      };
                    },
                  );
                  const data = await handle.promise;
                  const engSid = data.eng_session_id;
                  handle.armWSExtender(makeSessionExtender(engSid, "turn_complete"));
                  await selectSession(engSid);
                  setPromptEngModalDraft(null);
                  refreshSessions();
                } catch (e) {
                  setPromptEngStartError(
                    e instanceof Error ? e.message : "start failed"
                  );
                }
              },
            }}
          />
        ))}
      <BypassPermissionDialog
        open={bypassPermPending !== null}
        onSendAnyway={confirmBypassAndSend}
        onChangeInSettings={bypassGoToSettings}
        onDismiss={dismissBypassPending}
      />
      {sessionToDelete && (
        <ConfirmModal
          open={!!sessionToDelete}
          title={t("session.deleteTitle")}
          message={t("app.deleteSessionConfirm", { name: sessionBeingDeleted?.name || t("fork.fork") })}
          onConfirm={confirmDeleteSession}
          onCancel={() => setSessionToDelete(null)}
        />
      )}
      {projectSuggestion && (
        <ProjectSuggestionModal
          suggestion={projectSuggestion.suggestion}
          currentName={projectNameForCwd(cwd || currentSession?.cwd || "")}
          targetName={projectNameForCwd(projectSuggestion.suggestion.target_cwd)}
          onMove={() => projectSuggestion.resolve("move")}
          onSendHere={() => projectSuggestion.resolve("here")}
          onCancel={() => projectSuggestion.resolve("cancel")}
        />
      )}
      {refreshModalOpen && (
        <div className="modal-overlay" onClick={() => setRefreshModalOpen(false)}>
          <div
            className="modal-content refresh-choice-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="refresh-choice-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-header">
              <h2 id="refresh-choice-title">{t("app.refreshModalTitle")}</h2>
              <button
                className="modal-close"
                onClick={() => setRefreshModalOpen(false)}
                aria-label={t("startup_tasks.dismiss")}
              >
                &times;
              </button>
            </div>
            <div className="modal-body refresh-choice-body">
              <button
                type="button"
                className="refresh-choice-option"
                onClick={() => void handleRefreshApp("now")}
              >
                <span className="refresh-choice-icon">
                  <Icon name="refresh" size={18} />
                </span>
                <span>
                  <strong>{t("app.refreshNowTitle")}</strong>
                  <small>{t("app.refreshNowDescription")}</small>
                </span>
              </button>
              <button
                type="button"
                className="refresh-choice-option"
                onClick={() => void handleRefreshApp("idle")}
              >
                <span className="refresh-choice-icon">
                  <Icon name="clock" size={18} />
                </span>
                <span>
                  <strong>{t("app.refreshIdleTitle")}</strong>
                  <small>{t("app.refreshIdleDescription")}</small>
                </span>
              </button>
            </div>
            <div className="modal-footer">
              <button className="btn-secondary" onClick={() => setRefreshModalOpen(false)}>
                {t("app.cancel")}
              </button>
            </div>
          </div>
        </div>
      )}
      {detailsSessionId && (
        <SessionDetailsPanel
          open={!!detailsSessionId}
          sessionId={detailsSessionId}
          onClose={() => setDetailsSessionId(null)}
        />
      )}
      {supervisorPromptModalOpen && (
        sessionActionModalModules.map((module) => (
          <ExtensionModuleSlot
            key={`${module.extension_id}:${module.id}`}
            module={module}
            context={{
              activeModal: "supervisor-prompt",
              open: supervisorPromptModalOpen,
              mode: supervisorPromptModalMode,
              defaultPrompt: currentSession?.supervisor_custom_prompt ?? "",
              onConfirm: (prompt: string) => {
                if (!currentSession) return;
                setSupervisorPromptModalOpen(false);
                applySessionMetadata(currentSession.id, {
                  supervisor_enabled: true,
                  supervisor_custom_prompt: prompt,
                });
                void progressTrackedFetch(
                  `session:supervisorToggle:${currentSession.id}`,
                  `${supervisorApi()}/sessions/${currentSession.id}/supervisor-toggle`,
                  {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ enabled: true, custom_prompt: prompt }),
                  },
                );
              },
              onCancel: () => setSupervisorPromptModalOpen(false),
            }}
          />
        ))
      )}
      {promptEngStartError && (
        <div
          style={{
            position: "fixed",
            bottom: 16,
            insetInlineEnd: 16,
            padding: "8px 12px",
            background: "rgba(255,107,107,0.15)",
            color: "#ff8888",
            border: "1px solid rgba(255,107,107,0.4)",
            borderRadius: 4,
            fontSize: scaledFontSize(12),
            // Above the modal-overlay (z-index: 1000) so the user can
            // actually read why the start failed without dismissing.
            zIndex: 2000,
            maxWidth: 360,
          }}
        >
          {t("app.engineerStartFailed")}{promptEngStartError}
          <button
            onClick={() => setPromptEngStartError("")}
            style={{
              marginInlineStart: 8,
              background: "transparent",
              border: "none",
              color: "inherit",
              cursor: "pointer",
            }}
          >
            ×
          </button>
        </div>
      )}
      <RefreshResult />
    </>
    </InvestigateContextMenu>
    </MobileActionSheetProvider>
  );
}
