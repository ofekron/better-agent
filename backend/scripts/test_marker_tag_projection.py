"""Locks that the attention-marker TAG rides the marker projection.

`detect_markers` must stamp the source tag into the returned marker dict
(a NEW dict — never mutating the shared `rule["marker"]` ref) so status
sort can classify by tag instead of drifting color/tooltip, and so the
`set_marker` change-gate stays coherent (stored == incoming).

Run with:
    cd backend && .venv/bin/python scripts/test_marker_tag_projection.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home  # noqa: E402
_test_home.isolate("bc_test_marker_tag_")

import file_ref_resolver as frr  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    if not cond:
        failures.append(name)
        print(f"FAIL {name}")
    else:
        print(f"ok   {name}")


shared_marker = {"color": "#2563eb", "tooltip": "All tasks done", "sound": False}
frr.set_tag_rules([
    {
        "tag": "ALL_TASKS__DONE",
        "marker": shared_marker,
        "_extension_id": "ofek-dev.user-attention",
    },
])

out = frr.detect_markers("work <ALL_TASKS__DONE>finished</ALL_TASKS__DONE>")
check("detect.returns_one", len(out) == 1)
ext_id, marker = out[0]
check("detect.ext_id", ext_id == "ofek-dev.user-attention")
check("detect.tag_present", marker.get("tag") == "ALL_TASKS__DONE")
check("detect.color_preserved", marker.get("color") == "#2563eb")
# the rule's shared marker dict must NOT be mutated with the tag
check("detect.no_shared_mutation", "tag" not in shared_marker)
# no tag in text → no markers
check("detect.absent", frr.detect_markers("nothing here") == [])

frr.set_tag_rules([])  # reset global state

if failures:
    print(f"\n{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("\nPASS test_marker_tag_projection")
