"""Run: python3 tests/test_phonetics.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tdxf.phonetics import blocking_rows, codes, tokens

FAILURES = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"          expected: {expected!r}\n          actual:   {actual!r}")
        FAILURES.append(label)


def blocks(a, b):
    return bool(set(codes(a)) & set(codes(b)))


def main():
    print("\n[tokens]")
    check("stopwords and entity suffixes dropped", tokens("The NUVANA Brands, Inc."), ["NUVANA"])
    check("bare digits dropped", tokens("ZONE 5"), ["ZONE"])

    print("\n[blocking] pairs a phonetic key must catch")
    for a, b in [("NOOVANA", "NUVANA"), ("NUVAHNA", "NUVANA"),
                 ("PHOTOZONE", "FOTOZONE"), ("KLARNA", "CLARNA"), ("LYFT", "LIFT")]:
        check(f"{a} ~ {b}", blocks(a, b), True)

    print("\n[blocking] multi-word marks must still meet")
    check("NUVANA LABS ~ NOOVANA", blocks("NUVANA LABS", "NOOVANA"), True)

    print("\n[blocking] unrelated marks must NOT block")
    for a, b in [("APPLE", "ORANGE"), ("NUVANA", "PUMPCO"), ("ZENITH", "MARLOW")]:
        check(f"{a} !~ {b}", blocks(a, b), False)

    print("\n[limits] documented gaps, not bugs")
    check("SOLEIL !~ SOLAY (phonetic alone misses; trigram path covers it)",
          blocks("SOLEIL", "SOLAY"), False)

    print("\n[rows] pseudo marks widen recall for free")
    rows = blocking_rows("97000001", {"mark": ["NUVAHNA"], "pseudo_mark": ["NUVANA"]})
    check("both sources indexed", sorted({r[2] for r in rows}), ["mark", "pseudo_mark"])
    check("shared code with NOOVANA", "NFN" in {r[1] for r in rows}, True)

    print(f"\n{'ALL CHECKS PASSED' if not FAILURES else f'{len(FAILURES)} FAILURE(S)'}\n")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
