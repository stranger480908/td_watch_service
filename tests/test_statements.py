"""Run: python3 tests/test_statements.py"""

import io
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tdxf.parser import iter_case_files

FAILURES: list[str] = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"          expected: {expected!r}")
        print(f"          actual:   {actual!r}")
        FAILURES.append(label)


XML = """<?xml version="1.0" encoding="UTF-8"?>
<trademark-applications-daily>
<application-information><file-segments><file-segment>TRMK</file-segment>
<action-keys><action-key>00</action-key>
<case-file>
  <serial-number>97100001</serial-number>
  <case-file-header>
    <mark-identification>VERYLONGMARKNAMETHATRUNSPASTTHEFIELDWIDTH</mark-identification>
    <published-for-opposition-date>20260901</published-for-opposition-date>
    <status-code>686</status-code>
  </case-file-header>
  <case-file-statements>
    <case-file-statement>
      <type-code>GS0251</type-code>
      <text>Shirts; hats; jackets</text>
    </case-file-statement>
    <case-file-statement>
      <type-code>GS0252</type-code>
      <text>Trousers; belts</text>
    </case-file-statement>
    <case-file-statement>
      <type-code>GS0093</type-code>
      <text>Software ((and modems)) for *mobile* devices [obsolete units]</text>
    </case-file-statement>
    <case-file-statement>
      <type-code>TLIT00</type-code>
      <text>The non-Latin characters transliterate to KAIYUN.</text>
    </case-file-statement>
    <case-file-statement>
      <type-code>MK0000</type-code>
      <text>ANDKEEPSGOING</text>
    </case-file-statement>
    <case-file-statement>
      <type-code>D00000</type-code>
      <text>No claim is made to APPAREL.</text>
    </case-file-statement>
  </case-file-statements>
  <case-file-event-statements>
    <case-file-event-statement>
      <code>1000</code><type>T</type>
      <description-text>REQ. FOR EXTENSION OF TIME TO OPPOSE</description-text>
      <date>20260910</date><number>003</number>
    </case-file-event-statement>
    <case-file-event-statement>
      <code>1011</code><type>T</type>
      <description-text>EXTENSION OF TIME TO OPPOSE GRANTED</description-text>
      <date>20260912</date><number>004</number>
    </case-file-event-statement>
    <case-file-event-statement>
      <code>CNSA</code><type>O</type>
      <description-text>NOTICE OF PUBLICATION</description-text>
      <date>20260812</date><number>002</number>
    </case-file-event-statement>
  </case-file-event-statements>
  <classifications>
    <classification><international-code>025</international-code>
    <international-code>009</international-code><primary-code>025</primary-code></classification>
  </classifications>
</case-file>
</action-keys></file-segments></application-information>
</trademark-applications-daily>
"""


def main() -> int:
    cf = list(iter_case_files(io.BytesIO(XML.encode())))[0]

    print("\n[goods] removed-goods handling")
    check("GS flag 2 statement dropped", [c for c, _ in cf.goods_services], ["025", "009"])
    check(
        "embedded ((less goods)) and [deleted] stripped, *added* kept",
        dict(cf.goods_services)["009"],
        "Software for mobile devices",
    )
    check("raw view still has all three", len(cf.raw_goods_services), 3)
    check(
        "deleted goods absent from scoring text",
        "modems" in cf.goods_services_text or "Trousers" in cf.goods_services_text,
        False,
    )

    print("\n[statements] four-character prefixes")
    check("TLIT recognised", len(cf.transliterations), 1)
    check("TLIT not mis-bucketed as TR", cf.translations, [])
    check("disclaimer code is D0 not DC", [s.kind for s in cf.statements].count("D0"), 1)

    print("\n[mark] overflow")
    check("MK captured", cf.mark_overflow, ["ANDKEEPSGOING"])
    check(
        "full mark text reassembled",
        cf.full_mark_text,
        "VERYLONGMARKNAMETHATRUNSPASTTHEFIELDWIDTH ANDKEEPSGOING",
    )

    print("\n[events] prosecution history")
    check("all events parsed", len(cf.events), 3)
    check("event date typed", cf.events[0].date, date(2026, 9, 10))
    ext = cf.opposition_extension_events
    check("extension-to-oppose codes isolated", [e.code for e in ext], ["1000", "1011"])
    check("non-numeric event code tolerated", cf.events[2].code, "CNSA")

    print("\n[signal] publication flag")
    check("action-key 00 latched", cf.action_key, "00")
    check("status 686 published for opposition", cf.status_code, "686")

    print(f"\n{'ALL CHECKS PASSED' if not FAILURES else f'{len(FAILURES)} FAILURE(S)'}\n")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
