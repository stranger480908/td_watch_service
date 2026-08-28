-- 002: the customer side, the sent-alert ledger, and blocking keys.
--
-- 001 covered USPTO data only. Nothing in it knows who a customer is or what
-- has already been emailed, which is the second half of idempotency: the event
-- dedupe index stops duplicate EVENTS, only this stops duplicate EMAILS.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------- customers

CREATE TABLE IF NOT EXISTS customer (
    id              BIGSERIAL PRIMARY KEY,
    firm_name       TEXT        NOT NULL,
    contact_email   TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'beta',
        -- beta | active | paused | churned
    alert_threshold NUMERIC(4,3) NOT NULL DEFAULT 0.600,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT customer_email_lower CHECK (contact_email = lower(contact_email))
);

CREATE UNIQUE INDEX IF NOT EXISTS customer_email_idx ON customer (contact_email);

-- A watched mark is not necessarily a USPTO record: customers watch marks they
-- are about to file, and common-law marks that were never filed at all. So
-- mark_text is the required field and serial_number is the optional one.
CREATE TABLE IF NOT EXISTS watch_item (
    id              BIGSERIAL PRIMARY KEY,
    customer_id     BIGINT NOT NULL REFERENCES customer(id) ON DELETE CASCADE,
    mark_text       TEXT   NOT NULL,
    serial_number   TEXT,
    client_ref      TEXT,          -- appears in the alert subject line
    nice_classes    TEXT[] NOT NULL DEFAULT '{}',
    goods_text      TEXT   NOT NULL DEFAULT '',
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS watch_item_customer_idx ON watch_item (customer_id) WHERE active;
CREATE INDEX IF NOT EXISTS watch_item_trgm_idx ON watch_item USING gin (mark_text gin_trgm_ops);

-- ------------------------------------------------------------ blocking keys

-- Double Metaphone codes, one row per distinct token code. Populated by the
-- loader (tdxf/phonetics.py) rather than a generated column, because marks are
-- multi-word and need per-token keys: "NUVANA LABS" must block with "NUVANA".
CREATE TABLE IF NOT EXISTS mark_phonetic (
    serial_number   TEXT NOT NULL REFERENCES mark(serial_number) ON DELETE CASCADE,
    code            TEXT NOT NULL,
    source          TEXT NOT NULL,   -- mark | pseudo_mark | translation | transliteration
    PRIMARY KEY (serial_number, code, source)
);

CREATE INDEX IF NOT EXISTS mark_phonetic_code_idx ON mark_phonetic (code);

CREATE TABLE IF NOT EXISTS watch_phonetic (
    watch_item_id   BIGINT NOT NULL REFERENCES watch_item(id) ON DELETE CASCADE,
    code            TEXT   NOT NULL,
    PRIMARY KEY (watch_item_id, code)
);

CREATE INDEX IF NOT EXISTS watch_phonetic_code_idx ON watch_phonetic (code);

-- ------------------------------------------------------- class relatedness

-- Class equality is the wrong filter. Cosmetics (003), retail of cosmetics
-- (035) and beauty salon services (044) conflict with each other constantly.
-- Populate weight from TTAB opposition co-occurrence: any class pair appearing
-- across real oppositions is related, weighted by frequency. Until that runs,
-- seed identity pairs so the filter degrades to equality rather than to empty.
CREATE TABLE IF NOT EXISTS class_relatedness (
    class_a     TEXT NOT NULL,
    class_b     TEXT NOT NULL,
    weight      NUMERIC(4,3) NOT NULL,
    n_observed  INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'ttab',
    PRIMARY KEY (class_a, class_b),
    CONSTRAINT class_relatedness_ordered CHECK (class_a <= class_b)
);

INSERT INTO class_relatedness (class_a, class_b, weight, source)
SELECT c, c, 1.000, 'identity'
FROM (SELECT lpad(generate_series(1,45)::text, 3, '0') AS c) s
ON CONFLICT DO NOTHING;

-- ------------------------------------------------------------ sent alerts

-- Append-only, like mark_event. This is what makes a threshold change safe:
-- rescoring old events regenerates candidate alerts, and this table is what
-- stops them being emailed twice.
CREATE TABLE IF NOT EXISTS sent_alert (
    id                  BIGSERIAL PRIMARY KEY,
    customer_id         BIGINT NOT NULL REFERENCES customer(id) ON DELETE CASCADE,
    watch_item_id       BIGINT REFERENCES watch_item(id) ON DELETE SET NULL,
    serial_number       TEXT   NOT NULL,
    event_id            BIGINT REFERENCES mark_event(id),
    score               NUMERIC(4,3),
    opposition_deadline DATE,
    subject             TEXT   NOT NULL,
    ses_message_id      TEXT,
    sent_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One alert per (customer, conflicting mark, event). A second publication of
-- the same mark is a different event and legitimately alerts again.
CREATE UNIQUE INDEX IF NOT EXISTS sent_alert_dedupe_idx
    ON sent_alert (customer_id, serial_number, COALESCE(event_id, 0));

CREATE INDEX IF NOT EXISTS sent_alert_deadline_idx
    ON sent_alert (opposition_deadline) WHERE opposition_deadline IS NOT NULL;

-- --------------------------------------------------------- beta feedback

-- Beta attorneys agree to report misses. Their corrections are the training
-- signal, so they need somewhere to land that is not an inbox.
CREATE TABLE IF NOT EXISTS scoring_feedback (
    id              BIGSERIAL PRIMARY KEY,
    customer_id     BIGINT REFERENCES customer(id) ON DELETE SET NULL,
    watch_item_id   BIGINT REFERENCES watch_item(id) ON DELETE SET NULL,
    serial_number   TEXT   NOT NULL,
    verdict         TEXT   NOT NULL,   -- miss | false_positive | correct
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT scoring_feedback_verdict
        CHECK (verdict IN ('miss', 'false_positive', 'correct'))
);

-- ------------------------------------------------------ candidate generation

-- The blocking query. Two independent recall paths OR'd together, then a
-- class-relatedness filter. Scoring runs only on what survives this.
CREATE OR REPLACE VIEW candidate_pair AS
SELECT w.id          AS watch_item_id,
       w.customer_id,
       m.serial_number,
       m.mark_identification,
       m.published_for_opposition_date,
       t.trgm_sim,
       ph.shared_codes,
       GREATEST(t.trgm_sim, CASE WHEN ph.shared_codes > 0 THEN 0.5 ELSE 0 END)
                     AS block_strength
FROM watch_item w
JOIN mark m
  ON m.published_for_opposition_date IS NOT NULL
CROSS JOIN LATERAL (
    SELECT similarity(w.mark_text, m.mark_identification) AS trgm_sim
) t
CROSS JOIN LATERAL (
    SELECT count(*) AS shared_codes
    FROM watch_phonetic wp
    JOIN mark_phonetic mp ON mp.code = wp.code
    WHERE wp.watch_item_id = w.id
      AND mp.serial_number = m.serial_number
) ph
WHERE w.active
  AND (t.trgm_sim >= 0.30 OR ph.shared_codes > 0)
  AND (
        cardinality(w.nice_classes) = 0
     OR EXISTS (
            SELECT 1
            FROM unnest(w.nice_classes) AS x(wc)
            JOIN mark_class mc ON mc.serial_number = m.serial_number
            JOIN class_relatedness cr
              ON  cr.class_a = LEAST(mc.nice_class, x.wc)
              AND cr.class_b = GREATEST(mc.nice_class, x.wc)
            WHERE cr.weight >= 0.25
        )
  );

COMMENT ON VIEW candidate_pair IS
  'Blocking output. Never score the full cross product; score this.';
