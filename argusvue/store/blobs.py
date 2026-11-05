"""Content-addressed blob storage with an S3-shaped interface (put/get/open/exists by key).

Local-disk implementation under data/snapshots/. Keys look like `sha256/ab/cd/<hex><suffix>`;
`blob_ref` values stored in the DB are `local://<key>` so a later S3 backend can be `s3://bucket/<key>`.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from argusvue.settings import get_settings


@dataclass(frozen=True)
class BlobRef:
    key: str
    sha256: str
    size: int

    @property
    def ref(self) -> str:
        return f"local://{self.key}"


class BlobStore:
    scheme = "local"

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else get_settings().snapshots_dir
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(exist_ok=True)

    # ---- keys -------------------------------------------------------------------------------
    @staticmethod
    def key_for(sha256: str, suffix: str = "") -> str:
        return f"sha256/{sha256[:2]}/{sha256[2:4]}/{sha256}{suffix}"

    def path(self, key: str) -> Path:
        key = key.removeprefix("local://")
        return self.root / key

    def exists(self, key: str) -> bool:
        return self.path(key).exists()

    # ---- writes -----------------------------------------------------------------------------
    def put_bytes(self, data: bytes, *, suffix: str = "") -> BlobRef:
        return self.put_stream([data], suffix=suffix)

    def put_stream(self, chunks: Iterable[bytes], *, suffix: str = "") -> BlobRef:
        """Stream chunks to a temp file while hashing, then move into the content-addressed path.
        Identical content lands on the same key, so re-fetching never duplicates bytes."""
        h = hashlib.sha256()
        size = 0
        tmp = self.root / "tmp" / f"{id(h)}-{hashlib.sha1(str(self.root).encode()).hexdigest()[:6]}.part"
        with tmp.open("wb") as f:
            for c in chunks:
                h.update(c)
                size += len(c)
                f.write(c)
        digest = h.hexdigest()
        key = self.key_for(digest, suffix)
        dest = self.path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            tmp.unlink()
        else:
            tmp.replace(dest)
        return BlobRef(key=key, sha256=digest, size=size)

    def put_file(self, src: Path, *, suffix: str | None = None) -> BlobRef:
        suffix = src.suffix if suffix is None else suffix
        with src.open("rb") as f:
            return self.put_stream(iter(lambda: f.read(1 << 20), b""), suffix=suffix)

    # ---- reads ------------------------------------------------------------------------------
    def get_bytes(self, key: str) -> bytes:
        return self.path(key).read_bytes()

    @contextmanager
    def open(self, key: str) -> Iterator[BinaryIO]:
        with self.path(key).open("rb") as f:
            yield f

    def copy_to(self, key: str, dest: Path) -> Path:
        shutil.copyfile(self.path(key), dest)
        return dest
