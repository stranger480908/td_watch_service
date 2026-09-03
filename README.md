# td_watch_service

USPTO trademark monitoring. Two pipelines over one data source: a Postgres path
that will drive opposition-deadline alerts, and a Databricks lakehouse built as
a learning exercise.

Both consume the same daily bulk file and neither depends on the other.

## What runs today

```
EventBridge 09:15 UTC
  -> FetchFunction        download from USPTO ODP, write S3
       -> LoadFunction    parse, diff, write Postgres          (product path)
       -> UploadFunction  parse, push parquet, trigger DLT     (analytics path)
```

As of the last run: 182,386 marks and 341,534 events in Postgres, 241,114 rows
through the Databricks medallion, one published dashboard.

Not built yet: scoring, alert composition, email delivery, any customer-facing
interface. The product path stops at the event log.

## Layout

```
tdxf/parser.py       streaming TDXF parser, lxml iterparse
tdxf/statements.py   case-file-statement type-code semantics
tdxf/phonetics.py    Double Metaphone blocking keys
tdxf/diff.py         prior state -> event log with opposition deadlines
tdxf/loader.py       batched Postgres writer
tdxf/handlers.py     five Lambda entry points
migrations/          001 USPTO tables, 002 customers and blocking keys
template.yaml        SAM stack
bootstrap-oidc.yaml  one-time deploy role, deliberately not managed by CI
tools/               local sample fetcher, assumption analyzer, parquet writer
tests/               fixture suites plus a real-data suite
```

Infrastructure detail, resource IDs and runbook commands are in
`INFRASTRUCTURE.md`.

## Decision log

### Bulk files for discovery, TSDR only for detail

TSDR has no discovery query. It answers questions about serial numbers you can
already name, and there is no "what changed since yesterday" endpoint. Even at
unlimited throughput it could not drive the pipeline, because you would need
the answer before you could ask. The daily bulk file is the only thing that
says what changed.

The rate limit reinforces this. At 60 requests/minute, walking one daily file's
80,000 records takes about 22 hours - a daily job that cannot finish in a day.
So TSDR sits at the leaf, called only for the handful of marks that already
scored, where its cost is a rounding error.

### Two Lambda functions instead of one

`FetchFunction` runs outside the VPC because it needs public internet for USPTO
and Secrets Manager. `LoadFunction` runs inside because it talks to RDS, and
reaches S3 through a Gateway VPC Endpoint, which is free.

Collapsing them into one VPC-attached function would force a NAT Gateway at
roughly $32/month standing, before a byte moves. Database credentials arrive as
a KMS-decrypted environment variable that Lambda resolves before invocation, so
the VPC functions need no Secrets Manager reachability either.

### Fetch invokes load directly, not via S3 notification

An S3 `ObjectCreated` notification makes the bucket depend on the function (for
the notification target) while the function depends on the bucket (for its IAM
policy). CloudFormation rejects that as a circular dependency, and `cfn-lint`
does not catch it.

Direct invocation is also more explicit: replaying one day is a single call with
a known key.

### An append-only event log between ingest and alerting

`mark` holds current state and is upserted. `mark_event` is append-only, and
alerts read from it rather than from current state.

Each event row corresponds to something told to an attorney. If those rows can
be rewritten, replaying a day after a parser fix silently changes what you claim
to have said. For a service whose compliance line is "report filings and
deadlines as facts", being able to reconstruct exactly what was asserted and
when is not optional.

This is not event sourcing. `GOODS_SERVICES_CHANGED` is emitted without old and
new values, so `mark` cannot be rebuilt by folding the log. The rebuild path is
replaying the S3 archive.

### The content hash excludes transaction_date and action_key

USPTO re-emits records with a fresh transaction date and no substantive change.
Hashing those fields would mark every record modified every day and bury
customers in noise on the first send. There is a test for exactly this.

### Fan-out rather than a chain

`FetchFunction` invokes the Postgres loader and the Databricks uploader
independently, and each parses the file itself. That duplicates about ten
seconds of parsing per run.

The alternative - loader feeds uploader - couples them. A Databricks outage
would then break alerting, which is the thing that actually matters. Ten seconds
of duplicate work is a good price for that isolation.

### DLT instead of a Job with SQL tasks

Both were available on Free Edition. Jobs would have reused the SQL already
written and shipped faster.

DLT was chosen because it is declarative: tables are declared with their
derivations, and the dependency graph is inferred. That gave three things the
Job would not - automatic lineage, `EXPECT` constraints as native data quality,
and `APPLY CHANGES` replacing a hand-written MERGE plus watermark logic.

It also worked out on its own that `gold_publication_cadence` can update
incrementally while `gold_open_opposition_windows` needs full recompute, because
the latter depends on `current_date()`. Nobody told it that.

### Bronze holds records, not bytes

Strictly, bronze should hold source bytes unaltered. Here it holds one row per
USPTO record, parsed but otherwise unmodified.

The XML parsing is already validated against real files in `tdxf/parser.py`, and
reimplementing it inside Spark would duplicate proven code for no benefit. The
raw bytes still exist - the S3 archive is the real bronze, and the true replay
path. Filtering, typing and cleaning all still happen downstream in silver.

### Postgres for the product, Delta for analytics

The product asks "give me serial 97000001 and everything about it" and "which
watched marks resemble this one" - row lookups and indexed similarity search.
Postgres work.

The lakehouse asks "count filings by class per week" - scanning a column across
a quarter-million rows. Columnar work. Same data, genuinely different access
patterns.

That the two independently produce the same 170,118 distinct marks is the
cross-check that both are right.

## Free Edition limitations, and what production would look like

Databricks Free Edition cannot read S3. There is no storage credential, no
external location, no private networking. So `UploadFunction` pushes parquet in
over the files API rather than Databricks pulling.

**This is a workaround, not the design.** On a paid workspace it would not
exist: an external location over the raw bucket, Auto Loader picking up new
objects incrementally, no copying and no second parse.

Also cut for Free Edition, all of which the original spec called for:

- Terraform provisioning the workspace - no account-level API
- Customer-managed VPC, private subnets, KMS, IAM storage credentials
- MWAA - standing cost not justified at one file per day
- Job clusters and spot instances - serverless only

Free Edition is also non-commercial by its terms. The lakehouse is a learning
artifact over public data and does not feed the commercial pipeline.

## Things found by testing against real data

The fixture suites only prove the code agrees with assumptions I wrote into both
the fixture and the parser. The real-data suite, with thresholds derived from an
actual USPTO file, found these:

**Transliteration is `TL`, not `TLIT`.** DTD v2.0 documentation describes a
four-character prefix. Real v2.3 data uses two. A "fix" based on the
documentation broke working behaviour and silently dropped 245 records per file
- and transliteration is one of the six scoring factors.

**Class codes include more than Nice 001-045.** Certification marks use `A` and
`B`, collective membership uses `200`, and historical pre-Nice US classes like
`046` and `107` still appear. A validator that only knew Nice flagged 106 valid
records as invalid.

**Publications are 100% Tuesday.** Across 109,110 publication dates, without
exception. The Official Gazette is weekly, so the match engine must be sized for
a weekly spike rather than a daily average.

**The 40-character `<text>` chunking does not exist in v2.3.** Documented for
v2.0, and I wrote a join heuristic for it. Every statement in real data has
exactly one `<text>` element. Harmless, but unnecessary.

**Removed goods are marked in the type code.** Position 6 of a `GS` code is an
amendment flag, not a sequence number: `2` means the statement is entirely goods
that were removed. Concatenating those into the scoring text means matching
against coverage the registrant gave up - the exact false positive that gets a
service unsubscribed.

## Operational bugs worth remembering

**A counter inside a generator read as zero.** `records` was incremented in a
nested generator and read after `load_stream` consumed it. Data loaded fine;
`ingest_run` reported zero records. Observability failed while the pipeline
worked, which is the harder failure to notice.

**Concurrent loads clobbered run status.** `start_run` used `ON CONFLICT DO
UPDATE SET status = 'running'`, so a second invocation for the same file reset a
completed row. The data was unaffected - upsert and the dedupe index absorbed
it - but the log became unreadable.

Both are why `NoRecordsAlarm` watches for absence of invocations rather than
only for errors. A pipeline that goes quiet without erroring is how a customer
misses an opposition deadline.

## Known gaps

- The real-data suite skips silently in CI, because CI has no USPTO file. It
  passes without running.
- Nothing tests the Databricks side. `EXPECT` constraints are declared but no
  test asserts they hold.
- RDS, the VPC plumbing and the IAM user were created by hand and exist in no
  template. See `INFRASTRUCTURE.md`.
- The account is on the AWS Free Plan, which auto-closes when credits deplete.
  Upgrade before there is customer data.
- `class_relatedness` holds only identity pairs. Populating it from TTAB
  opposition co-occurrence is what makes class filtering meaningful.

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python tests/test_pipeline.py
python tests/test_statements.py
python tests/test_phonetics.py

# Real-data suite needs a file:
aws s3 cp s3://tmwatch-raw-.../apc260826.zip data/raw/
python tests/test_real_file.py
python tools/analyze_sample.py --dir data/raw
```

CI runs lint, all four suites, the analyzer and `cfn-lint`, then deploys to
`main` via OIDC. No long-lived AWS keys.
