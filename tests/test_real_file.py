"""
Checks that run against a real USPTO daily file, not hand-written fixtures.

Fixtures only prove the code agrees with itself: I wrote both the fixture and
the parser from the same assumptions. These checks prove the code agrees with
USPTO. Every threshold below was derived from apc260826.zip (43,175 records)
and is deliberately loose, so it catches a format change rather than normal
day-to-day variation.

Skips cleanly when no file is present, so CI stays green without one:

    aws s3 cp s3://tmwatch-raw-.../apc260826.zip data/raw/
    python tests/test_real_file.py
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tdxf.parser import iter_case_files_from_zip
from tdxf.phonetics import codes

NICE = {f"{i:03d}" for i in range(1, 46)}
FAILURES = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def main() -> int:
    files = sorted(Path("data/raw").glob("*.zip")) if Path("data/raw").exists() else []
    if not files:
        print("\nNo file in data/raw/, skipping real-data checks.\n")
        return 0

    path = files[0]
    print(f"\n[real data] {path.name}")

    n = 0
    stmt_prefix = Counter()
    gs_class = Counter()
    pub_weekday = Counter()
    marks_no_text = 0
    goods_chars = 0
    phonetic_hits = 0

    for cf in iter_case_files_from_zip(path):
        n += 1
        if not cf.mark_identification:
            marks_no_text += 1
        for s in cf.statements:
            tc = s.type_code or ""
            stmt_prefix[tc[:2]] += 1
            if tc.startswith("GS") and len(tc) >= 5:
                gs_class[tc[2:5]] += 1
        for _, desc in cf.goods_services:
            goods_chars += len(desc)
        if cf.published_for_opposition_date:
            pub_weekday[cf.published_for_opposition_date.strftime("%a")] += 1
        if cf.mark_identification and codes(cf.mark_identification):
            phonetic_hits += 1

    print(f"  parsed {n:,} records")
    check("record count is plausible for a daily file", 5_000 < n < 200_000, f"{n:,}")
    check("nearly every record has mark text", marks_no_text / n < 0.10,
          f"{marks_no_text:,} missing")

    print("\n[goods] class extraction")
    nice_hits = sum(v for k, v in gs_class.items() if k in NICE)
    total_gs = sum(gs_class.values())
    check("GS codes carry a valid Nice class", nice_hits / total_gs > 0.95,
          f"{nice_hits:,}/{total_gs:,}")
    check("goods text is substantial", goods_chars / n > 100,
          f"{goods_chars // max(n,1)} chars/record")

    print("\n[statements] type codes present in v2.3")
    for code, floor in [("GS", 10_000), ("PM", 1_000), ("TR", 100), ("TL", 50)]:
        check(f"{code} statements present", stmt_prefix[code] >= floor,
              f"{stmt_prefix[code]:,}")

    print("\n[phonetics] blocking keys generate")
    check("most marks produce a phonetic code", phonetic_hits / n > 0.90,
          f"{phonetic_hits:,}/{n:,}")

    print("\n[cadence] publication concentration")
    if pub_weekday:
        top, count = pub_weekday.most_common(1)[0]
        share = count / sum(pub_weekday.values())
        check("publications concentrate on one weekday", share > 0.90,
              f"{top} {share:.0%}")
        check("that weekday is Tuesday (Official Gazette)", top == "Tue", top)

    print(f"\n{'ALL CHECKS PASSED' if not FAILURES else f'{len(FAILURES)} FAILURE(S)'}\n")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
