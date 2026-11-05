"""Near-duplicate detection: 64-bit simhash, Hamming distance <= 3 (SPEC §6).

Features are term-frequency-weighted word unigrams and bigrams of the extracted body text. On article-
length text this keeps syndicated copies (dateline, copyright, minor edits) within 3 bits while
unrelated articles sit 20-35 bits apart. Short texts (< ~200 chars) are not simhashed at all; and the
store only compares documents fetched within a few days of each other, so templated monthly reports
(same words, new numbers, a month apart) are never collapsed.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter

_WORD = re.compile(r"[a-z0-9]+")


def features(text: str) -> Counter[str]:
    toks = _WORD.findall(text.lower())
    c: Counter[str] = Counter(toks)
    c.update(" ".join(toks[i:i + 2]) for i in range(len(toks) - 1))
    return c


def simhash64(text: str) -> int:
    """Unsigned 64-bit simhash. Empty text hashes to 0."""
    feats = features(text)
    if not feats:
        return 0
    v = [0] * 64
    for f, w in feats.items():
        h = int.from_bytes(hashlib.blake2b(f.encode("utf-8"), digest_size=8).digest(), "big")
        for i in range(64):
            v[i] += w if (h >> i) & 1 else -w
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= 1 << i
    return out


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & ((1 << 64) - 1)).bit_count()


def to_signed(u: int) -> int:
    """Postgres bigint is signed; store the unsigned simhash as two's complement."""
    return u - (1 << 64) if u >= (1 << 63) else u


def to_unsigned(s: int) -> int:
    return s + (1 << 64) if s < 0 else s


def is_near_duplicate(a: int, b: int, max_distance: int = 3) -> bool:
    return hamming(a, b) <= max_distance
