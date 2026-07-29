from __future__ import annotations

from typing import Any

from extension_identifiers import EXTENSION_ID_RE, EXTENSION_SETTING_KEY_RE


class HarnessSecretRefError(ValueError):
    pass


def normalize_harness_secret_refs(value: Any) -> dict[str, list[str]]:
    if type(value) is not dict:
        raise HarnessSecretRefError("secret_refs must be an object")
    normalized: dict[str, list[str]] = {}
    for extension_id, refs in value.items():
        if type(extension_id) is not str or not EXTENSION_ID_RE.fullmatch(
            extension_id,
        ):
            raise HarnessSecretRefError("secret_refs extension id is invalid")
        if type(refs) is not list:
            raise HarnessSecretRefError(
                f"secret_refs.{extension_id} must be a list",
            )
        seen: set[str] = set()
        normalized_refs: list[str] = []
        for reference in refs:
            if type(reference) is not str or reference in seen:
                raise HarnessSecretRefError(
                    f"secret_refs.{extension_id} contains an invalid reference",
                )
            parts = reference.split(":")
            if (
                len(parts) != 3
                or parts[0] != "extension-setting"
                or parts[1] != extension_id
                or not EXTENSION_SETTING_KEY_RE.fullmatch(parts[2])
            ):
                raise HarnessSecretRefError(
                    f"secret_refs.{extension_id} contains an invalid reference",
                )
            seen.add(reference)
            normalized_refs.append(reference)
        normalized[extension_id] = normalized_refs
    return normalized
