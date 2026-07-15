export type FrontendBackendSyncBatch = {
  id: number;
  area: string;
  files: readonly string[];
  mutations: readonly string[];
};

export const frontendBackendSyncScope = {
  include: [
    "Every user-triggered frontend mutation of backend-owned state",
    "Every optimistic frontend projection awaiting REST, WebSocket, or snapshot confirmation",
    "Every mutation failure that must reconcile to prior or authoritative backend state",
  ],
  exclude: [
    "Read-only requests",
    "Telemetry and frontend failure-log delivery",
    "Authentication transport and logout navigation",
    "Idempotent ensure, refresh, health, and background discovery requests without optimistic state",
    "Offline action transport already governed by durable backlog acknowledgement semantics",
  ],
} as const;

export const frontendBackendSyncBatches: readonly FrontendBackendSyncBatch[] = [
  {
    id: 1,
    area: "Canonical three-state sync infrastructure and failure UX",
    files: [
      "frontend/src/progress/store.ts",
      "frontend/src/lib/frontendLogger.ts",
      "frontend/src/sync/**",
      "frontend/src/components/SyncFailureToast.tsx",
      "frontend/src/components/SyncStatusIndicator.tsx",
      "frontend/src/App.css",
      "frontend/src/i18n/*.json",
      "frontend/tests/sync/**",
    ],
    mutations: [
      "pending after frontend action until authoritative confirmation",
      "confirm by explicit acknowledgement or expected backend snapshot/event predicate",
      "fail with prior/backend reconciliation, detailed toast, frontend log, and backend log delivery",
    ],
  },
  {
    id: 2,
    area: "Session identity, organization, navigation, and metadata",
    files: [
      "frontend/src/App.tsx",
      "frontend/src/api.ts",
      "frontend/src/components/SessionList.tsx",
      "frontend/src/components/SessionSelectorControls.tsx",
      "frontend/src/components/SessionTabsSettings.tsx",
      "frontend/src/sessionFolders.ts",
    ],
    mutations: [
      "rename/delete/fork/pin sessions",
      "session tags, folders, notes, and organization",
      "right-panel and session metadata persisted to backend",
    ],
  },
  {
    id: 3,
    area: "Session selectors, runtime controls, prompts, and approvals",
    files: [
      "frontend/src/App.tsx",
      "frontend/src/components/Chat.tsx",
      "frontend/src/components/ModelSelector.tsx",
      "frontend/src/hooks/useSession.ts",
      "frontend/src/utils/preSendAdvisory.ts",
      "frontend/src/utils/writeBacklog.ts",
    ],
    mutations: [
      "model/provider/reasoning/cwd/orchestration selectors",
      "stop, retry, rewind, continue, send, queue, and approval decisions",
      "worker creation policy and session runtime state controls",
    ],
  },
  {
    id: 4,
    area: "Projects, files, editor, discussions, and UI selection",
    files: [
      "frontend/src/App.tsx",
      "frontend/src/components/FileEditor.tsx",
      "frontend/src/components/FileTree.tsx",
      "frontend/src/components/FileViewer.tsx",
      "frontend/src/components/ProjectGitStatus.tsx",
      "frontend/src/components/ProjectSettings.tsx",
      "frontend/src/components/DirPickerModal.tsx",
      "frontend/src/utils/uiSelection.ts",
    ],
    mutations: [
      "project add/remove/touch/mapping changes",
      "file writes, drafts, editor lifecycle, panels, and discussions",
      "backend-owned UI selection changes",
    ],
  },
  {
    id: 5,
    area: "Settings and preferences",
    files: [
      "frontend/src/components/SettingsPage.tsx",
      "frontend/src/components/*Setting.tsx",
      "frontend/src/components/LanguageSelector.tsx",
      "frontend/src/components/ShortcutResponses.tsx",
      "frontend/src/components/ShortcutSettings.tsx",
      "frontend/src/components/VoiceSettings.tsx",
      "frontend/src/components/UserDisplayNameSetting.tsx",
    ],
    mutations: [
      "user preferences and appearance",
      "provider, model, context, delegation, restart, retention, language, voice, and shortcut settings",
      "credential and password-manager settings",
    ],
  },
  {
    id: 6,
    area: "Machines, nodes, providers, setup, and authentication settings",
    files: [
      "frontend/src/hooks/useMachines.ts",
      "frontend/src/hooks/usePendingNodeRegistrations.ts",
      "frontend/src/hooks/useProviderInstalls.ts",
      "frontend/src/components/Login.tsx",
      "frontend/src/components/Setup.tsx",
      "frontend/src/components/NativeImportSetting.tsx",
      "frontend/src/components/AuthCredentialsSetting.tsx",
    ],
    mutations: [
      "machine/node approve, deny, revoke, restart, and registration",
      "provider install/uninstall/import and setup mutations",
      "persistent authentication configuration changes",
    ],
  },
  {
    id: 7,
    area: "Extensions and provider-config-sync surfaces",
    files: [
      "frontend/src/components/ExtensionSlots.tsx",
      "frontend/src/components/ExtensionUiHooks.tsx",
      "frontend/src/components/ExtensionPaymentModal.tsx",
      "frontend/src/lib/providerConfigSyncRoute.ts",
      "frontend/src/components/ProviderConfigSync*.tsx",
    ],
    mutations: [
      "extension enable/disable/install/update/payment actions",
      "extension-provided backend-owned mutations exposed through core slots",
      "provider-config-sync settings, files, capabilities, apply, and synchronization actions",
    ],
  },
  {
    id: 8,
    area: "Coverage enforcement, integration, and backend failure diagnostics",
    files: [
      "frontend/src/api.ts",
      "frontend/src/sync/frontendBackendMutationCoverage.ts",
      "frontend/tests/sync/frontendBackendMutationCoverage.test.ts",
      "backend/main.py",
      "backend/bff_app_routes.py",
      "backend/scripts/test_*frontend*",
    ],
    mutations: [
      "audit all remaining mutation call sites against this map",
      "enforce mapped/excluded mutation classification in tests",
      "verify backend mutation failures have safe structured logging and correlation metadata",
    ],
  },
] as const;
