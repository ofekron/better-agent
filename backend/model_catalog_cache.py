from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from codex_execution_common import SHA256_RE, canonical_json
from json_store import write_json_durable
from model_catalog_authority import (
    CatalogAuthority,
    CatalogAuthorityError,
)


CACHE_SCHEMA_VERSION = 3
MAX_CACHE_BYTES = 4 * 1024 * 1024
MAX_MODELS = 10_000
MAX_RETIRED = 100
MAX_MODEL_ID_LENGTH = 512
_SNAPSHOT_KEYS = frozenset({
    "schema",
    "authority",
    "models",
    "retired",
    "last_refreshed_at",
    "last_fetch_state",
    "digest",
})
_RETIRED_KEYS = frozenset({"id", "first_absent_at"})
_FETCH_STATES = frozenset({"ok", "failing"})


class CatalogCacheError(RuntimeError):
    pass


@dataclass(frozen=True)
class RetiredModel:
    id: str
    first_absent_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "first_absent_at": self.first_absent_at,
        }


@dataclass(frozen=True)
class CatalogSnapshot:
    authority: CatalogAuthority
    models: tuple[str, ...]
    retired: tuple[RetiredModel, ...]
    last_refreshed_at: float
    last_fetch_state: str

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload = {
            "schema": CACHE_SCHEMA_VERSION,
            "authority": self.authority.to_dict(),
            "models": list(self.models),
            "retired": [record.to_dict() for record in self.retired],
            "last_refreshed_at": self.last_refreshed_at,
            "last_fetch_state": self.last_fetch_state,
        }
        if include_digest:
            payload["digest"] = _snapshot_digest(payload)
        return payload

    @property
    def digest(self) -> str:
        return _snapshot_digest(self.to_dict(include_digest=False))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> CatalogSnapshot:
        if type(raw) is not dict or set(raw) != _SNAPSHOT_KEYS:
            raise CatalogCacheError("invalid catalog snapshot")
        if raw.get("schema") != CACHE_SCHEMA_VERSION:
            raise CatalogCacheError("unsupported catalog cache schema")
        try:
            authority = CatalogAuthority.from_dict(raw.get("authority"))
        except CatalogAuthorityError as exc:
            raise CatalogCacheError("invalid catalog authority") from exc
        models = _model_ids(raw.get("models"), limit=MAX_MODELS)
        retired = _retired_models(raw.get("retired"))
        retired_ids = tuple(record.id for record in retired)
        if set(models) & set(retired_ids):
            raise CatalogCacheError("active and retired models overlap")
        last_refreshed_at = raw.get("last_refreshed_at")
        if (
            type(last_refreshed_at) is not float
            or not math.isfinite(last_refreshed_at)
            or last_refreshed_at < 0
        ):
            raise CatalogCacheError("invalid catalog refresh timestamp")
        last_fetch_state = raw.get("last_fetch_state")
        if (
            type(last_fetch_state) is not str
            or last_fetch_state not in _FETCH_STATES
        ):
            raise CatalogCacheError("invalid catalog fetch state")
        supplied_digest = raw.get("digest")
        payload = dict(raw)
        payload.pop("digest", None)
        if (
            type(supplied_digest) is not str
            or not SHA256_RE.fullmatch(supplied_digest)
            or supplied_digest != _snapshot_digest(payload)
        ):
            raise CatalogCacheError("catalog snapshot digest mismatch")
        return cls(
            authority=authority,
            models=models,
            retired=retired,
            last_refreshed_at=last_refreshed_at,
            last_fetch_state=last_fetch_state,
        )


@dataclass(frozen=True)
class CatalogCacheRead:
    status: Literal["matching", "source_changed", "missing", "invalid"]
    snapshot: CatalogSnapshot | None = None
    stored_authority: CatalogAuthority | None = None


def _model_id(raw: object) -> str:
    if (
        type(raw) is not str
        or not raw
        or raw.strip() != raw
        or len(raw) > MAX_MODEL_ID_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise CatalogCacheError("invalid model id")
    return raw


def _model_ids(raw: object, *, limit: int) -> tuple[str, ...]:
    if type(raw) is not list or len(raw) > limit:
        raise CatalogCacheError("invalid model list")
    models = tuple(_model_id(value) for value in raw)
    if len(set(models)) != len(models):
        raise CatalogCacheError("duplicate model id")
    return models


def _retired_models(raw: object) -> tuple[RetiredModel, ...]:
    if type(raw) is not list or len(raw) > MAX_RETIRED:
        raise CatalogCacheError("invalid retired model list")
    records: list[RetiredModel] = []
    for item in raw:
        if type(item) is not dict or set(item) != _RETIRED_KEYS:
            raise CatalogCacheError("invalid retired model")
        first_absent_at = item.get("first_absent_at")
        if (
            type(first_absent_at) is not float
            or not math.isfinite(first_absent_at)
            or first_absent_at < 0
        ):
            raise CatalogCacheError("invalid retired model timestamp")
        records.append(
            RetiredModel(
                id=_model_id(item.get("id")),
                first_absent_at=first_absent_at,
            ),
        )
    ids = tuple(record.id for record in records)
    if len(set(ids)) != len(ids):
        raise CatalogCacheError("duplicate retired model id")
    return tuple(records)


def _snapshot_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def build_catalog_snapshot(
    *,
    authority: CatalogAuthority,
    models: Sequence[str],
    retired: Sequence[Mapping[str, Any]],
    last_refreshed_at: float,
    last_fetch_state: str,
) -> CatalogSnapshot:
    if (
        type(authority) is not CatalogAuthority
        or type(models) not in {list, tuple}
        or type(retired) not in {list, tuple}
    ):
        raise CatalogCacheError("invalid catalog snapshot input")
    payload = {
        "schema": CACHE_SCHEMA_VERSION,
        "authority": authority.to_dict(),
        "models": list(models),
        "retired": list(retired),
        "last_refreshed_at": last_refreshed_at,
        "last_fetch_state": last_fetch_state,
    }
    payload["digest"] = _snapshot_digest(payload)
    return CatalogSnapshot.from_dict(payload)


def _read_bytes(
    path: Path,
    expected: os.stat_result,
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_dev != expected.st_dev
            or observed.st_ino != expected.st_ino
            or observed.st_size > MAX_CACHE_BYTES
        ):
            raise CatalogCacheError("invalid catalog cache file")
        chunks: list[bytes] = []
        remaining = MAX_CACHE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_CACHE_BYTES:
            raise CatalogCacheError("catalog cache is too large")
        after = os.fstat(descriptor)
        if observed != after:
            raise CatalogCacheError("catalog cache changed during read")
        return data, after
    finally:
        os.close(descriptor)


def _same_path_identity(path: Path, observed: os.stat_result) -> bool:
    try:
        current = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(current.st_mode)
        and current.st_dev == observed.st_dev
        and current.st_ino == observed.st_ino
    )


def _remove_if_same(path: Path, observed: os.stat_result | None) -> None:
    try:
        if observed is None:
            if path.is_symlink():
                path.unlink()
            return
        if _same_path_identity(path, observed):
            path.unlink()
    except OSError:
        return


def _parse_snapshot(data: bytes) -> CatalogSnapshot:
    try:
        decoded = data.decode("utf-8")
        raw = json.loads(
            decoded,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite number"),
            ),
        )
    except (RecursionError, UnicodeDecodeError, ValueError) as exc:
        raise CatalogCacheError("invalid catalog cache JSON") from exc
    return CatalogSnapshot.from_dict(raw)


def _load_snapshot(path: Path) -> CatalogSnapshot | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CatalogCacheError("invalid catalog cache file") from exc
    if not stat.S_ISREG(before.st_mode):
        _remove_if_same(path, None)
        raise CatalogCacheError("invalid catalog cache file")
    try:
        data, observed = _read_bytes(path, before)
    except FileNotFoundError:
        return None
    except (CatalogCacheError, OSError) as exc:
        _remove_if_same(path, before)
        raise CatalogCacheError("invalid catalog cache file") from exc
    try:
        snapshot = _parse_snapshot(data)
    except CatalogCacheError:
        _remove_if_same(path, observed)
        raise
    if not _same_path_identity(path, observed):
        raise CatalogCacheError("catalog cache changed during read")
    return snapshot


def read_catalog_cache(
    path: Path,
    *,
    expected_authority: CatalogAuthority,
) -> CatalogCacheRead:
    if type(expected_authority) is not CatalogAuthority:
        raise CatalogCacheError("expected catalog authority is required")
    try:
        snapshot = _load_snapshot(path)
    except CatalogCacheError:
        return CatalogCacheRead(status="invalid")
    if snapshot is None:
        return CatalogCacheRead(status="missing")
    if snapshot.authority != expected_authority:
        return CatalogCacheRead(
            status="source_changed",
            stored_authority=snapshot.authority,
        )
    return CatalogCacheRead(
        status="matching",
        snapshot=snapshot,
        stored_authority=snapshot.authority,
    )


def write_catalog_cache(path: Path, snapshot: CatalogSnapshot) -> bool:
    if not isinstance(path, Path) or type(snapshot) is not CatalogSnapshot:
        raise CatalogCacheError("invalid catalog cache write")
    payload = snapshot.to_dict()
    try:
        current = _load_snapshot(path)
    except CatalogCacheError:
        current = None
    if current is not None and current.to_dict() == payload:
        return False
    write_json_durable(path, payload)
    return True
