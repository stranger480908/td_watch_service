"""Run: python3 tests/test_pipeline.py"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tdxf.parser import iter_case_files
from tdxf.diff import DictPriorState, EventType, MarkState, diff_stream, summarise

HERE = Path(__file__).parent
FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    ok = actual == expected
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"          expected: {expected!r}")
        print(f"          actual:   {actual!r}")
        FAILURES.append(label)


def load(name: str):
    with open(HERE / name, "rb") as fh:
        return list(iter_case_files(fh))


def main() -> int:
    print("\n[parse] day1")
    day1 = load("day1.xml")
    check("record count", len(day1), 3)

    a = day1[0]
    check("serial number", a.serial_number, "97000001")
    check("mark text", a.mark_identification, "NUVAHNA")
    check("filing date", a.filing_date, date(2026, 8, 1))
    check("action key latched", a.action_key, "NA")
    check("attorney", a.attorney_name, "Dana R. Whitfield")
    check("standard characters flag", a.standard_characters_claimed, True)
    check("zero-filled pub date -> None", a.published_for_opposition_date, None)
    check("nice classes", a.nice_classes, ["003"])
    check("pseudo mark extracted", a.pseudo_marks, ["NUVANA"])
    check("correspondent lines", len(a.correspondent), 4)
    check("owner name", a.owner_names, ["Nuvahna Beauty LLC"])

    print("\n[parse] GS type-code -> class mapping")
    c = day1[2]
    check("multi-class", c.nice_classes, ["003", "035"])
    check(
        "goods split by class",
        [(k, t[:21]) for k, t in c.goods_services],
        [("003", "Perfumes; essential o"), ("035", "Retail store services")],
    )
    check("translation captured", len(c.translations), 1)
    check("owner nationality", c.owners[0].nationality, "FR")

    print("\n[diff] day1 against empty state")
    state = DictPriorState()
    events1 = list(diff_stream(day1, state, observed_on=date(2026, 8, 26)))
    check("all new", summarise(events1), {"NEW_APPLICATION": 3})
    check("no premature deadlines", [e for e in events1 if e.opposition_deadline], [])
    for cf in day1:
        state.apply(MarkState.from_case_file(cf))

    print("\n[diff] day2 against day1 state")
    day2 = load("day2.xml")
    events2 = list(diff_stream(day2, state, observed_on=date(2026, 8, 27)))
    check(
        "event mix",
        summarise(events2),
        {
            "CLASSES_CHANGED": 1,
            "GOODS_SERVICES_CHANGED": 1,
            "NEW_APPLICATION": 1,
            "OWNER_CHANGED": 1,
            "PUBLISHED_FOR_OPPOSITION": 2,
            "STATUS_CHANGED": 1,
        },
    )

    unchanged = [e for e in events2 if e.serial_number == "97000002"]
    check("unchanged record emits nothing", unchanged, [])

    pub = next(
        e
        for e in events2
        if e.serial_number == "97000001"
        and e.event_type is EventType.PUBLISHED_FOR_OPPOSITION
    )
    check("opposition deadline", pub.opposition_deadline, date(2026, 10, 1))
    check("days remaining", pub.days_to_deadline, 35)

    backfill = [e for e in events2 if e.serial_number == "97000004"]
    check("first sighting emits both events", len(backfill), 2)
    check(
        "already-published deadline preserved",
        next(e.opposition_deadline for e in backfill if e.opposition_deadline),
        date(2026, 9, 24),
    )

    print("\n[alert] subject line rendering")
    subject = (
        f"Opposition deadline in {pub.days_to_deadline} days: "
        f"{pub.case_file.mark_identification} "
        f"(Cl. {', '.join(pub.case_file.nice_classes)})"
    )
    check(
        "subject",
        subject,
        "Opposition deadline in 35 days: NUVAHNA (Cl. 003)",
    )

    print(f"\n{'ALL CHECKS PASSED' if not FAILURES else f'{len(FAILURES)} FAILURE(S)'}\n")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
