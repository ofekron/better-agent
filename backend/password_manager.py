from __future__ import annotations

import json

import oskeychain
from keychain_names import LEGACY_SERVICE, PRIMARY_SERVICE, service_names

MAX_SERVICE_LEN = 128
MAX_ACCOUNT_LEN = 256
MAX_PASSWORD_LEN = 65536
INDEX_SERVICE = PRIMARY_SERVICE
LEGACY_INDEX_SERVICE = LEGACY_SERVICE
INDEX_ACCOUNT = "password-manager-index"
EXPECTED_FIELDS = frozenset({"service", "account", "password"})
DELETE_FIELDS = frozenset({"service", "account"})
RESERVED_SERVICES = frozenset({
    INDEX_SERVICE,
    LEGACY_INDEX_SERVICE,
    "claude code-credentials",
})


class PasswordManagerError(ValueError):
    pass


def _clean_text(value: object, field: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise PasswordManagerError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise PasswordManagerError(f"{field} is required")
    if len(text) > max_len:
        raise PasswordManagerError(f"{field} is too long")
    if any(ord(ch) < 32 for ch in text):
        raise PasswordManagerError(f"{field} contains control characters")
    if "/" in text:
        raise PasswordManagerError(f"{field} cannot contain /")
    return text


def store_service_password(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise PasswordManagerError("body must be an object")
    unexpected = set(payload) - EXPECTED_FIELDS
    if unexpected:
        raise PasswordManagerError("unexpected field")
    service = _clean_text(payload.get("service"), "service", MAX_SERVICE_LEN)
    account = _clean_text(payload.get("account"), "account", MAX_ACCOUNT_LEN)
    if service.lower() in RESERVED_SERVICES:
        raise PasswordManagerError("service is reserved")
    password = payload.get("password")
    if not isinstance(password, str):
        raise PasswordManagerError("password must be a string")
    if not password:
        raise PasswordManagerError("password is required")
    if len(password) > MAX_PASSWORD_LEN:
        raise PasswordManagerError("password is too long")
    oskeychain.store(service, account, password)
    _add_index_item(service, account)
    return {"service": service, "account": account}


def list_service_passwords() -> dict:
    return {"items": _read_index()}


def delete_service_password(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise PasswordManagerError("body must be an object")
    unexpected = set(payload) - DELETE_FIELDS
    if unexpected:
        raise PasswordManagerError("unexpected field")
    service = _clean_text(payload.get("service"), "service", MAX_SERVICE_LEN)
    account = _clean_text(payload.get("account"), "account", MAX_ACCOUNT_LEN)
    if service.lower() in RESERVED_SERVICES:
        raise PasswordManagerError("service is reserved")
    oskeychain.delete(service, account)
    _remove_index_item(service, account)
    return {"service": service, "account": account}


def has_service_password(service: str, account: str) -> bool:
    service = _clean_text(service, "service", MAX_SERVICE_LEN)
    account = _clean_text(account, "account", MAX_ACCOUNT_LEN)
    if not _is_indexed(service, account):
        return False
    return oskeychain.get(service, account) is not None


def get_service_password(service: str, account: str) -> str:
    service = _clean_text(service, "service", MAX_SERVICE_LEN)
    account = _clean_text(account, "account", MAX_ACCOUNT_LEN)
    if not _is_indexed(service, account):
        raise PasswordManagerError("password not found")
    value = oskeychain.get(service, account)
    if value is None:
        raise PasswordManagerError("password not found")
    return value


def _is_indexed(service: str, account: str) -> bool:
    return any(
        item["service"] == service and item["account"] == account
        for item in _read_index()
    )


def _read_index() -> list[dict]:
    parsed_items = []
    for service in service_names(INDEX_SERVICE, LEGACY_INDEX_SERVICE):
        raw = oskeychain.get(service, INDEX_ACCOUNT)
        if raw is None:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PasswordManagerError("password manager index is invalid") from exc
        if not isinstance(parsed, list):
            raise PasswordManagerError("password manager index is invalid")
        parsed_items.extend(parsed)
    if not parsed_items:
        return []
    return _clean_index_items(parsed_items)


def _clean_index_items(parsed: list[object]) -> list[dict]:
    items = []
    seen = set()
    for item in parsed:
        if not isinstance(item, dict):
            raise PasswordManagerError("password manager index is invalid")
        service = _clean_text(item.get("service"), "service", MAX_SERVICE_LEN)
        account = _clean_text(item.get("account"), "account", MAX_ACCOUNT_LEN)
        key = (service, account)
        if key in seen:
            continue
        seen.add(key)
        items.append({"service": service, "account": account})
    return sorted(items, key=lambda item: (item["service"].lower(), item["account"].lower()))


def _write_index(items: list[dict]) -> None:
    encoded = json.dumps(items, separators=(",", ":"))
    for service in service_names(INDEX_SERVICE, LEGACY_INDEX_SERVICE):
        oskeychain.store(service, INDEX_ACCOUNT, encoded)


def _add_index_item(service: str, account: str) -> None:
    items = _read_index()
    if not any(item["service"] == service and item["account"] == account for item in items):
        items.append({"service": service, "account": account})
    _write_index(sorted(items, key=lambda item: (item["service"].lower(), item["account"].lower())))


def _remove_index_item(service: str, account: str) -> None:
    items = [
        item for item in _read_index()
        if item["service"] != service or item["account"] != account
    ]
    _write_index(items)
