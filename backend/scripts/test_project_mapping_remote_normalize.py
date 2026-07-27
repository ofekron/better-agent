"""Regression test: auto_match pass 1 must group projects by the NORMALIZED
git remote, so scp/https/.git variants of the same repo merge into one group.

Mirrors the isolation style of test_project_mapping_store_hot_reads.py:
engage an isolated test home BEFORE importing backend modules, call the
function directly, assert, then clean up.
"""
from __future__ import annotations

import os
import shutil
import sys
import uuid

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-project-mapping-remote-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import project_mapping_store  # noqa: E402
from project_mapping_store import auto_match, normalize_remote_url  # noqa: E402


def main() -> None:
    # Unit-level checks on the helper itself.
    cases = {
        "git@github.com:acme/widget.git": "github.com/acme/widget",
        "https://github.com/acme/widget": "github.com/acme/widget",
        "https://github.com/acme/widget.git": "github.com/acme/widget",
        "git@gitlab.com:acme/widget": "gitlab.com/acme/widget",
        "ssh://git@github.com/acme/widget.git": "github.com/acme/widget",
        "": "",
    }
    for raw, expected in cases.items():
        got = normalize_remote_url(raw)
        if got != expected:
            raise AssertionError(
                f"normalize_remote_url({raw!r}) = {got!r}, expected {expected!r}"
            )

    # None / non-str inputs must coerce to "" (defensive).
    if normalize_remote_url(None) != "":  # type: ignore[arg-type]
        raise AssertionError("normalize_remote_url(None) must return ''")
    if normalize_remote_url("   ") != "":
        raise AssertionError("normalize_remote_url('   ') must return ''")

    # auto_match pass 1: two projects on DIFFERENT nodes with EQUIVALENT
    # but differently-written remotes for the SAME repo must merge.
    projects = [
        {
            "node_id": "primary",
            "path": "/home/me/widget",
            "name": "widget",
            "git_remote": "git@github.com:acme/widget.git",
        },
        {
            "node_id": "node-b",
            "path": "/Users/bob/widget",
            "name": "widget",
            "git_remote": "https://github.com/acme/widget",
        },
        # A genuinely different repo — must NOT merge with the widget group.
        {
            "node_id": "node-c",
            "path": "/srv/gadget",
            "name": "gadget",
            "git_remote": "git@github.com:acme/gadget.git",
        },
        # A solo project (only one node) — must NOT produce a git group at all.
        {
            "node_id": "primary",
            "path": "/srv/solo",
            "name": "solo",
            "git_remote": "git@github.com:acme/solo.git",
        },
    ]

    groups = auto_match(projects)

    expected_gid = str(uuid.uuid5(uuid.NAMESPACE_URL, "git:github.com/acme/widget"))
    gids = {g["group_id"]: g for g in groups}

    if expected_gid not in gids:
        raise AssertionError(
            f"expected merged git group {expected_gid} not found; "
            f"got group ids: {sorted(gids)}"
        )

    widget_group = gids[expected_gid]
    widget_nodes = sorted(m["node_id"] for m in widget_group["members"])
    if widget_nodes != ["node-b", "primary"]:
        raise AssertionError(
            f"widget group should merge primary + node-b, got members: {widget_group['members']}"
        )
    # The displayed git_remote on each member is the normalized key.
    for m in widget_group["members"]:
        if m["git_remote"] != "github.com/acme/widget":
            raise AssertionError(
                f"member git_remote should be normalized key, got {m['git_remote']!r}"
            )

    # Gadget (single repo, single node) must not appear as a git group.
    gadget_gid = str(uuid.uuid5(uuid.NAMESPACE_URL, "git:github.com/acme/gadget"))
    if gadget_gid in gids:
        raise AssertionError(
            f"gadget should NOT form a git group (only one node): {gids[gadget_gid]}"
        )

    print("PASS project_mapping remote normalize groups equivalent remotes")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
