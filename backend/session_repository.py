from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Protocol

import session_store


RootWriterGuard = Callable[[str, Callable[[], None]], None]
RootVersion = tuple[int, int, int, int, int]


class SessionRootRepository(Protocol):
    def storage_identity(self) -> Path: ...

    def register_writer_guard(self, guard: RootWriterGuard) -> None: ...

    def resolve_root_ids(
        self,
        session_ids: list[str] | tuple[str, ...],
    ) -> dict[str, str]: ...

    def root_version(self, session_id: str) -> Optional[RootVersion]: ...

    def read_root(self, session_id: str) -> Optional[dict]: ...

    def copy_persistable_root(self, root: dict) -> dict: ...

    def write_root(
        self,
        root: dict,
        *,
        bump_updated_at: bool = True,
        preserve_projection_fields: bool = False,
        already_persistable: bool = False,
    ) -> Optional[RootVersion]: ...

    def delete_root(self, root_id: str) -> bool: ...


class JsonSessionRootRepository:
    def storage_identity(self) -> Path:
        return session_store._sessions_dir()

    def register_writer_guard(self, guard: RootWriterGuard) -> None:
        session_store.register_root_writer_guard(guard)

    def resolve_root_ids(
        self,
        session_ids: list[str] | tuple[str, ...],
    ) -> dict[str, str]:
        return session_store._resolve_root_ids(session_ids)

    def root_version(self, session_id: str) -> Optional[RootVersion]:
        return session_store.session_file_fingerprint(session_id)

    def read_root(self, session_id: str) -> Optional[dict]:
        return session_store.get_root_tree(session_id)

    def copy_persistable_root(self, root: dict) -> dict:
        return session_store.copy_persistable_tree(root)

    def write_root(
        self,
        root: dict,
        *,
        bump_updated_at: bool = True,
        preserve_projection_fields: bool = False,
        already_persistable: bool = False,
    ) -> Optional[RootVersion]:
        return session_store.write_session_full(
            root,
            bump_updated_at=bump_updated_at,
            preserve_projection_fields=preserve_projection_fields,
            already_persistable=already_persistable,
        )

    def delete_root(self, root_id: str) -> bool:
        return session_store.delete_session(root_id)
