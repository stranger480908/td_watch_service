-- USPTO Trademark Watch — v1 schema
--
-- Two jobs only: hold current state well enough to diff against, and hold the
-- event log the alerting layer reads. No reporting tables, no dashboard tables.

CREATE TABLE IF NOT EXISTS ingest_run (
    id              BIGSERIAL PRIMARY KEY,
    product         TEXT        NOT NULL DEFAULT 'TRTDXFAP',
    file_name       TEXT        NOT NULL,
    file_date       DATE        NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    records_seen    INTEGER     NOT NULL DEFAULT 0,
    events_emitted  INTEGER     NOT NULL DEFAULT 0,
    status          TEXT        NOT NULL DEFAULT 'running',
    error           TEXT,
    UNIQUE (product, file_name)
);

-- Current state, one row per serial number.
CREATE TABLE IF NOT EXISTS mark (
    serial_number                   TEXT PRIMARY KEY,
    registration_number             TEXT,
    mark_identification             TEXT,
    mark_drawing_code               TEXT,
    filing_date                     DATE,
    registration_date               DATE,
    status_code                     TEXT,
    status_date                     DATE,
    published_for_opposition_date   DATE,
    abandonment_date                DATE,
    cancellation_date               DATE,
    renewal_date                    DATE,
    attorney_name                   TEXT,
    attorney_docket_number          TEXT,
    law_office_code                 TEXT,
    standard_characters_claimed     BOOLEAN NOT NULL DEFAULT FALSE,
    opposition_pending              BOOLEAN NOT NULL DEFAULT FALSE,
    cancellation_pending            BOOLEAN NOT NULL DEFAULT FALSE,
    section_2f                      BOOLEAN NOT NULL DEFAULT FALSE,
    intent_to_use                   BOOLEAN NOT NULL DEFAULT FALSE,
    goods_services_text             TEXT    NOT NULL DEFAULT '',
    content_hash                    TEXT    NOT NULL,
    first_seen_run                  BIGINT REFERENCES ingest_run(id),
    last_seen_run                   BIGINT REFERENCES ingest_run(id),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The publication index. Every alert-worthy mark is found through this.
CREATE INDEX IF NOT EXISTS mark_published_idx
    ON mark (published_for_opposition_date)
    WHERE published_for_opposition_date IS NOT NULL;

-- Prospect list: attorneys of record with many active marks.
CREATE INDEX IF NOT EXISTS mark_attorney_idx ON mark (attorney_name);

-- Trigram index for candidate generation before scoring. Scoring is expensive;
-- this is the cheap filter that keeps it off the full corpus.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS mark_identification_trgm_idx
    ON mark USING gin (mark_identification gin_trgm_ops);

CREATE TABLE IF NOT EXISTS mark_class (
    serial_number   TEXT NOT NULL REFERENCES mark(serial_number) ON DELETE CASCADE,
    nice_class      TEXT NOT NULL,
    is_primary      BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (serial_number, nice_class)
);

CREATE INDEX IF NOT EXISTS mark_class_class_idx ON mark_class (nice_class);

-- Goods and services, split per class. Kept separate from mark.goods_services_text
-- because class-scoped overlap is what suppresses the false positives; the
-- concatenated blob is only for cheap full-text prefiltering.
CREATE TABLE IF NOT EXISTS mark_goods (
    id              BIGSERIAL PRIMARY KEY,
    serial_number   TEXT NOT NULL REFERENCES mark(serial_number) ON DELETE CASCADE,
    nice_class      TEXT,
    description     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS mark_goods_serial_idx ON mark_goods (serial_number);

-- Pseudo marks and translations. USPTO computes these itself; they are free
-- phonetic and cross-language signal for the scoring engine.
CREATE TABLE IF NOT EXISTS mark_alias (
    id              BIGSERIAL PRIMARY KEY,
    serial_number   TEXT NOT NULL REFERENCES mark(serial_number) ON DELETE CASCADE,
    alias_type      TEXT NOT NULL,   -- 'pseudo_mark' | 'translation'
    text            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS mark_alias_text_trgm_idx
    ON mark_alias USING gin (text gin_trgm_ops);

CREATE TABLE IF NOT EXISTS mark_owner (
    id              BIGSERIAL PRIMARY KEY,
    serial_number   TEXT NOT NULL REFERENCES mark(serial_number) ON DELETE CASCADE,
    party_name      TEXT,
    party_type      TEXT,
    legal_entity_type_code TEXT,
    city            TEXT,
    state           TEXT,
    country         TEXT,
    nationality     TEXT
);

CREATE INDEX IF NOT EXISTS mark_owner_serial_idx ON mark_owner (serial_number);

-- Append-only. Alerts are generated from here, never from mark state.
CREATE TABLE IF NOT EXISTS mark_event (
    id                    BIGSERIAL PRIMARY KEY,
    run_id                BIGINT REFERENCES ingest_run(id),
    serial_number         TEXT   NOT NULL,
    event_type            TEXT   NOT NULL,
    observed_on           DATE   NOT NULL,
    old_value             TEXT,
    new_value             TEXT,
    opposition_deadline   DATE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS mark_event_deadline_idx
    ON mark_event (opposition_deadline)
    WHERE opposition_deadline IS NOT NULL;

CREATE INDEX IF NOT EXISTS mark_event_serial_idx ON mark_event (serial_number, observed_on);

-- Idempotency: re-running a daily file must not re-send alerts.
CREATE UNIQUE INDEX IF NOT EXISTS mark_event_dedupe_idx
    ON mark_event (serial_number, event_type, observed_on, COALESCE(new_value, ''));
