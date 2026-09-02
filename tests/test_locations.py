import pytest

from radar.parse.locations import parse_locations, summarize_locations

CASES = [
    ("New York, NY", "metro", "new_york", "NY", "US", "ny_nyc"),
    ("McLean, VA", "metro", "washington_dc", "VA", "US", "va"),
    ("Seattle, Washington", "metro", "seattle", "WA", "US", "wa"),
    ("Washington, DC", "metro", "washington_dc", "DC", "US", "dc"),
    ("Jersey City, NJ", "metro", "new_york", "NJ", "US", "nj"),
    ("Bethesda, MD", "metro", "washington_dc", "MD", "US", "md"),
    ("O'Fallon, Missouri", "metro", "st_louis", "MO", "US", "mo"),
    ("US-NY-New York", "metro", "new_york", "NY", "US", "ny_nyc"),
    ("Menlo Park, CA", "metro", "san_francisco", "CA", "US", "ca"),
    ("Columbus, GA", "us_unknown", None, "GA", "US", "ga"),
    ("Columbus, OH", "metro", "columbus", "OH", "US", "oh"),
    ("Paris, TX", "us_unknown", None, "TX", "US", "tx"),
    ("Bangalore, IN", "international", None, None, "IN", None),
    ("Dublin, Ireland", "international", None, None, "IE", None),
    ("London, United Kingdom", "international", None, None, "GB", None),
    ("Toronto, ON", "international", None, None, "CA", None),
    ("Remote - US", "remote", None, None, "US", "remote"),
    ("Multiple Locations", "multiple", None, None, "US", "us_unknown"),
]


@pytest.mark.parametrize("raw,kind,metro,state,country,tax", CASES)
def test_parse_location(raw, kind, metro, state, country, tax):
    [loc] = parse_locations(raw)
    assert loc.kind == kind
    assert loc.metro == metro
    assert loc.state == state
    assert loc.country == country
    assert loc.tax_jurisdiction == tax


def test_multi_office_split_and_primary():
    locs = parse_locations("San Francisco, CA; New York, NY")
    assert [loc.metro for loc in locs] == ["san_francisco", "new_york"]
    s = summarize_locations(locs)
    assert s["primary_metro"] == "new_york"  # NYC is the operator's #1 → best office wins
    assert s["is_multiple_locations"] is True
    assert s["work_mode"] == "onsite"


def test_hybrid_and_remote_modes():
    assert summarize_locations(parse_locations("Austin, TX (Hybrid)"))["work_mode"] == "hybrid"
    assert summarize_locations(parse_locations("Remote"))["work_mode"] == "remote"
    assert (
        summarize_locations(parse_locations(["Israel, Yokneam"]))["is_international_only"] is True
    )
