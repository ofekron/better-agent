import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bff_chat_cache import CachedProjection, ChatProjectionCache


def entry(root, weight):
    return CachedProjection(root, 0, 0, [], {"root": root}, None, weight)


def test_cache_enforces_root_and_byte_limits():
    cache = ChatProjectionCache(max_roots=2, max_bytes=10)
    cache.put(entry("a", 4))
    cache.put(entry("b", 4))
    assert cache.get("a") is not None
    cache.put(entry("c", 4))
    assert cache.get("b") is None
    assert cache.stats()["roots"] == 2
    cache.put(entry("large", 11))
    assert cache.get("large") is None


if __name__ == "__main__":
    test_cache_enforces_root_and_byte_limits()
    print("BFF chat cache tests passed")
