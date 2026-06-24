"""Tests for file_browser.search_tree (pruned recursive search) plus a
regression check that get_file_tree / list_directories are unchanged.

Pure-Python (no claude subprocess). Run: python backend/scripts/test_file_search.py
"""

import os
import sys
import stat
import shutil
import tempfile
from pathlib import Path

# State-dir isolation rule: set BETTER_CLAUDE_HOME before importing backend.
import _test_home
_test_home.isolate("bc_test_filesearch_")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_browser import search_tree, get_file_tree, list_directories  # noqa: E402


def collect(node, acc):
    """Flatten a FileNode tree into a list of (rel_name, type) by name."""
    if node is None:
        return acc
    acc.append((node["name"], node["type"]))
    for c in node.get("children", []) or []:
        collect(c, acc)
    return acc


def names(res):
    return {n for (n, _t) in collect(res.get("root"), [])}


def build_fixture(base: Path):
    (base / "src" / "components" / "Deep" / "Inner").mkdir(parents=True)
    (base / "src" / "components" / "Deep" / "Inner" / "target_file.tsx").write_text("x")
    (base / "src" / "components" / "Other.tsx").write_text("x")
    (base / "src" / "utils.py").write_text("x")
    (base / "README.md").write_text("x")

    (base / "node_modules" / "pkg").mkdir(parents=True)
    (base / "node_modules" / "pkg" / "match_file.tsx").write_text("x")

    (base / ".secret").mkdir()
    (base / ".secret" / "hidden.txt").write_text("x")
    (base / ".env").write_text("x")

    (base / "zdir" / "zsub").mkdir(parents=True)
    (base / "zdir" / "zsub" / "leaf.txt").write_text("x")

    # symbols host: token lives ONLY in file CONTENT (uppercase), absent
    # from every path/name. Drives the ripgrep "symbols" method + the
    # uppercase-content / lowercased-query case.
    (base / "src" / "symhost.txt").write_text("prefix ZZQQSYMBOL suffix\n")

    # symlink cycle: must be skipped, must not hang
    os.symlink(str(base), str(base / "loopdir"))

    # unreadable dir: PermissionError mid-walk must not crash
    noperm = base / "noperm"
    noperm.mkdir()
    (noperm / "secret.tsx").write_text("x")
    os.chmod(noperm, 0)
    return noperm


def main():
    tmp = Path(tempfile.mkdtemp(prefix="bc_filesearch_fixture_"))
    noperm = build_fixture(tmp)
    root = str(tmp)
    try:
        # 1. deep file match: full ancestor chain, non-matching sibling absent
        r = search_tree(root, "target_file", "file")
        assert r["root"] is not None, "deep file should match"
        ns = names(r)
        for seg in ("src", "components", "Deep", "Inner", "target_file.tsx"):
            assert seg in ns, f"missing ancestor/match {seg}: {ns}"
        assert "Other.tsx" not in ns, f"non-matching sibling leaked: {ns}"
        assert "utils.py" not in ns, f"non-matching sibling leaked: {ns}"
        assert r["count"] == 1, r["count"]

        # 2. dir-segment query surfaces descendant files (flattenFiles parity)
        r = search_tree(root, "components", "file")
        ns = names(r)
        assert {"target_file.tsx", "Other.tsx"} <= ns, f"dir-segment parity broken: {ns}"

        # 3. SKIP_DIRS (node_modules) excluded
        r = search_tree(root, "match_file", "file")
        assert r["root"] is None and r["count"] == 0, "node_modules must be skipped"

        # 4. hidden dir is searchable (user preference: no skip)
        r = search_tree(root, "hidden", "file")
        assert "hidden.txt" in names(r), "hidden files inside dotdirs must be found"

        # 5. .env included despite being a dotfile
        r = search_tree(root, "env", "file")
        assert ".env" in names(r), "(.env) must be searchable"

        # 6. kind=dir matches dirs not files; root not spuriously returned
        r = search_tree(root, "zsub", "dir")
        coll = collect(r.get("root"), [])
        assert ("zsub", "directory") in coll, coll
        assert all(t == "directory" for (_n, t) in coll), f"file leaked into dir search: {coll}"
        assert r["count"] == 1, f"only zsub should count, got {r['count']}"
        r = search_tree(root, "target_file", "dir")
        assert r["root"] is None, "a file must not match kind=dir"

        # 7. truncated when exceeding max_results
        r = search_tree(root, ".tsx", "file", max_results=1)
        assert r["truncated"] is True and r["count"] <= 1, (r["truncated"], r["count"])

        # 8. symlink loop: skipped, does not hang
        r = search_tree(root, "loopdir", "dir")
        assert r["root"] is None, "directory symlink must be skipped"

        # 9. PermissionError mid-walk does not crash
        r = search_tree(root, "secret", "file")
        assert "secret.tsx" not in names(r), "unreadable dir contents must not appear"

        # 10. empty / no-match query
        assert search_tree(root, "", "file")["root"] is None
        assert search_tree(root, "zzzznope", "file")["root"] is None

        # 11. name-only vs path-only divergence. "components" is only an
        # ANCESTOR dir segment of these files, never a basename.
        r = search_tree(root, "components", "file", ["path"])
        assert {"target_file.tsx", "Other.tsx"} <= names(r), names(r)
        r = search_tree(root, "components", "file", ["name"])
        assert r["root"] is None, f"name method must not match dir segment: {names(r)}"
        # basename query matched by the name method
        r = search_tree(root, "other", "file", ["name"])
        assert "Other.tsx" in names(r), names(r)

        # 12. OR union ⊇ single-method result
        r = search_tree(root, "components", "file", ["path", "name"])
        assert {"target_file.tsx", "Other.tsx"} <= names(r), names(r)

        # 13. empty / all-invalid methods => nothing (no masked default)
        r = search_tree(root, "target_file", "file", [])
        assert r["root"] is None and r["symbols_unavailable"] is False, r
        r = search_tree(root, "target_file", "file", ["bogus"])
        assert r["root"] is None, r

        # 14. kind=="dir" drops "symbols" => nothing matches
        r = search_tree(root, "zsub", "dir", ["symbols"])
        assert r["root"] is None, "symbols must be dropped for kind=dir"

        # 15. regression: default methods == explicit ["path"]
        a = search_tree(root, "target_file", "file")
        b = search_tree(root, "target_file", "file", ["path"])
        assert a["count"] == b["count"] == 1 and names(a) == names(b), (a, b)

        # 16. symbols: content-only token (uppercase) found via ripgrep
        #     with a lowercased query; NOT found by path; symlinked root
        #     still resolves correctly.
        TOKEN = "zzqqsymbol"  # lowercase query vs uppercase file content
        r_path = search_tree(root, TOKEN, "file", ["path"])
        assert r_path["root"] is None, "content-only token must not match by path"
        r_sym = search_tree(root, TOKEN, "file", ["symbols"])
        if shutil.which("rg") is None:
            assert r_sym["symbols_unavailable"] is True, r_sym
            print("OK (note: rg not installed — symbols positive skipped)")
        else:
            assert r_sym["symbols_unavailable"] is False, r_sym
            assert "symhost.txt" in names(r_sym), names(r_sym)
            # symlinked root must resolve so str(entry) == rg output
            link_parent = Path(tempfile.mkdtemp(prefix="bc_filesearch_link_"))
            link_root = link_parent / "linkroot"
            os.symlink(root, str(link_root))
            try:
                r_link = search_tree(str(link_root), TOKEN, "file", ["symbols"])
                assert "symhost.txt" in names(r_link), (
                    f"symlinked-root symbols broke (resolve invariant): {names(r_link)}"
                )
            finally:
                shutil.rmtree(link_parent, ignore_errors=True)

        # --- regression: get_file_tree unchanged (depth cap + skips) ---
        gt = get_file_tree(root)
        gnames = set()
        collect(gt, [])  # smoke
        gn = names({"root": gt})
        assert "src" in gn and ".env" in gn, gn
        assert "node_modules" not in gn, gn
        assert "target_file.tsx" not in gn, "depth cap (3) must still hide deep files"

        # --- regression: list_directories unchanged ---
        ld = list_directories(root)
        assert set(ld.keys()) == {"path", "parent", "entries"}, ld.keys()
        entry_names = {e["name"] for e in ld["entries"]}
        assert "src" in entry_names and "zdir" in entry_names, entry_names
        assert ".secret" not in entry_names, "hidden dir must be excluded"

        print("OK - all file_search tests passed")
    finally:
        os.chmod(noperm, stat.S_IRWXU)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"], ignore_errors=True)


if __name__ == "__main__":
    main()
