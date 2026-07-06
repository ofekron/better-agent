"""Locks the union-merge of previous-build hashed assets into a new dist.

Content-hashed chunks from the previous build must survive a rebuild so
long-lived tabs keep resolving their lazy chunks (no forced app reload on
file-panel open). Existing names must never be overwritten; stale files
(>7 days) must not be carried forward.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "frontend" / "scripts" / "build-atomic.mjs"


def run() -> None:
    node = shutil.which("node")
    assert node, "node is required for this test"
    with tempfile.TemporaryDirectory() as tmp:
        prev = Path(tmp) / "prev"
        new = Path(tmp) / "new"
        (prev / "assets").mkdir(parents=True)
        (new / "assets").mkdir(parents=True)

        (prev / "assets" / "OldChunk-aaa.js").write_text("old", encoding="utf-8")
        stale = prev / "assets" / "Stale-bbb.js"
        stale.write_text("stale", encoding="utf-8")
        eight_days_ago = time.time() - 8 * 24 * 3600
        os.utime(stale, (eight_days_ago, eight_days_ago))
        (prev / "assets" / "Dupe-ccc.js").write_text("previous content", encoding="utf-8")
        (new / "assets" / "Dupe-ccc.js").write_text("current content", encoding="utf-8")
        (prev / "index.html").write_text("<html>old shell</html>", encoding="utf-8")

        out = subprocess.run(
            [node, str(SCRIPT), "--merge-assets", str(prev), str(new)],
            capture_output=True,
            text=True,
            check=True,
        )
        merged = json.loads(out.stdout)["merged"]

        assert "OldChunk-aaa.js" in merged, f"fresh old chunk must merge, got {merged}"
        assert (new / "assets" / "OldChunk-aaa.js").read_text(encoding="utf-8") == "old"
        assert "Stale-bbb.js" not in merged and not (new / "assets" / "Stale-bbb.js").exists(), (
            "stale chunk must be pruned"
        )
        assert (new / "assets" / "Dupe-ccc.js").read_text(encoding="utf-8") == "current content", (
            "existing names must never be overwritten"
        )
        assert not (new / "index.html").exists() or "old shell" not in (
            new / "index.html"
        ).read_text(encoding="utf-8"), "merge must touch only assets/, never index.html"

    with tempfile.TemporaryDirectory() as tmp:
        dist = Path(tmp) / "dist"
        new = Path(tmp) / "new"
        (dist / "assets").mkdir(parents=True)
        (new / "assets").mkdir(parents=True)

        (dist / "index.html").write_text("<html>old shell</html>", encoding="utf-8")
        (dist / "assets" / "OldChunk-aaa.js").write_text("old", encoding="utf-8")
        (new / "index.html").write_text("<html>new shell</html>", encoding="utf-8")
        (new / "assets" / "NewChunk-bbb.js").write_text("new", encoding="utf-8")

        out = subprocess.run(
            [node, str(SCRIPT), "--publish-build", str(new), str(dist)],
            capture_output=True,
            text=True,
            check=True,
        )
        published = json.loads(out.stdout)

        assert published["sentinelExistedDuringPublish"] is True, (
            "publish must keep the served dist directory present during replacement"
        )
        assert (dist / "index.html").read_text(encoding="utf-8") == "<html>new shell</html>"
        assert (dist / "assets" / "NewChunk-bbb.js").read_text(encoding="utf-8") == "new"
        assert (dist / "assets" / "OldChunk-aaa.js").read_text(encoding="utf-8") == "old"

    print("test_frontend_build_asset_merge: OK")


if __name__ == "__main__":
    run()
