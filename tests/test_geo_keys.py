import pytest

from argusvue.geo.keys import (
    LEVELS,
    GeoKey,
    GeoKeyError,
    code_parent,
    h3_keys,
    is_valid_key,
    make_key,
    parse_key,
)


@pytest.mark.parametrize(
    "s,level,code",
    [
        ("country:US", "country", "US"),
        ("state:04", "state", "04"),
        ("cbsa:38060", "cbsa", "38060"),
        ("county:04013", "county", "04013"),
        ("place:0455000", "place", "0455000"),
        ("sub:phx-north-tsmc", "sub", "phx-north-tsmc"),
        ("zcta:85085", "zcta", "85085"),
        ("tract:04013101001", "tract", "04013101001"),
        ("parcel:04013:211-12-345", "parcel", "04013:211-12-345"),
        ("h3:872a1072bffffff", "h3", "872a1072bffffff"),
    ],
)
def test_parse_valid(s, level, code):
    k = parse_key(s)
    assert (k.level, k.code) == (level, code)
    assert str(k) == s
    assert is_valid_key(s)


@pytest.mark.parametrize(
    "s",
    [
        "zcta85085",            # no separator
        "zip:85085",            # unknown level
        "zcta:8508",            # wrong length
        "county:4013",          # missing leading zero
        "place:55000",          # must be state+place
        "tract:0401310100",     # 10 digits
        "sub:Phx North",        # not slug
        "sub:-phx",             # leading dash
        "h3:zzzzzzzzzzzzzzz",   # not hex
        "h3:8f2a1072bffffff",   # hex but not a valid cell
        "state:4",
        "",
    ],
)
def test_parse_invalid(s):
    with pytest.raises(GeoKeyError):
        parse_key(s)
    assert not is_valid_key(s)


def test_level_order_is_broad_to_fine():
    assert LEVELS.index("country") < LEVELS.index("state") < LEVELS.index("cbsa") < LEVELS.index("county")
    assert LEVELS.index("county") < LEVELS.index("place") < LEVELS.index("sub") < LEVELS.index("zcta")
    assert LEVELS.index("zcta") < LEVELS.index("tract") < LEVELS.index("parcel")
    assert LEVELS[-1] == "h3"
    assert GeoKey("state", "04").is_coarser_than(GeoKey("zcta", "85085"))
    assert not GeoKey("tract", "04013101001").is_coarser_than(GeoKey("county", "04013"))


def test_make_key_roundtrip():
    assert make_key("zcta", "85085") == "zcta:85085"
    with pytest.raises(GeoKeyError):
        make_key("zcta", "ABCDE")


def test_code_parent_structural():
    assert code_parent(parse_key("tract:04013101001")) == GeoKey("county", "04013")
    assert code_parent(parse_key("parcel:56039:22-41-16-33-1-00-001")) == GeoKey("county", "56039")
    assert code_parent(parse_key("county:56039")) == GeoKey("state", "56")
    assert code_parent(parse_key("state:56")) == GeoKey("country", "US")
    assert code_parent(parse_key("zcta:85085")) is None   # needs geometry


def test_h3_keys_three_resolutions():
    keys = h3_keys(33.78, -112.15)
    assert len(keys) == 3
    assert all(is_valid_key(k) for k in keys)
    assert [parse_key(k).level for k in keys] == ["h3", "h3", "h3"]
    # resolution 7 cell is a parent of the resolution 8 cell
    import h3

    assert h3.cell_to_parent(parse_key(keys[1]).code, 7) == parse_key(keys[0]).code
