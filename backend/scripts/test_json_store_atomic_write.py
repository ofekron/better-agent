from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json_store  # noqa: E402


def test_write_json_uses_unique_temp_file_per_write() -> None:
    with tempfile.TemporaryDirectory(prefix="bc-test-json-store-") as tmpdir:
        path = Path(tmpdir) / "store.json"
        sources: list[str] = []
        real_replace = json_store.os.replace

        def recording_replace(src, dst):
            sources.append(Path(src).name)
            real_replace(src, dst)

        json_store.os.replace = recording_replace
        try:
            json_store.write_json(path, {"n": 1})
            json_store.write_json(path, {"n": 2})
        finally:
            json_store.os.replace = real_replace

        assert len(sources) == 2
        assert len(set(sources)) == 2, sources
        assert json.loads(path.read_text(encoding="utf-8")) == {"n": 2}
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def main() -> None:
    test_write_json_uses_unique_temp_file_per_write()
    print("PASS json_store atomic writes use unique temp files")


if __name__ == "__main__":
    main()
