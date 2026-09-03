"""
Load parsed records into Postgres and emit events.

Batched throughout. A daily file is ~80k records; row-at-a-time inserts turn a
10-second parse into a 20-minute one and will blow the Lambda timeout.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import date

from psycopg import Connection
from psycopg.rows import tuple_row

from .diff import Event, MarkState, diff_record
from .parser import CaseFile
from .phonetics import blocking_rows

BATCH = 2000

UPSERT_MARK = """
INSERT INTO mark (
    serial_number, registration_number, mark_identification, mark_drawing_code,
    filing_date, registration_date, status_code, status_date,
    published_for_opposition_date, abandonment_date, cancellation_date,
    renewal_date, attorney_name, attorney_docket_number, law_office_code,
    standard_characters_claimed, opposition_pending, cancellation_pending,
    section_2f, intent_to_use, goods_services_text, content_hash,
    first_seen_run, last_seen_run, updated_at
) VALUES (
    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()
)
ON CONFLICT (serial_number) DO UPDATE SET
    registration_number = EXCLUDED.registration_number,
    mark_identification = EXCLUDED.mark_identification,
    mark_drawing_code = EXCLUDED.mark_drawing_code,
    filing_date = EXCLUDED.filing_date,
    registration_date = EXCLUDED.registration_date,
    status_code = EXCLUDED.status_code,
    status_date = EXCLUDED.status_date,
    published_for_opposition_date = EXCLUDED.published_for_opposition_date,
    abandonment_date = EXCLUDED.abandonment_date,
    cancellation_date = EXCLUDED.cancellation_date,
    renewal_date = EXCLUDED.renewal_date,
    attorney_name = EXCLUDED.attorney_name,
    attorney_docket_number = EXCLUDED.attorney_docket_number,
    law_office_code = EXCLUDED.law_office_code,
    standard_characters_claimed = EXCLUDED.standard_characters_claimed,
    opposition_pending = EXCLUDED.opposition_pending,
    cancellation_pending = EXCLUDED.cancellation_pending,
    section_2f = EXCLUDED.section_2f,
    intent_to_use = EXCLUDED.intent_to_use,
    goods_services_text = EXCLUDED.goods_services_text,
    content_hash = EXCLUDED.content_hash,
    last_seen_run = EXCLUDED.last_seen_run,
    updated_at = now()
"""

INSERT_EVENT = """
INSERT INTO mark_event (
    run_id, serial_number, event_type, observed_on,
    old_value, new_value, opposition_deadline
) VALUES (%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT DO NOTHING
"""

SELECT_STATE = """
SELECT serial_number, content_hash, mark_identification, status_code,
       published_for_opposition_date, registration_date, abandonment_date,
       cancellation_date, opposition_pending, goods_services_text
FROM mark WHERE serial_number = ANY(%s)
"""

SELECT_CLASSES = "SELECT serial_number, nice_class FROM mark_class WHERE serial_number = ANY(%s)"
SELECT_OWNERS = "SELECT serial_number, party_name FROM mark_owner WHERE serial_number = ANY(%s)"


def start_run(conn: Connection, file_name: str, file_date: date) -> int:
    with conn.cursor() as cur:
        cur.execute(
            # Only reset a run that is not already finished. Concurrent
            # invocations for the same file used to clobber a completed row
            # back to 'running', which made the log unreadable even though the
            # data was fine.
            """INSERT INTO ingest_run (file_name, file_date) VALUES (%s,%s)
               ON CONFLICT (product, file_name) DO UPDATE
                 SET started_at = now(),
                     status = CASE WHEN ingest_run.status = 'ok'
                                   THEN ingest_run.status ELSE 'running' END
               RETURNING id""",
            (file_name, file_date),
        )
        return cur.fetchone()[0]


def finish_run(conn: Connection, run_id: int, records: int, events: int,
               error: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE ingest_run SET finished_at = now(), records_seen = %s,
                      events_emitted = %s, status = %s, error = %s WHERE id = %s""",
            (records, events, "failed" if error else "ok", error, run_id),
        )


def load_prior_state(conn: Connection, serials: list[str]) -> dict[str, MarkState]:
    """Fetch stored state for a batch. Child rows are pulled in two extra
    round-trips rather than a join, to avoid fanning the parent rows out."""
    if not serials:
        return {}
    classes: dict[str, list[str]] = {}
    owners: dict[str, list[str]] = {}
    with conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(SELECT_CLASSES, (serials,))
        for sn, c in cur.fetchall():
            classes.setdefault(sn, []).append(c)
        cur.execute(SELECT_OWNERS, (serials,))
        for sn, n in cur.fetchall():
            owners.setdefault(sn, []).append(n)
        cur.execute(SELECT_STATE, (serials,))
        rows = cur.fetchall()

    out: dict[str, MarkState] = {}
    for r in rows:
        sn = r[0]
        out[sn] = MarkState(
            serial_number=sn, content_hash=r[1], mark_identification=r[2],
            status_code=r[3], published_for_opposition_date=r[4],
            registration_date=r[5], abandonment_date=r[6], cancellation_date=r[7],
            opposition_pending=r[8],
            nice_classes=tuple(sorted(classes.get(sn, []))),
            goods_services_text=r[9] or "",
            owner_names=tuple(owners.get(sn, [])),
        )
    return out


def _mark_row(cf: CaseFile, run_id: int) -> tuple:
    return (
        cf.serial_number, cf.registration_number, cf.mark_identification,
        cf.mark_drawing_code, cf.filing_date, cf.registration_date,
        cf.status_code, cf.status_date, cf.published_for_opposition_date,
        cf.abandonment_date, cf.cancellation_date, cf.renewal_date,
        cf.attorney_name, cf.attorney_docket_number,
        cf.law_office_assigned_location_code, cf.standard_characters_claimed,
        cf.opposition_pending, cf.cancellation_pending, cf.section_2f,
        cf.intent_to_use, cf.goods_services_text, cf.content_hash(),
        run_id, run_id,
    )


def _write_children(conn: Connection, batch: list[CaseFile]) -> None:
    serials = [c.serial_number for c in batch]
    classes, goods, aliases, owners, phonetic = [], [], [], [], []
    for cf in batch:
        primary = {c.primary_code for c in cf.classifications if c.primary_code}
        for nc in cf.nice_classes:
            classes.append((cf.serial_number, nc, nc in primary))
        for nc, desc in cf.goods_services:
            goods.append((cf.serial_number, nc, desc))
        for t in cf.pseudo_marks:
            aliases.append((cf.serial_number, "pseudo_mark", t))
        for t in cf.translations:
            aliases.append((cf.serial_number, "translation", t))
        for t in cf.transliterations:
            aliases.append((cf.serial_number, "transliteration", t))
        phonetic.extend(blocking_rows(cf.serial_number, {
            "mark": [cf.full_mark_text] if cf.full_mark_text else [],
            "pseudo_mark": cf.pseudo_marks,
            "translation": cf.translations,
            "transliteration": cf.transliterations,
        }))
        for o in cf.owners:
            owners.append((cf.serial_number, o.party_name, o.party_type,
                           o.legal_entity_type_code, o.city, o.state,
                           o.country, o.nationality))

    with conn.cursor() as cur:
        # Child rows are replace-not-merge: a class or owner can be removed
        # upstream, and merging would leave the removal invisible.
        cur.execute("DELETE FROM mark_class WHERE serial_number = ANY(%s)", (serials,))
        cur.execute("DELETE FROM mark_goods WHERE serial_number = ANY(%s)", (serials,))
        cur.execute("DELETE FROM mark_alias WHERE serial_number = ANY(%s)", (serials,))
        cur.execute("DELETE FROM mark_owner WHERE serial_number = ANY(%s)", (serials,))
        cur.execute("DELETE FROM mark_phonetic WHERE serial_number = ANY(%s)", (serials,))
        if classes:
            cur.executemany(
                "INSERT INTO mark_class (serial_number, nice_class, is_primary)"
                " VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", classes)
        if goods:
            cur.executemany(
                "INSERT INTO mark_goods (serial_number, nice_class, description)"
                " VALUES (%s,%s,%s)", goods)
        if aliases:
            cur.executemany(
                "INSERT INTO mark_alias (serial_number, alias_type, text)"
                " VALUES (%s,%s,%s)", aliases)
        if owners:
            cur.executemany(
                "INSERT INTO mark_owner (serial_number, party_name, party_type,"
                " legal_entity_type_code, city, state, country, nationality)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", owners)
        if phonetic:
            cur.executemany(
                "INSERT INTO mark_phonetic (serial_number, code, source)"
                " VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", phonetic)


def load_batch(conn: Connection, batch: list[CaseFile], run_id: int,
               observed_on: date) -> list[Event]:
    prior = load_prior_state(conn, [c.serial_number for c in batch])
    events: list[Event] = []
    for cf in batch:
        events.extend(diff_record(cf, prior.get(cf.serial_number), observed_on))

    with conn.cursor() as cur:
        cur.executemany(UPSERT_MARK, [_mark_row(c, run_id) for c in batch])
        if events:
            cur.executemany(INSERT_EVENT, [
                (run_id, e.serial_number, e.event_type.value, e.observed_on,
                 e.old_value, e.new_value, e.opposition_deadline) for e in events])
    _write_children(conn, batch)
    return events


def load_stream(conn: Connection, case_files: Iterable[CaseFile], run_id: int,
                observed_on: date, batch_size: int = BATCH) -> Iterator[Event]:
    batch: list[CaseFile] = []
    for cf in case_files:
        if not cf.serial_number:
            continue
        batch.append(cf)
        if len(batch) >= batch_size:
            yield from load_batch(conn, batch, run_id, observed_on)
            conn.commit()
            batch = []
    if batch:
        yield from load_batch(conn, batch, run_id, observed_on)
        conn.commit()
