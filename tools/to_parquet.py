"""
Convert USPTO daily zips into Parquet for the lakehouse bronze layer.

Bronze normally holds bytes exactly as received. Here it holds one row per
USPTO record with no values altered, because the XML parsing is already
validated against real files in tdxf/parser.py and reimplementing it inside
Spark would duplicate proven code. Filtering, cleaning and typing all still
happen downstream in silver.
"""

import datetime as dt
import pathlib
import sys

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from tdxf.parser import iter_case_files_from_zip  # noqa: E402


def row(cf, zf):
    return {
        "serial_number": cf.serial_number,
        "registration_number": cf.registration_number,
        "mark_identification": cf.mark_identification,
        "full_mark_text": cf.full_mark_text or None,
        "mark_drawing_code": cf.mark_drawing_code,
        "filing_date": cf.filing_date,
        "registration_date": cf.registration_date,
        "status_code": cf.status_code,
        "status_date": cf.status_date,
        "published_for_opposition_date": cf.published_for_opposition_date,
        "abandonment_date": cf.abandonment_date,
        "cancellation_date": cf.cancellation_date,
        "attorney_name": cf.attorney_name,
        "standard_characters_claimed": cf.standard_characters_claimed,
        "opposition_pending": cf.opposition_pending,
        "intent_to_use": cf.intent_to_use,
        "nice_classes": cf.nice_classes,
        "goods_classes": [c for c, _ in cf.goods_services],
        "goods_descriptions": [d for _, d in cf.goods_services],
        "pseudo_marks": cf.pseudo_marks,
        "translations": cf.translations,
        "transliterations": cf.transliterations,
        "owner_names": cf.owner_names,
        "owner_countries": [o.country for o in cf.owners if o.country],
        "event_codes": [e.code for e in cf.events if e.code],
        "event_dates": [e.date for e in cf.events if e.date],
        "source_file": zf.name,
        "file_date": dt.date(
            2000 + int(zf.stem[3:5]), int(zf.stem[5:7]), int(zf.stem[7:9])
        ),
    }


def main() -> int:
    out = pathlib.Path("data/parquet")
    out.mkdir(parents=True, exist_ok=True)
    zips = sorted(pathlib.Path("data/raw").rglob("*.zip"))
    if not zips:
        print("no zips in data/raw/")
        return 1
    seen = set()
    for zf in zips:
        if zf.name in seen:
            continue
        seen.add(zf.name)
        rows = [row(cf, zf) for cf in iter_case_files_from_zip(zf) if cf.serial_number]
        dest = out / f"{zf.stem}.parquet"
        pq.write_table(pa.Table.from_pylist(rows), dest, compression="snappy")
        print(f"{zf.name}: {len(rows):,} rows -> {dest.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
