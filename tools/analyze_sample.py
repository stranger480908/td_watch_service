"""
Audit real USPTO daily files against the assumptions the pipeline rests on.

Deliberately does NOT import tdxf.parser. It reads the XML with raw lxml so it
can contradict the parser rather than inherit its assumptions.

    python tools/analyze_sample.py --dir data/raw/applications
    python tools/analyze_sample.py --dir tests            # fixtures, smoke test

Each section prints a verdict. Anything marked FIX means the parser is wrong
about real data and needs changing before the scoring engine is built on it.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path

from lxml import etree

VALID_CLASSES = {f"{i:03d}" for i in range(1, 46)} | {"200", "A", "B", "201", "202"}

LESS_GOODS = re.compile(r"\(\([^)]*\)\)")
NEW_WORDING = re.compile(r"\*[^*]+\*")
DELETED_GOODS = re.compile(r"\[[^\]]+\]")

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class Stats:
    def __init__(self) -> None:
        self.files = 0
        self.records = 0
        self.stmt_prefix = Counter()
        self.stmt_len = Counter()
        self.gs_flag = Counter()
        self.gs_class_valid = Counter()
        self.gs_vs_classification = Counter()
        self.gs_markers = Counter()
        self.text_multiplicity = Counter()
        self.text_chunk_len = Counter()
        self.action_keys = Counter()
        self.action_key_order = Counter()
        self.pub_by_weekday = Counter()
        self.pub_by_date = Counter()
        self.pub_per_file: dict[str, int] = {}
        self.oppose_events = Counter()
        self.has_events = Counter()
        self.field_present = Counter()
        self.status_codes = Counter()
        self.mark_overflow = 0


def _t(el) -> str | None:
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def _ct(parent, tag) -> str | None:
    return _t(parent.find(tag)) if parent is not None else None


def _pdate(raw: str | None) -> date | None:
    if not raw or len(raw) != 8 or not raw.isdigit() or set(raw) == {"0"}:
        return None
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None


def scan_stream(src, label: str, st: Stats) -> None:
    st.files += 1
    pubs = 0
    action_key = None
    saw_case_before_key = False

    ctx = etree.iterparse(
        src, events=("end",), tag=("action-key", "case-file"), recover=True
    )
    for _, el in ctx:
        if el.tag == "action-key":
            action_key = _t(el)
            st.action_keys[action_key or "<empty>"] += 1
        else:
            st.records += 1
            if action_key is None:
                saw_case_before_key = True

            h = el.find("case-file-header")
            if h is not None:
                for tag in (
                    "mark-identification",
                    "filing-date",
                    "status-code",
                    "published-for-opposition-date",
                    "attorney-name",
                    "registration-number",
                ):
                    if _ct(h, tag):
                        st.field_present[tag] += 1
                sc = _ct(h, "status-code")
                if sc:
                    st.status_codes[sc] += 1
                p = _pdate(_ct(h, "published-for-opposition-date"))
                if p:
                    pubs += 1
                    st.pub_by_weekday[WEEKDAYS[p.weekday()]] += 1
                    st.pub_by_date[p.isoformat()] += 1

            cls_codes = {
                t
                for t in (_t(x) for x in el.findall("classifications/classification/international-code"))
                if t
            }

            for s in el.findall("case-file-statements/case-file-statement"):
                code = _ct(s, "type-code") or ""
                st.stmt_len[len(code)] += 1
                prefix = code[:4] if code[:4] in ("TLIT", "TNSF") else code[:2]
                st.stmt_prefix[prefix] += 1
                if prefix == "MK":
                    st.mark_overflow += 1

                texts = [t for t in (_t(x) for x in s.findall("text")) if t]
                st.text_multiplicity[min(len(texts), 6)] += 1
                if len(texts) > 1:
                    for t in texts[:-1]:
                        st.text_chunk_len[len(t)] += 1

                if prefix == "GS" and len(code) >= 6:
                    cls, flag = code[2:5], code[5]
                    st.gs_flag[flag] += 1
                    st.gs_class_valid["valid" if cls in VALID_CLASSES else f"INVALID:{cls}"] += 1
                    if cls_codes:
                        st.gs_vs_classification[
                            "match" if cls in cls_codes else "mismatch"
                        ] += 1
                    body = " ".join(texts)
                    if LESS_GOODS.search(body):
                        st.gs_markers["((less goods))"] += 1
                    if NEW_WORDING.search(body):
                        st.gs_markers["*new wording*"] += 1
                    if DELETED_GOODS.search(body):
                        st.gs_markers["[deleted]"] += 1

            evs = el.findall("case-file-event-statements/case-file-event-statement")
            st.has_events["yes" if evs else "no"] += 1
            for ev in evs:
                c = _ct(ev, "code")
                if c and c.isdigit() and 1000 <= int(c) <= 1060:
                    st.oppose_events[c] += 1

        el.clear()
        parent = el.getparent()
        if parent is not None:
            while el.getprevious() is not None:
                del parent[0]

    st.action_key_order["case-file before any action-key" if saw_case_before_key else "ok"] += 1
    st.pub_per_file[label] = pubs


def scan_path(p: Path, st: Stats) -> None:
    if p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".xml"):
                    with zf.open(name) as fh:
                        scan_stream(io.BufferedReader(fh), p.name, st)
    else:
        with open(p, "rb") as fh:
            scan_stream(fh, p.name, st)


def top(c: Counter, n: int = 12) -> str:
    if not c:
        return "    (none)"
    return "\n".join(f"    {k!s:<28} {v:,}" for k, v in c.most_common(n))


def report(st: Stats) -> None:
    print(f"\n{'='*66}\n{st.files} file(s), {st.records:,} case-file records\n{'='*66}")

    print("\n[1] Statement type-code prefixes")
    print(top(st.stmt_prefix))
    print(f"  code lengths: {dict(st.stmt_len)}")
    tlit = st.stmt_prefix.get("TLIT", 0)
    print(
        f"  VERDICT: TLIT (transliteration) present {tlit:,} times. "
        + ("Parser must handle 4-char prefixes -- FIX" if tlit else "Absent in this sample.")
    )
    print(f"  MK (mark overflow) statements: {st.mark_overflow:,}"
          + ("  -- mark text may be truncated, FIX" if st.mark_overflow else ""))

    print("\n[2] GS type-code structure  (spec: GS + 3-digit class + 1 flag)")
    print(top(st.gs_class_valid, 6))
    print(f"  position-6 flag: {dict(st.gs_flag)}")
    print("    1 = goods text, no less-goods   2 = ONLY less-goods   3 = embedded less-goods")
    print(f"  GS class vs <classifications>: {dict(st.gs_vs_classification)}")
    bad = sum(v for k, v in st.gs_class_valid.items() if k.startswith("INVALID"))
    print(f"  VERDICT: {'slice [2:5] holds' if not bad else f'{bad:,} INVALID classes -- FIX'}")

    print("\n[3] Amendment markers inside goods text")
    print(top(st.gs_markers))
    flag23 = st.gs_flag.get("2", 0) + st.gs_flag.get("3", 0)
    print(
        f"  VERDICT: {flag23:,} statements carry deleted/less goods. "
        + ("Parser concatenates these into goods_services_text -- FIX" if flag23 else "None here.")
    )

    print("\n[4] <text> multiplicity  (is it 40-char chunking?)")
    print(f"  texts per statement: {dict(sorted(st.text_multiplicity.items()))}")
    if st.text_chunk_len:
        lens = st.text_chunk_len
        near40 = sum(v for k, v in lens.items() if 38 <= k <= 41)
        tot = sum(lens.values())
        print(f"  non-final chunk lengths, top: {dict(lens.most_common(5))}")
        print(f"  VERDICT: {near40:,}/{tot:,} chunks are 38-41 chars. "
              + ("Fixed-width -> join with '' not ' ' -- FIX" if near40 > tot * 0.5
                 else "Not fixed-width; space join is fine."))
    else:
        print("  VERDICT: no multi-text statements in sample; join rule untested.")

    print("\n[5] action-key inventory and ordering")
    print(top(st.action_keys))
    print(f"  ordering: {dict(st.action_key_order)}")
    og = st.action_keys.get("00", 0)
    print(f"  VERDICT: action-key '00' = published for opposition, seen {og:,} times.")
    print("           This is a direct publication flag, cheaper than diffing dates.")

    print("\n[6] Publication cadence  (flat or weekly burst?)")
    print("  publications per file:")
    for name, n in st.pub_per_file.items():
        print(f"    {name:<28} {n:,}")
    print("  published-for-opposition-date by weekday:")
    print(top(st.pub_by_weekday, 7))
    if st.pub_by_weekday:
        dom = st.pub_by_weekday.most_common(1)[0]
        share = dom[1] / sum(st.pub_by_weekday.values())
        print(f"  VERDICT: {dom[0]} holds {share:.0%} of publication dates. "
              + ("Bursty -- size the match engine for the spike."
                 if share > 0.5 else "Not concentrated; load is flatter than assumed."))

    print("\n[7] Prosecution events  (extension-of-time-to-oppose = prospect list)")
    print(f"  records with event statements: {dict(st.has_events)}")
    print(top(st.oppose_events, 8))
    print("  1000 = req. extension to oppose · 1011 = extension granted")
    print("  VERDICT: parser currently DROPS case-file-event-statements entirely."
          + ("  Data is present -- FIX" if st.oppose_events else ""))

    print("\n[8] Field presence (denominator = records)")
    for k, v in st.field_present.most_common():
        print(f"    {k:<32} {v:,}  ({v/max(st.records,1):.0%})")

    print("\n[9] Status codes  (686 = published for opposition, 700 = registered,")
    print("                   802 = req. extension to oppose, 774 = opposition pending)")
    print(top(st.status_codes, 10))
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--limit", type=int, default=0, help="max files to scan")
    args = ap.parse_args()

    d = Path(args.dir)
    files = sorted([p for p in d.iterdir() if p.suffix.lower() in (".zip", ".xml")])
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"no .zip or .xml files in {d}", file=sys.stderr)
        return 1

    st = Stats()
    for p in files:
        print(f"scanning {p.name} ...", file=sys.stderr)
        scan_path(p, st)
    report(st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
