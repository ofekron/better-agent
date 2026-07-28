from __future__ import annotations

import sqlite3
import time
from contextlib import AbstractContextManager
from typing import Any

import native_transcript_index

_MAX_RESULT_BYTES = 2 * 1024 * 1024


class NativeAnalyticsSnapshot(AbstractContextManager["NativeAnalyticsSnapshot"]):
    def __init__(self, timeout_s: float = 5.0) -> None:
        self._deadline = time.monotonic() + timeout_s
        self._conn: sqlite3.Connection | None = None
        self.status: dict[str, Any] = {"state": "unavailable"}

    def __enter__(self) -> "NativeAnalyticsSnapshot":
        native_transcript_index._require_off_loop("native analytics snapshot")
        state = native_transcript_index.quick_state()
        path = native_transcript_index._db_path()
        if not path.exists():
            return self
        if not state.get("usable"):
            native_transcript_index.request_refresh()
        self.status = {
            "state": "current" if state.get("usable") else "stale",
            "refresh_requested": not state.get("usable"),
        }
        try:
            self._conn = native_transcript_index._connect(path, readonly=True)
            self._conn.set_progress_handler(
                lambda: 1 if time.monotonic() > self._deadline else 0,
                10_000,
            )
            self._conn.execute("BEGIN")
        except sqlite3.Error:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self.status = {"state": "unavailable"}
        return self

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
        statement = sql.strip().rstrip(";").strip()
        head = statement.lstrip("( \t\r\n").lower()
        if not (head.startswith("select") or head.startswith("with")):
            raise ValueError("native analytics accepts one read-only query")
        if ";" in statement:
            raise ValueError("native analytics accepts one read-only query")
        try:
            self._conn.set_authorizer(native_transcript_index._sql_authorizer)
            cursor = self._conn.execute(statement, params)
            columns = [column[0] for column in cursor.description or ()]
            rows: list[dict[str, Any]] = []
            result_bytes = 0
            for row in cursor:
                rendered = dict(zip(columns, row))
                result_bytes += sum(
                    len(str(value).encode("utf-8"))
                    for value in rendered.values()
                    if value is not None
                )
                if result_bytes > _MAX_RESULT_BYTES:
                    raise OverflowError("native analytics result exceeds byte budget")
                rows.append(rendered)
            return rows
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
