from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from paths import ba_home

import device_token_store

logger = logging.getLogger(__name__)

_SERVICE_ACCOUNT_ENV = "BETTER_AGENT_FCM_SERVICE_ACCOUNT"

_INIT_LOCK = threading.RLock()
_INIT_ATTEMPTED = False
_APP: object | None = None
_WARNED_UNCONFIGURED = False


def _service_account_path() -> Path | None:
    env_path = os.environ.get(_SERVICE_ACCOUNT_ENV, "").strip()
    if env_path:
        path = Path(env_path)
        return path if path.is_file() else None
    default_path = ba_home() / "config" / "fcm_service_account.json"
    return default_path if default_path.is_file() else None


def _get_app() -> object | None:
    """Lazily initialize the firebase-admin app. Returns None if unconfigured."""
    global _INIT_ATTEMPTED, _APP, _WARNED_UNCONFIGURED
    with _INIT_LOCK:
        if _INIT_ATTEMPTED:
            return _APP
        _INIT_ATTEMPTED = True
        service_account = _service_account_path()
        if service_account is None:
            if not _WARNED_UNCONFIGURED:
                logger.info(
                    "push_sender: no FCM service account configured, push notifications disabled"
                )
                _WARNED_UNCONFIGURED = True
            return None
        try:
            import firebase_admin
            from firebase_admin import credentials

            cred = credentials.Certificate(str(service_account))
            _APP = firebase_admin.initialize_app(cred)
        except Exception:
            logger.exception("push_sender: failed to initialize firebase-admin")
            _APP = None
        return _APP


def send_pending_input_push(session_id: str, request_kind: str, request_id: str) -> None:
    """Notify devices registered for this session about a new pending request.

    Never raises: a push failure must not break the caller's request flow.
    """
    try:
        _send_pending_input_push(session_id, request_kind, request_id)
    except Exception:
        logger.exception("push_sender: send_pending_input_push failed for session=%s", session_id)


def _send_pending_input_push(session_id: str, request_kind: str, request_id: str) -> None:
    app = _get_app()
    if app is None:
        return
    devices = device_token_store.get_tokens_for_session(session_id)
    if not devices:
        return

    from firebase_admin import messaging

    title = "Better Agent needs your input"
    body = "Approve or respond to continue" if request_kind == "approval" else "A question is waiting for your response"

    for device in devices:
        token = device.get("token")
        if not token:
            continue
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data={
                "session_id": session_id,
                "request_id": request_id,
                "request_kind": request_kind,
            },
            token=token,
        )
        try:
            messaging.send(message, app=app)
        except Exception as exc:
            if _is_unregistered_error(exc):
                device_token_store.unregister_token_for_value(token)
            else:
                logger.warning(
                    "push_sender: send failed for device=%s: %s",
                    device.get("device_id"),
                    type(exc).__name__,
                )


def _is_unregistered_error(exc: Exception) -> bool:
    try:
        from firebase_admin import exceptions as fa_exceptions
        from firebase_admin.messaging import UnregisteredError

        if isinstance(exc, UnregisteredError):
            return True
        if isinstance(exc, fa_exceptions.NotFoundError):
            return True
    except Exception:
        pass
    code = getattr(exc, "code", None)
    return code in ("UNREGISTERED", "NOT_FOUND", "INVALID_ARGUMENT")
