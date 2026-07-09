/**
 * Hardcoded global identity for the virtual singleton "Ask" session.
 *
 * Mirrors `backend/session_search.py:ASK_SINGLETON_ID`. The two strings
 * MUST match — the routes resolve to the same virtual session record.
 *
 * The Ask UI lives inside the regular session-view component but is
 * specialised by an id check (`currentSession?.id === ASK_SINGLETON_ID`):
 * scrollback is hidden, a synthetic greeting is rendered, and the
 * picker is rendered once the singleton's last assistant message
 * carries canonical `{ results, reasoning }` search data.
 */
export const ASK_SINGLETON_ID = "virtual:ofek-dev.ask:ask";
