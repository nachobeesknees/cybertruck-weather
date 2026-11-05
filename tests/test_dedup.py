import random

from argusvue.collect.dedup import hamming, is_near_duplicate, simhash64, to_signed, to_unsigned

_VOCAB_A = ["council", "approved", "rezoning", "request", "apartment", "complex", "units", "corridor", "demand", "campus", "suppliers", "developer", "ground", "spring", "deliver", "filings", "planning", "department", "hearing", "residents", "vote", "zoning", "permits", "construction"]
_VOCAB_B = ["commissioners", "workforce", "deed", "restriction", "lottery", "lodging", "tax", "collections", "visitors", "airport", "enplanements", "snowpack", "pass", "closure", "wages", "housing", "waitlist", "season", "tourism", "bookings", "occupancy", "treasurer"]


def _article(vocab, n=400, seed=1):
    r = random.Random(seed)
    return " ".join(r.choice(vocab) for _ in range(n))


BASE = _article(_VOCAB_A)


def test_identical_text_distance_zero():
    assert hamming(simhash64(BASE), simhash64(BASE)) == 0


def test_small_edit_is_near_duplicate():
    words = BASE.split()
    words[5], words[40] = "tuesday", "forty"
    assert is_near_duplicate(simhash64(BASE), simhash64(" ".join(words)))


def test_syndicated_copy_with_dateline_and_copyright_is_near():
    copy = "PHOENIX (AP) - " + BASE + " Copyright 2026 The Associated Press. All rights reserved."
    assert hamming(simhash64(BASE), simhash64(copy)) <= 3


def test_unrelated_article_is_far():
    assert hamming(simhash64(BASE), simhash64(_article(_VOCAB_B, seed=2))) > 10


def test_same_topic_different_article_is_far():
    assert hamming(simhash64(BASE), simhash64(_article(_VOCAB_A, seed=3))) > 3


def test_signed_roundtrip_and_empty():
    for u in (0, 1, (1 << 63), (1 << 64) - 1, 0xDEADBEEFCAFEBABE):
        s = to_signed(u)
        assert -(1 << 63) <= s < (1 << 63)
        assert to_unsigned(s) == u
    assert simhash64("") == 0
