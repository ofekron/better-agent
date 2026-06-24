from __future__ import annotations

import oskeychain
from credential_broker.descriptor import coerce_secret_map
from credential_broker.executors.base import ExecResult, SinkExecutor


class LocalKeychainExecutor(SinkExecutor):
    kind = "local_keychain"

    def execute(self, descriptor: dict, secret: str | dict[str, str]) -> ExecResult:
        secrets = coerce_secret_map(secret)
        sink = descriptor["sink"]
        try:
            oskeychain.store(sink["service"], sink["account"], secrets["secret"])
        except Exception:
            return ExecResult(ok=False, error="failed to store credential in local keychain")
        return ExecResult(ok=True, body="stored")
