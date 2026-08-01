from __future__ import annotations

import hashlib
import itertools

import transcript_text_collapse as ttc


# --- normalize_repeated_text -------------------------------------------------

def test_normalize_collapses_runs_and_strips():
    assert ttc.normalize_repeated_text("  a   b  ") == "a b"


def test_normalize_handles_tabs_and_newlines():
    assert ttc.normalize_repeated_text("a\t\nb") == "a b"


def test_normalize_empty_and_whitespace_only():
    assert ttc.normalize_repeated_text("") == ""
    assert ttc.normalize_repeated_text("   ") == ""


def test_normalize_passthrough_single_token():
    assert ttc.normalize_repeated_text("a") == "a"


# --- hash_text ---------------------------------------------------------------

def test_hash_text_matches_sha256_surrogatepass():
    assert ttc.hash_text("abc") == hashlib.sha256(b"abc").hexdigest()


def test_hash_text_is_hex_64():
    assert len(ttc.hash_text("anything")) == 64
    assert all(c in "0123456789abcdef" for c in ttc.hash_text("anything"))


def test_hash_text_empty():
    assert ttc.hash_text("") == hashlib.sha256(b"").hexdigest()


def test_hash_text_lone_surrogate_uses_surrogatepass():
    # Lone surrogate: strict utf-8 would raise; surrogatepass must encode it.
    value = "\udcff"
    assert ttc.hash_text(value) == hashlib.sha256(
        value.encode("utf-8", errors="surrogatepass")
    ).hexdigest()


# --- common_prefix_len -------------------------------------------------------

def test_common_prefix_basic():
    assert ttc.common_prefix_len("abcde", "abxyz") == 2


def test_common_prefix_empty_either_side():
    assert ttc.common_prefix_len("", "abc") == 0
    assert ttc.common_prefix_len("abc", "") == 0


def test_common_prefix_full_and_extension():
    assert ttc.common_prefix_len("abc", "abc") == 3
    assert ttc.common_prefix_len("abc", "abcd") == 3


# --- raw_index_after_normalized_prefix_checked -------------------------------

def test_raw_checked_leading_whitespace_skipped():
    # normalized "ab", prefix_len 1 reached at 'a' → raw index after 'a' is 3
    assert ttc.raw_index_after_normalized_prefix_checked("  ab", 1) == (3, True)


def test_raw_checked_internal_whitespace_collapses():
    # normalized "a b", prefix_len 2 reached after the space → 'b' at raw idx 4
    assert ttc.raw_index_after_normalized_prefix_checked("a   b", 2) == (4, True)


def test_raw_checked_trailing_whitespace_completes_prefix():
    # normalized "a" (trailing ws dropped) len 1 < 2 → not reached; raw = len
    # but here prefix_len 1 is reached by 'a', and trailing ws is a 2nd
    # normalized char completing prefix_len 2 → reached at end of text.
    assert ttc.raw_index_after_normalized_prefix_checked("a  ", 2) == (3, True)


def test_raw_checked_text_shorter_than_prefix():
    assert ttc.raw_index_after_normalized_prefix_checked("ab", 5) == (2, False)


def test_raw_checked_empty_text():
    assert ttc.raw_index_after_normalized_prefix_checked("", 1) == (0, False)
    # prefix_len 0 on empty: reached is len(normalized) >= 0 → True
    assert ttc.raw_index_after_normalized_prefix_checked("", 0) == (0, True)


def test_raw_checked_prefix_len_zero_returns_first_nonspace():
    assert ttc.raw_index_after_normalized_prefix_checked("ab", 0) == (0, True)
    assert ttc.raw_index_after_normalized_prefix_checked("  ab", 0) == (2, True)


def test_raw_checked_whitespace_only_text_not_reached():
    # leading whitespace never emits; no normalized chars produced
    assert ttc.raw_index_after_normalized_prefix_checked("   ", 1) == (3, False)


def test_raw_checked_nonspace_return_points_at_following_space():
    # 'a' reaches prefix_len 1 → returns index+1 = 1, which is the space char
    assert ttc.raw_index_after_normalized_prefix_checked("a b", 1) == (1, True)


def test_raw_checked_wrapper_matches_checked_index():
    for text, pl in [("a   b", 2), ("  ab", 1), ("ab", 5), ("", 0)]:
        assert ttc.raw_index_after_normalized_prefix(text, pl) == ttc.raw_index_after_normalized_prefix_checked(text, pl)[0]


# --- brute-force consistency invariant ---------------------------------------

def test_raw_wrapper_always_equals_checked_index_bruteforce():
    alphabet = "ab \t\n"
    for length in range(0, 6):
        for combo in itertools.product(alphabet, repeat=length):
            text = "".join(combo)
            for pl in range(0, length + 2):
                raw = ttc.raw_index_after_normalized_prefix(text, pl)
                assert raw == ttc.raw_index_after_normalized_prefix_checked(text, pl)[0]
