// Top-level native render surface — the `ba.surface_native` replacement
// for the chat content region (Chat.tsx's turn-group ternary), self-
// contained: owns its own loading skeleton and "load older" chrome so the
// integration point in Chat.tsx stays a single branch. ForkSplitView is
// explicitly out of scope for stage 1 (see components/Chat.tsx's call
// site) — this component only ever renders a single (non-forked) surface.

import { useTranslation } from "react-i18next";
import { useSurfaceStore } from "./useSurfaceStore";
import { TurnView } from "./TurnView";
import { InstructionWidgetView } from "./nodes/Misc";

export function ChatSurfaceView({ sessionId }: { sessionId: string }) {
  const { t } = useTranslation();
  const { store, snapshot } = useSurfaceStore(sessionId);

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
          ) : (
            <button className="load-older-link" onClick={() => void store.loadOlder()}>
              {t("chat.loadOlderMessages")}
            </button>
          )}
        </div>
      )}

      {snapshot.turnOrder.map((turnId) => {
        const entry = snapshot.turnsById.get(turnId);
        if (!entry) return null;
        return <TurnView key={turnId} entry={entry} store={store} runsById={snapshot.runsById} />;
      })}
    </div>
  );
}
