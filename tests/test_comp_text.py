"""Posted-range text extraction (radar/parse/comp.py).

Pins a real case (2026-08-31): "Pay Range: $32.19-$53.68/hour" sat in a fetched description
while the row ranked #2 in the queue off a $139k LCA prior — plain hourly decimals matched no
_NUM alternative, so the whole hourly path was unreachable.
"""

from radar.parse.comp import annualize, extract_posted_range

# The exact pay-range sentence from a real "New Grad 2027 - Software Engineer" posting, as
# rendered into description_md. Pinned like the D64 Notion sentence: do not paraphrase.
HOURLY_SENTENCE = (
    "**Pay Range:**\n\n$32.19-$53.68/hour\n\nActual base salary varies based on factors"
)


def test_hourly_range_is_extracted_and_annualized():
    pr = extract_posted_range(HOURLY_SENTENCE)
    assert pr is not None, "hourly decimal range must be extractable"
    assert pr.interval == "hour"
    assert (pr.min, pr.max) == (32.19, 53.68)
    assert annualize(pr.min, pr.interval) == 32.19 * 2080
    assert annualize(pr.max, pr.interval) == 53.68 * 2080
    assert pr.confidence >= 0.7  # "Pay Range" + "/hour" + "$" — enough to count as posted


def test_bare_hourly_ints_with_hourly_context():
    pr = extract_posted_range("pays $20 to $25 an hour depending on experience")
    assert pr is not None and pr.interval == "hour" and (pr.min, pr.max) == (20.0, 25.0)


def test_existing_annual_formats_still_extract():
    pr = extract_posted_range("salary range: $110,000 - $135,000 per year")
    assert pr is not None and pr.interval == "year" and (pr.min, pr.max) == (110000.0, 135000.0)
    pr = extract_posted_range("base pay 62.5k-75k")
    assert pr is not None and (pr.min, pr.max) == (62500.0, 75000.0)


def test_small_number_pairs_without_hourly_context_are_rejected():
    # The new plain 2-3 digit alternative must not turn prose ranges into comp.
    for text in (
        "requires 10-15 years of experience",
        "posted 2026-08-18 through 2026-09-21",
        "bonus of 10 - 20% of base",
        "40 hours per week, 5-8 hour shifts",
        "supporting 25-50 client accounts",
    ):
        assert extract_posted_range(text) is None, text
