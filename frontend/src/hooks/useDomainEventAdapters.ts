import { useEffect, useRef } from "react";

import { eventBus, type BusEventMap } from "../lib/eventBus";

type SessionInventoryHandlers = {
  onCreated: (session: BusEventMap["session_created"]["session"]) => void;
  onDeleted: (sessionId: string) => void;
  onRenamed: (sessionId: string, name: string) => void;
  onForked: (
    session: BusEventMap["session_forked"]["session"],
    parentSessionId: string | null,
  ) => void;
};

type ProjectInventoryHandlers = {
  onProjectsChanged: () => void;
  onProjectUpdatesChanged: (
    projectId: string,
    unseenCount: number,
  ) => void;
  onWorkersChanged: () => void;
  onSessionOrganizationChanged: () => void;
  onProjectMappingsChanged: () => void;
};

function useLatest<T>(value: T) {
  const ref = useRef(value);
  useEffect(() => {
    ref.current = value;
  }, [value]);
  return ref;
}

export function useSessionInventoryEvents(handlers: SessionInventoryHandlers) {
  const handlersRef = useLatest(handlers);

  useEffect(() => {
    const offCreated = eventBus.subscribe("session_created", ({ session }) => {
      handlersRef.current.onCreated(session);
    });
    const offDeleted = eventBus.subscribe("session_deleted", ({ session_id }) => {
      handlersRef.current.onDeleted(session_id);
    });
    const offRenamed = eventBus.subscribe("session_renamed", ({ session_id, name }) => {
      handlersRef.current.onRenamed(session_id, name);
    });
    const offForked = eventBus.subscribe(
      "session_forked",
      ({ session, parent_session_id }) => {
        handlersRef.current.onForked(session, parent_session_id);
      },
    );

    return () => {
      offCreated();
      offDeleted();
      offRenamed();
      offForked();
    };
  }, [handlersRef]);
}

export function useProjectInventoryEvents(handlers: ProjectInventoryHandlers) {
  const handlersRef = useLatest(handlers);

  useEffect(() => {
    const offProjects = eventBus.subscribe("projects_changed", () => {
      handlersRef.current.onProjectsChanged();
    });
    const offUpdates = eventBus.subscribe(
      "project_updates_changed",
      ({ project_id, unseen_count }) => {
        handlersRef.current.onProjectUpdatesChanged(project_id, unseen_count);
      },
    );
    const offWorkers = eventBus.subscribe("workers_changed", () => {
      handlersRef.current.onWorkersChanged();
    });
    const offOrganization = eventBus.subscribe("session_organization_changed", () => {
      handlersRef.current.onSessionOrganizationChanged();
    });
    const offMappings = eventBus.subscribe("project_mappings_changed", () => {
      handlersRef.current.onProjectMappingsChanged();
    });

    return () => {
      offProjects();
      offUpdates();
      offWorkers();
      offOrganization();
      offMappings();
    };
  }, [handlersRef]);
}
