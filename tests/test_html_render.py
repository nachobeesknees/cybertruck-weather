"""The briefing page re-presents the published report: parser, footnote numbering, formatting (no DB)."""
from __future__ import annotations

from datetime import date

from argusvue.render.html import (
    Citer,
    compact,
    falsifier_sentence,
    fmt_value,
    parse_intel_markdown,
    plain_indicator,
    sparkline,
)

SAMPLE = """# Jackson, WY-ID: intelligence report, 2026-11-05

> **Record receipts, thin market**

> Every sentence below cites the ledger.

## Bottom line

Receipts rose 30% [i:sales_use_tax_distribution:2026-10]. Sales were thin [i:homes_sold:2026-09] [c:145247].

## What moved

- Tax up [i:sales_use_tax_distribution:2026-10].
- Plan approved [c:145229] [c:145230].

## Signals

## Risks and contradictions

- Two reports disagree [c:145246] [c:145255].

## Watchlist

- December 7 hearing [c:145315].

## Note for the operator

> First cycle complete.

## Theses

- **jh-x** (`county:56039`): stuff [c:145229].

## Key indicators

| indicator | geo | period | latest | MoM | YoY | z | tag |
|---|---|---|---|---|---|---|---|
| Sales tax | `county:56039` | 2026-10 | 16,540,015.61 USD | +37.3% | +30.5% | +3.26 | [i:sales_use_tax_distribution:2026-10] |

## Evidence cited in the report

- `[c:145229]` DEV2026-0002: policy.decision = approved — "APPROVED" (Regular Meeting, tetoncountywy.gov, observed 2026-11-05) [c:145229]
- `[c:145247]` Allure: housing.median_sale_price = 2900000 USD — "- Median Sold Price: $2,900,000" (Report, allurebrokerage.com, observed 2026-11-05) [c:145247]
"""


def test_parse_intel_markdown_reads_every_part():
    r = parse_intel_markdown(SAMPLE)
    assert r.day == "2026-11-05" and r.headline == "Record receipts, thin market"
    assert r.bottom_line.startswith("Receipts rose 30%") and "[c:145247]" in r.bottom_line
    assert r.sections["What moved"] == ["Tax up [i:sales_use_tax_distribution:2026-10].", "Plan approved [c:145229] [c:145230]."]
    assert r.sections["Signals"] == [] and r.sections["Watchlist"] == ["December 7 hearing [c:145315]."]
    assert r.note == "First cycle complete."
    assert r.indicator_rows[0][0] == "Sales tax" and r.indicator_rows[0][3] == "16,540,015.61 USD"
    assert r.claim_ids == [145229, 145247]


def test_citer_numbers_tags_in_order_and_links_them():
    seen = []

    def resolve(key):
        seen.append(key)
        return f"label {key}", f'tip "{key}"', None, ""

    c = Citer("jackson", resolve)
    html = c.render("Receipts rose 30% [i:sales_use_tax_distribution:2026-10]. Sales were thin [i:homes_sold:2026-09] [c:145247].")
    assert '<a class="cite" href="#jackson-src-1"' in html and 'href="#jackson-src-3"' in html
    assert html.count('class="cite"') == 3 and "[c:" not in html and "[i:" not in html
    assert "Receipts rose 30%.<sup" in html and "</sup>." not in html      # footnote numbers follow the full stop, no stray space
    assert "&quot;i:sales_use_tax_distribution:2026-10&quot;" in html       # tooltip text is attribute-escaped
    again = c.render("Again [c:145247].")
    assert 'href="#jackson-src-3"' in again and len(c.sources) == 3           # same source keeps its number
    assert seen == ["i:sales_use_tax_distribution:2026-10", "i:homes_sold:2026-09", "c:145247"]


def test_value_formatting_and_sentences():
    assert fmt_value(1437098.18, "USD") == "$1.44M" and fmt_value(441097.1, "USD") == "$441K" and fmt_value(1720.25, "USD") == "$1,720"
    assert fmt_value(4.2, "pct") == "4.2%" and fmt_value(0.3195, "pct") in ("31.9%", "32.0%") and fmt_value(2475.2, "thousand_jobs") == "2,475,200"
    assert fmt_value(4.6, "months") == "4.6 mo" and compact(26399) == "26,399" and compact(53905) == "53,905" and compact(150790) == "151K"
    f = {"action": "downgrade", "operator": "<", "threshold": "1815.17", "by_date": date(2027, 8, 31), "period": date(2027, 6, 1)}
    s = falsifier_sentence(f, plain_indicator("zori_rent", "Zillow Observed Rent Index (smoothed)"), "ZIP 85085", "USD")
    assert s == "We lower our confidence if the Zillow rent index in ZIP 85085 is below $1,815 in Jun 2027, by Aug 31, 2027."
    assert plain_indicator("snotel_swe_grand_targhee", "Snow water equivalent, end of month, Grand Targhee SNOTEL (1082)") == "Snow water equivalent, end of month, Grand Targhee SNOTEL"
    svg = sparkline([1.0, 2.0, 3.0])
    assert svg.startswith("<svg") and "spark-line" in svg and 'r="4"' in svg and sparkline([]) == ""
