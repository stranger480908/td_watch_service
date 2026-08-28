# USPTO Trademark Watch — ingest and diff

Weeks 1–2 deliverable: daily file in, alertable events out.

```
tdxf/parser.py      streaming TDXF applications parser (lxml iterparse)
tdxf/statements.py  case-file-statement type-code semantics
tdxf/phonetics.py   Double Metaphone blocking keys
tdxf/diff.py        prior state -> event log, with opposition deadlines
tdxf/loader.py      batched Postgres writer
tdxf/handlers.py    Lambda entry points (fetch outside VPC, load inside)
migrations/         001 core USPTO tables, 002 customers + blocking
template.yaml       SAM stack: S3, two Lambdas, S3 gateway endpoint, alarms
tools/              local sample fetcher and assumption analyzer
tests/              fixtures + parser, statement, and phonetic suites
```

## Deploy

CI runs tests on every push and deploys `main` via OIDC. One-time setup:

GitHub **secrets**: `AWS_DEPLOY_ROLE_ARN`, `DATABASE_URL`
GitHub **variables**: `AWS_REGION`, `VPC_ID`, `PRIVATE_SUBNETS`, `ROUTE_TABLE_IDS`, `ALERT_EMAIL`

After the first deploy, set the real ODP key on the `OdpApiKey` secret and run
the migrations against RDS in order.

### Why two Lambdas

`FetchFunction` sits outside the VPC because it needs public internet for
USPTO. `LoadFunction` sits inside because it talks to RDS, and reaches S3
through a free Gateway VPC Endpoint. DB credentials arrive as a KMS-decrypted
env var, resolved by the Lambda service before invocation, so the VPC function
needs no Secrets Manager reachability either.

Net effect: no NAT Gateway, which would otherwise cost ~$32/month standing
before a byte moves. Collapsing these into one VPC-attached function
reintroduces that cost immediately.

Run the tests:

```bash
python3 tests/test_pipeline.py
```

Measured on an 80,000-record synthetic file: 9,400 records/sec, flat memory
(0 MB RSS growth). A real daily file should parse in roughly 10 seconds.

## Data source

Product `TRTDXFAP` — "Trademark Full Text XML Data (No Images) – Daily
Applications", at `https://data.uspto.gov/bulkdata/datasets/TRTDXFAP`.
Schema is the TRADEMARK-APPLICATIONS-DAILY DTD v2.0 (2004-11-08), which the
parser is written against directly.

Related products for later: `TTABTDXF` (daily TTAB — extensions of time to
oppose, and the labelled opposition pairs for training) and `TRTDXFAG` (daily
assignments).

BDSS product endpoints have moved to ODP:
`/api/v1/datasets/products/{productIdentifier}?latest=true`.

## Access: the plan needs one correction

The project notes assume bulk files need no API key and that account setup is
"USPTO.gov account with MFA". Programmatic bulk download is stricter than that:

- The ODP API requires an API key passed as `X-API-KEY`, and that includes the
  Bulk Data Directory endpoints used for automated downloads.
- Issuing the key requires the USPTO.gov account to be linked to a **validated
  ID.me identity**. That is an identity-verification step, not just MFA, and it
  is the long pole.
- Additional profile fields became mandatory 2026-08-18.
- Unused ODP keys are deleted after 90 days.

Manual download through the portal UI is still keyless, which is enough to get a
sample file today, but a daily unattended job needs the key. TSDR remains a
separate server and a separate key, as already noted.

Practical effect: ID.me verification should start immediately and is more of a
schedule risk than the notes assume. Everything in this repo runs without it.

## Two things to verify against the first real file

1. **GS type-code class slice.** The parser reads the international class out of
   goods/services type codes as `GS` + 3-digit class + sequence (`GS0351` →
   class 035). The fixtures encode that same assumption, so the passing test
   confirms the code matches the assumption — not that the assumption matches
   USPTO. Check it against real data before trusting class-scoped goods overlap.
2. **`action-key` grouping.** The DTD allows repeated `action-key` and
   `case-file` children under one `action-keys` parent. The parser latches the
   most recent key seen. Confirm real files emit key-then-cases in that order.

## Design decisions worth keeping

**Events, not snapshots.** `mark_event` is append-only and alerts read from it.
Deriving "what changed" at send time from current state is where duplicate and
missed alerts come from.

**`content_hash` excludes `transaction_date` and `action_key`.** USPTO re-emits
records with a fresh transaction date and no substantive change. Hashing those
fields would mark every record modified every day and bury customers in noise.
There is a test for exactly this (serial 97000002).

**Republication resets the clock.** A changed publication date emits
`PUBLICATION_DATE_CHANGED` carrying a new deadline rather than being treated as
a no-op, and a record first seen already-published still emits its deadline.

## Free signal the notes did not account for

The daily file already contains two fields that overlap with scoring factors
budgeted as build work:

- **Pseudo marks** (`PM` statements) — USPTO's own alternate spellings for marks
  with numbers, misspellings, or phonetic substitutions. `NUVAHNA` carries the
  pseudo mark `NUVANA`. This is agency-generated phonetic equivalence, free.
- **Translation statements** (`TR`) — the translation and transliteration
  factor, stated by the applicant.

Both are parsed and get their own indexed table (`mark_alias`). Treat them as
strong features and as a baseline the learned scorer has to beat, not as a
replacement for phonetic scoring.

## Not built yet

Downloader (blocked on the API key), Postgres loader, scoring engine, alerting.
The loader is a thin mapping from `CaseFile` to the tables in `schema.sql`.
