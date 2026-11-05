from argusvue.store.blobs import BlobStore


def test_blobstore_is_content_addressed_and_dedups(tmp_path):
    s = BlobStore(tmp_path)
    a = s.put_bytes(b"hello", suffix=".txt")
    b = s.put_bytes(b"hello", suffix=".txt")
    c = s.put_stream([b"hel", b"lo"], suffix=".txt")
    assert a == b == c
    assert a.size == 5 and a.ref.startswith("local://sha256/")
    assert s.exists(a.key) and s.get_bytes(a.key) == b"hello"
    assert s.path(a.ref) == s.path(a.key)
    assert len(list((tmp_path / "tmp").iterdir())) == 0        # no leftover temp files
    d = s.put_bytes(b"other")
    assert d.key != a.key
