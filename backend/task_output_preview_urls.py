from __future__ import annotations

import hmac
import re

from itsdangerous import BadData, URLSafeTimedSerializer

MAX_AGE_SECONDS = 10 * 60
MAX_TOKEN_LENGTH = 1024
_SALT = "routine-output-preview-v1"
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_OUTPUT_ID_RE = re.compile(r"^[a-f0-9]{12}$")


def _validate_ids(task_id: str, output_id: str) -> tuple[str, str]:
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError("invalid task id")
    if not isinstance(output_id, str) or not _OUTPUT_ID_RE.fullmatch(output_id):
        raise ValueError("invalid output id")
    return task_id, output_id


def _serializer() -> URLSafeTimedSerializer:
    import auth

    return URLSafeTimedSerializer(auth.get_session_secret(), salt=_SALT)


def mint(task_id: str, output_id: str) -> str:
    task_id, output_id = _validate_ids(task_id, output_id)
    return _serializer().dumps({"task_id": task_id, "output_id": output_id})


def verify(token: str, task_id: str, output_id: str) -> None:
    task_id, output_id = _validate_ids(task_id, output_id)
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
        raise ValueError("invalid preview token")
    try:
        payload = _serializer().loads(token, max_age=MAX_AGE_SECONDS)
    except BadData as exc:
        raise ValueError("invalid preview token") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid preview token")
    signed_task_id = payload.get("task_id")
    signed_output_id = payload.get("output_id")
    if not isinstance(signed_task_id, str) or not isinstance(signed_output_id, str):
        raise ValueError("invalid preview token")
    if not hmac.compare_digest(signed_task_id, task_id):
        raise ValueError("invalid preview token")
    if not hmac.compare_digest(signed_output_id, output_id):
        raise ValueError("invalid preview token")
