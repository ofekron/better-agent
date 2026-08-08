// Top-level native render surface — the `ba.surface_native` replacement
// for the chat content region (Chat.tsx's turn-group ternary), self-
// contained: owns its own loading skeleton and "load older" chrome so the
// integration point in Chat.tsx stays a single branch. ForkSplitView is
// explicitly out of scope for stage 1 (see components/Chat.tsx's call
// site) — this component only ever renders a single (non-forked) surface.

import { useSyncExternalStore } from "react";
import { useTranslation } from "react-i18next";
import { useSurfaceStore } from "./useSurfaceStore";
import type { SurfaceStore } from "./state";
import { TurnView } from "./TurnView";
import { InstructionWidgetView } from "./nodes/Misc";

export function ChatSurfaceView({
  sessionId,
  store: providedStore,
  userDisplayName,
}: {
  sessionId: string;
  /** When supplied, this EXACT instance is used (via useSyncExternalStore
   * below) instead of this component creating its own second SurfaceStore
   * for the same session — Chat.tsx's composer needs to send() through the
   * same store this view renders from, so an optimistic send lands in the
   * same tree instead of an invisible sibling store nobody reads. Omit for
   * a read-only mount with no composer of its own (ForkSplitView's panes),
   * which keeps this component fully self-contained as before. */
  store?: SurfaceStore | null;
  userDisplayName?: string | null;
}) {
  const { t } = useTranslation();
  const owned = useSurfaceStore(providedStore === undefined ? sessionId : null);
  const store = providedStore !== undefined ? providedStore : owned.store;
  const snapshot = useSyncExternalStore(
    store?.subscribe ?? (() => () => {}),
    store?.getSnapshot ?? (() => null),
    store?.getSnapshot ?? (() => null),
  );

  if (!store || !snapshot || !snapshot.hydrated) {
    return (
      <div className="chat-loading-skeleton" data-testid="surface-loading-skeleton">
        <div className="chat-loading-pulse" />
      </div>
    );
  }

  return (
    <div data-testid="surface-chat-view">
      {snapshot.instructionWidget && (
        <InstructionWidgetView payload={snapshot.instructionWidget.payload as import("../adapter/wire").InstructionWidgetPayloadWire} />
      )}

      {snapshot.olderCursor && (
        <div className="load-older-wrapper">
          {snapshot.loadingOlder ? (
            <div className="load-older-spinner">
              {t("chat.loadingOlderMessages")}
              <span className="load-older-dots" aria-hidden="true">
                <i /><i /><i />
              </span>
            </div>
          ) : snapshot.olderError ? (
            <div className="load-older-error" role="alert">
              <span>{t("chat.loadOlderFailed")}</span>
              <button className="load-older-link" onClick={() => void store.loadOlder()}>
                {t("chat.sessionLoadRetry")}
              </button>
            </div>
          ) : (
            <button className="load-older-link" onClick={() => void store.loadOlder()}>
              {t("chat.loadOlderMessages")}
            </button>
          )}
        </div>
      )}

      {snapshot.turnOrder.map((turnId, index) => {
        const entry = snapshot.turnsById.get(turnId);
        if (!entry) return null;
        return (
          <TurnView
            key={turnId}
            entry={entry}
            store={store}
            runsById={snapshot.runsById}
            userDisplayName={userDisplayName}
            isLatestTurn={index === snapshot.turnOrder.length - 1}
          />
        );
      })}
    </div>
  );
}
