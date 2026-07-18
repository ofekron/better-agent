import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { eventBus } from "../lib/eventBus";
import type { UserInteractionRequest } from "../types";
import { notifyUserRequest } from "../utils/userInputNotifications";

function pendingRequests(value: unknown): UserInteractionRequest[] {
  if (!Array.isArray(value)) return [];
  return value.filter((request): request is UserInteractionRequest => {
    if (!request || typeof request !== "object") return false;
    const candidate = request as Partial<UserInteractionRequest>;
    return (
      candidate.status === "pending" &&
      typeof candidate.request_id === "string" &&
      typeof candidate.app_session_id === "string" &&
      (candidate.kind === "input" || candidate.kind === "approval")
    );
  });
}

async function loadPendingRequests(): Promise<UserInteractionRequest[] | null> {
  try {
    const response = await fetch(`${API}/api/user-input/pending`, { credentials: "include" });
    if (!response.ok) return null;
    const data = await response.json();
    return pendingRequests(data.requests);
  } catch {
    return null;
  }
}

export function usePendingUserInteractions() {
  const { t } = useTranslation();
  const [requests, setRequests] = useState<UserInteractionRequest[]>([]);
  const knownIdsRef = useRef<Set<string>>(new Set());
  const fetchSequenceRef = useRef(0);

  const notify = useCallback((request: UserInteractionRequest) => {
    void notifyUserRequest(request, t("userApproval.title"), t("userInput.title"));
  }, [t]);

  const refetch = useCallback(async (notifyNew = false) => {
    const sequence = ++fetchSequenceRef.current;
    const next = await loadPendingRequests();
    if (next === null || sequence !== fetchSequenceRef.current) return;
    if (notifyNew) {
      for (const request of next) {
        if (!knownIdsRef.current.has(request.request_id)) notify(request);
      }
    }
    knownIdsRef.current = new Set(next.map((request) => request.request_id));
    setRequests(next);
  }, [notify]);

  useEffect(() => {
    const sequence = ++fetchSequenceRef.current;
    let active = true;
    void loadPendingRequests().then((next) => {
      if (!active || next === null || sequence !== fetchSequenceRef.current) return;
      knownIdsRef.current = new Set(next.map((request) => request.request_id));
      setRequests(next);
    });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    const onRequested = (event: Event) => {
      const request = (event as CustomEvent<UserInteractionRequest>).detail;
      if (!request || request.status !== "pending") return;
      fetchSequenceRef.current += 1;
      const isNew = !knownIdsRef.current.has(request.request_id);
      knownIdsRef.current.add(request.request_id);
      setRequests((current) => [
        ...current.filter((item) => item.request_id !== request.request_id),
        request,
      ]);
      if (isNew) notify(request);
    };
    const onResolved = (event: Event) => {
      const detail = (event as CustomEvent<{ request_id?: string }>).detail;
      if (!detail?.request_id) return;
      fetchSequenceRef.current += 1;
      knownIdsRef.current.delete(detail.request_id);
      setRequests((current) => current.filter((item) => item.request_id !== detail.request_id));
    };
    const unsubscribe = eventBus.subscribe("session_user_input_changed", () => {
      void refetch(true);
    });
    window.addEventListener("user_input_requested", onRequested);
    window.addEventListener("user_input_resolved", onResolved);
    return () => {
      unsubscribe();
      window.removeEventListener("user_input_requested", onRequested);
      window.removeEventListener("user_input_resolved", onResolved);
    };
  }, [notify, refetch]);

  const removeRequest = useCallback((requestId: string) => {
    knownIdsRef.current.delete(requestId);
    setRequests((current) => current.filter((request) => request.request_id !== requestId));
  }, []);

  return { requests, removeRequest };
}
