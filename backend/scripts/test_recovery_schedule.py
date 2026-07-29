import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import recovery_schedule


def _fresh():
    recovery_schedule.reset_for_tests()
    return recovery_schedule.RecoverySchedule()


def test_pops_most_recently_active_first() -> None:
    s = _fresh()
    s.push("old", ["old"], ("2026-07-01T00:00:00Z", 1.0))
    s.push("newest", ["newest"], ("2026-07-29T00:00:00Z", 1.0))
    s.push("mid", ["mid"], ("2026-07-15T00:00:00Z", 1.0))
    assert [s.pop()[0] for _ in range(3)] == ["newest", "mid", "old"]
    assert s.pop() is None


def test_boost_makes_session_the_next_pop() -> None:
    s = _fresh()
    recovery_schedule.set_active(s)
    for i, key in enumerate(["a", "b", "c", "d"]):
        s.push(key, [key], (f"2026-07-0{i + 1}T00:00:00Z", 1.0))
    # Without a boost "d" is newest and pops first.
    assert s.pop()[0] == "d"
    # User opens "a", the oldest bucket — it must be next.
    recovery_schedule.boost("a")
    assert s.pop()[0] == "a"
    # Remaining order is unchanged.
    assert [s.pop()[0] for _ in range(2)] == ["c", "b"]


def test_boost_before_push_is_remembered() -> None:
    s = _fresh()
    recovery_schedule.set_active(s)
    recovery_schedule.boost("late")
    s.push("newest", ["newest"], ("2026-07-29T00:00:00Z", 1.0))
    s.push("late", ["late"], ("2026-01-01T00:00:00Z", 1.0))
    assert s.pop()[0] == "late"
    assert recovery_schedule.is_boosted("late")


def test_boost_of_taken_bucket_is_a_noop() -> None:
    s = _fresh()
    recovery_schedule.set_active(s)
    s.push("a", ["a"], ("2026-07-01T00:00:00Z", 1.0))
    assert s.pop()[0] == "a"
    assert s.boost("a") is False
    assert s.pop() is None


def test_each_bucket_pops_exactly_once_after_repeated_boosts() -> None:
    s = _fresh()
    recovery_schedule.set_active(s)
    for i in range(5):
        s.push(f"s{i}", [i], (f"2026-07-0{i + 1}T00:00:00Z", 1.0))
    for _ in range(3):
        recovery_schedule.boost("s0")
    popped = []
    while True:
        got = s.pop()
        if got is None:
            break
        popped.append(got[0])
    assert popped[0] == "s0"
    assert sorted(popped) == ["s0", "s1", "s2", "s3", "s4"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    recovery_schedule.reset_for_tests()
    print("PASS")
