from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

TEST_HOME = Path(tempfile.mkdtemp(prefix="better-agent-thread-vector-test-"))
os.environ["BETTER_AGENT_HOME"] = str(TEST_HOME)
atexit.register(shutil.rmtree, TEST_HOME, True)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requirement_context as context  # noqa: E402


def _records(count: int) -> list[dict[str, object]]:
    return [
        {"id": f"id-{index}", "text": f"text-{index}", "cwds": ["/project"]}
        for index in range(count)
    ]


class RecordingEmbedder:
    def __init__(
        self,
        fail_call: int | None = None,
        embedding_space: str = "test-embedding-space-v1",
        on_call=None,
    ):
        self.calls: list[list[str]] = []
        self.fail_call = fail_call
        self.embedding_space = embedding_space
        self.on_call = on_call

    def __call__(self, texts: list[str]) -> np.ndarray:
        self.calls.append(list(texts))
        if self.on_call is not None:
            self.on_call()
        if self.fail_call == len(self.calls):
            raise RuntimeError("injected termination")
        return np.asarray(
            [[float(len(text)), float(sum(text.encode()) % 97)] for text in texts],
            dtype=np.float32,
        )


def _build(records, embedder, db_path: Path):
    return context._build_vector_index(
        records,
        id_field="id",
        text_fn=lambda record: str(record["text"]),
        cwds_fn=lambda record: record["cwds"],
        embedder=embedder,
        embedding_space=str(
            embedder.embedding_space
        ),
        db_path=db_path,
    )


def test_content_addressing_reuses_reordered_and_duplicate_texts() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "vectors.npz"
        initial = _records(3)
        first = RecordingEmbedder()
        vectors, ids, cwds, shas = _build(initial, first, db_path)
        context._write_vector_index_atomic(db_path, vectors, ids, cwds, shas)

        changed = [
            initial[2],
            {**initial[1], "text": "changed"},
            initial[0],
            {"id": "duplicate", "text": initial[0]["text"], "cwds": ["/other"]},
        ]
        second = RecordingEmbedder()
        vectors, ids, _cwds, _shas = _build(changed, second, db_path)
        assert second.calls == [["changed"]]
        assert list(ids) == ["id-2", "id-1", "id-0", "duplicate"]
        assert np.array_equal(vectors[2], vectors[3])


def test_build_batches_and_resumes_persisted_progress() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "vectors.npz"
        records = _records(context.VECTOR_BUILD_BATCH_SIZE * 2 + 5)
        interrupted = RecordingEmbedder(fail_call=2)
        try:
            _build(records, interrupted, db_path)
        except RuntimeError as exc:
            assert str(exc) == "injected termination"
        else:
            raise AssertionError("injected termination must escape")
        assert db_path.with_name(f"{db_path.name}.progress.npz").exists()

        resumed = RecordingEmbedder()
        _build(records, resumed, db_path)
        assert sum(map(len, resumed.calls)) == len(records) - context.VECTOR_BUILD_BATCH_SIZE
        assert all(len(call) <= context.VECTOR_BUILD_BATCH_SIZE for call in resumed.calls)


def test_empty_projection_does_not_poison_first_nonempty_build() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "vectors.npz"
        empty = RecordingEmbedder()
        vectors, ids, cwds, shas = _build([], empty, db_path)
        context._write_vector_index_atomic(db_path, vectors, ids, cwds, shas)
        populated = RecordingEmbedder()
        vectors, _ids, _cwds, _shas = _build(_records(1), populated, db_path)
        assert vectors.shape == (1, 2)
        assert populated.calls == [["text-0"]]


def test_stale_status_is_read_only_and_explicit() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        source.write_text("[]", encoding="utf-8")
        db_path = root / "vectors.npz"
        state_path = root / "state.json"
        state_path.write_text('{"source_mtime_ns":"0"}', encoding="utf-8")
        before = {
            path.name: (path.stat().st_mtime_ns, path.read_bytes())
            for path in root.iterdir()
        }
        status = context._vector_index_status(
            db_path=db_path,
            state_path=state_path,
            source_path=source,
            records=[],
            id_field="id",
            text_fn=lambda record: str(record["text"]),
            cwds_fn=lambda record: record["cwds"],
            embedding_space="test-embedding-space-v1",
        )
        after = {
            path.name: (path.stat().st_mtime_ns, path.read_bytes())
            for path in root.iterdir()
        }
        assert status["ready"] is False
        assert status["reason"] == "index_not_ready_or_stale"
        assert before == after


def test_cwd_change_invalidates_ready_projection() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        records = _records(1)
        source.write_text(json.dumps(records), encoding="utf-8")
        db_path = root / "vectors.npz"
        state_path = root / "state.json"
        embedder = RecordingEmbedder()
        built = context._ensure_vector_index(
            db_path=db_path,
            state_path=state_path,
            source_path=source,
            records=records,
            id_field="id",
            text_fn=lambda record: str(record["text"]),
            cwds_fn=lambda record: record["cwds"],
            embedder=embedder,
            embedding_space=embedder.embedding_space,
        )
        assert built["ready"] is True
        changed = [{**records[0], "cwds": ["/new-project"]}]
        status = context._vector_index_status(
            db_path=db_path,
            state_path=state_path,
            source_path=source,
            records=changed,
            id_field="id",
            text_fn=lambda record: str(record["text"]),
            cwds_fn=lambda record: record["cwds"],
            embedding_space=embedder.embedding_space,
        )
        assert status["ready"] is False


def test_embedding_space_change_rebuilds_same_dimension_vectors() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        records = _records(2)
        source.write_text(json.dumps(records), encoding="utf-8")
        first = RecordingEmbedder(embedding_space="model-v1")
        context._ensure_vector_index(
            db_path=root / "vectors.npz",
            state_path=root / "state.json",
            source_path=source,
            records=records,
            id_field="id",
            text_fn=lambda record: str(record["text"]),
            cwds_fn=lambda record: record["cwds"],
            embedder=first,
            embedding_space=first.embedding_space,
        )
        second = RecordingEmbedder(embedding_space="model-v2")
        context._ensure_vector_index(
            db_path=root / "vectors.npz",
            state_path=root / "state.json",
            source_path=source,
            records=records,
            id_field="id",
            text_fn=lambda record: str(record["text"]),
            cwds_fn=lambda record: record["cwds"],
            embedder=second,
            embedding_space=second.embedding_space,
        )
        assert second.calls == [["text-0", "text-1"]]


def test_embedding_space_change_can_change_vector_dimension() -> None:
    class ThreeDimensionalEmbedder(RecordingEmbedder):
        def __call__(self, texts: list[str]) -> np.ndarray:
            self.calls.append(list(texts))
            return np.asarray(
                [
                    [float(len(text)), float(sum(text.encode()) % 97), 1.0]
                    for text in texts
                ],
                dtype=np.float32,
            )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        records = _records(2)
        source.write_text(json.dumps(records), encoding="utf-8")
        first = RecordingEmbedder(embedding_space="model-v1")
        common = {
            "db_path": root / "vectors.npz",
            "state_path": root / "state.json",
            "source_path": source,
            "records": records,
            "id_field": "id",
            "text_fn": lambda record: str(record["text"]),
            "cwds_fn": lambda record: record["cwds"],
        }
        context._ensure_vector_index(
            **common,
            embedder=first,
            embedding_space=first.embedding_space,
        )
        second = ThreeDimensionalEmbedder(embedding_space="model-v2")
        result = context._ensure_vector_index(
            **common,
            embedder=second,
            embedding_space=second.embedding_space,
        )
        assert result["ready"] is True
        arrays = context._load_vector_arrays(root / "vectors.npz")
        assert arrays is not None
        assert arrays[0].shape == (2, 3)


def test_source_change_during_build_aborts_publication() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        records = _records(2)
        source.write_text(json.dumps(records), encoding="utf-8")
        embedder = RecordingEmbedder(
            on_call=lambda: source.write_text("changed", encoding="utf-8")
        )
        result = context._ensure_vector_index(
            db_path=root / "vectors.npz",
            state_path=root / "state.json",
            source_path=source,
            records=records,
            id_field="id",
            text_fn=lambda record: str(record["text"]),
            cwds_fn=lambda record: record["cwds"],
            embedder=embedder,
            embedding_space=embedder.embedding_space,
        )
        assert result["ready"] is False
        assert result["reason"] == "source_changed_during_build"
        assert not (root / "vectors.npz").exists()
        assert not (root / "state.json").exists()


def test_live_readers_see_only_complete_competing_generations() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db_path = root / "vectors.npz"
        state_path = root / "state.json"
        old_records = _records(5)
        old_source = root / "old.json"
        old_source.write_text(json.dumps(old_records), encoding="utf-8")
        old_embedder = RecordingEmbedder()
        context._ensure_vector_index(
            db_path=db_path,
            state_path=state_path,
            source_path=old_source,
            records=old_records,
            id_field="id",
            text_fn=lambda record: str(record["text"]),
            cwds_fn=lambda record: record["cwds"],
            embedder=old_embedder,
            embedding_space=old_embedder.embedding_space,
        )

        generations = []
        for prefix, count in (("a", 65), ("b", 67)):
            records = [
                {
                    "id": f"{prefix}-{index}",
                    "text": f"{prefix}-text-{index}",
                    "cwds": [f"/{prefix}"],
                }
                for index in range(count)
            ]
            source = root / f"{prefix}.json"
            source.write_text(json.dumps(records), encoding="utf-8")
            generations.append((prefix, source, records))

        script = """
import json, sys, time
from pathlib import Path
import numpy as np
sys.path.insert(0, sys.argv[1])
import requirement_context as c
source=Path(sys.argv[2])
records=json.loads(source.read_text())
def embed(texts):
    time.sleep(0.02)
    return np.asarray([[float(len(t)), float(sum(t.encode()) % 97)] for t in texts], dtype=np.float32)
c._ensure_vector_index(
    db_path=Path(sys.argv[3]),
    state_path=Path(sys.argv[4]),
    source_path=source,
    records=records,
    id_field="id",
    text_fn=lambda r: r["text"],
    cwds_fn=lambda r: r["cwds"],
    embedder=embed,
    embedding_space="test-embedding-space-v1",
)
"""
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(ROOT),
                    str(source),
                    str(db_path),
                    str(state_path),
                ]
            )
            for _prefix, source, _records_for_generation in generations
        ]
        observed_ready = {"old"}
        while any(process.poll() is None for process in processes):
            for label, source, records in [
                ("old", old_source, old_records),
                *generations,
            ]:
                status = context._vector_index_status(
                    db_path=db_path,
                    state_path=state_path,
                    source_path=source,
                    records=records,
                    id_field="id",
                    text_fn=lambda record: str(record["text"]),
                    cwds_fn=lambda record: record["cwds"],
                    embedding_space="test-embedding-space-v1",
                )
                if status["ready"]:
                    observed_ready.add(label)
            time.sleep(0.001)
        assert [process.wait(timeout=30) for process in processes] == [0, 0]
        final_ready = []
        for label, source, records in generations:
            status = context._vector_index_status(
                db_path=db_path,
                state_path=state_path,
                source_path=source,
                records=records,
                id_field="id",
                text_fn=lambda record: str(record["text"]),
                cwds_fn=lambda record: record["cwds"],
                embedding_space="test-embedding-space-v1",
            )
            if status["ready"]:
                final_ready.append(label)
        assert len(final_ready) == 1
        assert observed_ready & {"a", "b"}


if __name__ == "__main__":
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print("PASS", name)
