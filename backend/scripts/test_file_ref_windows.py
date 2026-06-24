"""Regression lock: file-ref resolution recognizes Windows paths.

Before the fix, `_FILE_RE` only understood `/` separators and had no
drive-letter provision, so a Windows absolute path like
`C:\\proj\\app.py` was truncated to its tail (`app.py`) and never
linked; and `_sub` gated absoluteness on `startswith("/")`, which a
drive path fails. This silently produced wrong/missing file links on
Windows while macOS worked.

The cache is seeded directly (bypassing os.path.isfile via the
_ExistsCache hit at lines 67-70) so the test is host-independent: a
Windows path can't exist on the macOS/Linux CI box.
"""

import sys
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import file_ref_resolver as fr  # noqa: E402

WIN_BS = r"C:\proj\app.py"
WIN_FS = "C:/proj/app.py"
UNC = r"\\server\share\x.py"
POSIX = "/Users/x/app.py"


def _seed(p: str) -> None:
    fr._cache._d[p] = True


def _href_path(rendered: str) -> str | None:
    """Extract + decode the path from a single bcfile link, or None."""
    i = rendered.find("bcfile:")
    if i == -1:
        return None
    rest = rendered[i + len("bcfile:"):]
    rest = rest.split(")")[0]
    raw = rest.split("?")[0]
    return unquote(raw)


def check(name: str, cond: bool) -> None:
    if not cond:
        print(f"FAIL — {name}")
        raise SystemExit(1)
    print(f"ok — {name}")


def main() -> int:
    # 1. Regex captures whole Windows paths (both separators) + UNC.
    m = fr._FILE_RE.search(WIN_BS)
    check("regex captures full backslash path", m and m.group("path") == WIN_BS)
    m = fr._FILE_RE.search(WIN_FS)
    check("regex captures full forward-slash drive path", m and m.group("path") == WIN_FS)
    m = fr._FILE_RE.search(UNC)
    check("regex keeps both UNC leading backslashes", m and m.group("path") == UNC)
    m = fr._FILE_RE.search(WIN_BS + ":10-20")
    check(
        "drive colon vs line-range disambiguate",
        m and m.group("path") == WIN_BS and m.group("lines") == "10-20",
    )

    # 2. _is_absolute truth table (B4: drive-relative stays relative).
    check("posix abs", fr._is_absolute("/x.py"))
    check("drive abs backslash", fr._is_absolute(r"C:\x.py"))
    check("drive abs slash", fr._is_absolute("C:/x.py"))
    check("unc abs", fr._is_absolute(UNC))
    check("leading backslash abs", fr._is_absolute(r"\x.py"))
    check("drive-relative is NOT absolute", not fr._is_absolute("C:app.py"))
    check("relative is NOT absolute", not fr._is_absolute("src/app.py"))

    # 3. rewrite_text links a Windows abs path to its OWN path, un-joined.
    _seed(WIN_BS)
    out = fr.rewrite_text(f"see {WIN_BS} please", cwd=r"C:\proj")
    check("backslash path linked", "bcfile:" in out)
    check("backslash path not cwd-joined", _href_path(out) == WIN_BS)

    _seed(WIN_FS)
    out = fr.rewrite_text(f"see {WIN_FS} please", cwd="C:/proj")
    check("forward-slash drive path not cwd-joined", _href_path(out) == WIN_FS)

    # 4. POSIX absolute still works (no regression).
    _seed(POSIX)
    out = fr.rewrite_text(f"open {POSIX}", cwd=None)
    check("posix abs still linked", _href_path(out) == POSIX)

    # 5. Relative still resolves against cwd (no regression).
    _seed("/proj/src/app.py")
    out = fr.rewrite_text("edit src/app.py", cwd="/proj")
    check("relative joined to cwd", _href_path(out) == "/proj/src/app.py")

    print("PASS — Windows file-ref resolution locked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
