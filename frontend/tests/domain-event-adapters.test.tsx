import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  useProjectInventoryEvents,
  useSessionInventoryEvents,
} from "../src/hooks/useDomainEventAdapters";
import { eventBus } from "../src/lib/eventBus";
import type { Session } from "../src/types";

const session = { id: "session-1", name: "Original" } as Session;

describe("domain event adapters", () => {
  it("routes session inventory facts and detaches on unmount", () => {
    const handlers = {
      onCreated: vi.fn(),
      onDeleted: vi.fn(),
      onRenamed: vi.fn(),
      onForked: vi.fn(),
    };
    const { unmount } = renderHook(() => useSessionInventoryEvents(handlers));

    act(() => {
      eventBus.publish("session_created", { session });
      eventBus.publish("session_deleted", { session_id: session.id });
      eventBus.publish("session_renamed", { session_id: session.id, name: "Renamed" });
      eventBus.publish("session_forked", { session, parent_session_id: "parent-1" });
    });

    expect(handlers.onCreated).toHaveBeenCalledWith(session);
    expect(handlers.onDeleted).toHaveBeenCalledWith(session.id);
    expect(handlers.onRenamed).toHaveBeenCalledWith(session.id, "Renamed");
    expect(handlers.onForked).toHaveBeenCalledWith(session, "parent-1");

    unmount();
    eventBus.publish("session_created", { session });
    expect(handlers.onCreated).toHaveBeenCalledTimes(1);
  });

  it("uses fresh project handlers without resubscribing", () => {
    const first = vi.fn();
    const second = vi.fn();
    const stableHandlers = {
      onProjectUpdatesChanged: vi.fn(),
      onWorkersChanged: vi.fn(),
      onSessionOrganizationChanged: vi.fn(),
      onProjectMappingsChanged: vi.fn(),
    };
    const { rerender } = renderHook(
      ({ onProjectsChanged }) => useProjectInventoryEvents({
        ...stableHandlers,
        onProjectsChanged,
      }),
      { initialProps: { onProjectsChanged: first } },
    );

    rerender({ onProjectsChanged: second });
    act(() => {
      eventBus.publish("projects_changed", {});
      eventBus.publish("project_updates_changed", {
        project_id: "project-1",
        unseen_count: 3,
      });
      eventBus.publish("workers_changed", {});
      eventBus.publish("session_organization_changed", {});
      eventBus.publish("project_mappings_changed", {});
    });

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledOnce();
    expect(stableHandlers.onProjectUpdatesChanged).toHaveBeenCalledWith("project-1", 3);
    expect(stableHandlers.onWorkersChanged).toHaveBeenCalledOnce();
    expect(stableHandlers.onSessionOrganizationChanged).toHaveBeenCalledOnce();
    expect(stableHandlers.onProjectMappingsChanged).toHaveBeenCalledOnce();
  });
});
