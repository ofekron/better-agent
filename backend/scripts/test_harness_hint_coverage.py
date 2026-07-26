"""Every configurable harness control must carry a user-facing explanation.

Reads the two real sources — the group/permission vocabularies declared in
the backend, and the shipped i18n catalogs — so adding a group or a
permission without explaining it fails here instead of shipping a bare
toggle.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
REPO = BACKEND.parent
I18N = REPO / "frontend" / "src" / "i18n"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

HINT_PREFIX = "harnessProfile."
GROUP_HINT = "harnessProfile.groupHint."
PERMISSION_HINT = "harnessProfile.itemHint.permissions."


def _group_ids() -> set[str]:
    """Group ids from harness_fields' GROUP_* constants, read as source so an
    import of the whole backend package isn't required."""
    tree = ast.parse((BACKEND / "harness_fields.py").read_text(encoding="utf-8"))
    ids: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.startswith("GROUP_"):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    ids.add(node.value.value)
    return ids


def _permission_names() -> set[str]:
    """The `allowed` permission vocabulary inside _validate_permissions."""
    tree = ast.parse((BACKEND / "extension_store.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_validate_permissions":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "allowed" for t in inner.targets
            ):
                return {
                    element.value
                    for element in inner.value.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                }
    raise AssertionError("_validate_permissions.allowed not found")


def _catalog(locale: str) -> dict[str, str]:
    return json.loads((I18N / f"{locale}.json").read_text(encoding="utf-8"))


def test_every_group_has_an_explanation() -> None:
    catalog = _catalog("en")
    # profile_meta is rendered by its own widget with dedicated hints, not by
    # the generic group renderer.
    missing = sorted(
        group_id
        for group_id in _group_ids()
        if group_id != "profile_meta" and not catalog.get(f"{GROUP_HINT}{group_id}", "").strip()
    )
    assert not missing, f"harness groups without an explanation: {missing}"


def test_every_permission_has_an_explanation() -> None:
    catalog = _catalog("en")
    missing = sorted(
        name
        for name in _permission_names()
        if not catalog.get(f"{PERMISSION_HINT}{name}", "").strip()
    )
    assert not missing, f"permissions without an explanation: {missing}"


def test_every_locale_translates_the_explanations() -> None:
    english = {
        key
        for key in _catalog("en")
        if key.startswith(GROUP_HINT) or key.startswith("harnessProfile.itemHint.")
    }
    assert english, "no harness explanation keys found in en.json"
    for path in sorted(I18N.glob("*.json")):
        if path.stem == "en":
            continue
        catalog = _catalog(path.stem)
        missing = sorted(key for key in english if not catalog.get(key, "").strip())
        assert not missing, f"{path.stem}.json is missing explanations: {missing}"


def main() -> int:
    for test in (
        test_every_group_has_an_explanation,
        test_every_permission_has_an_explanation,
        test_every_locale_translates_the_explanations,
    ):
        test()
    print("PASS harness hint coverage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
