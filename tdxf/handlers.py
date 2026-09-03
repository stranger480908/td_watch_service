"""
Lambda entry points.

Two functions, deliberately split, because the split is what avoids a NAT
Gateway:

  fetch_handler   NOT in a VPC. Needs outbound internet to reach USPTO and
                  Secrets Manager. Writes the raw zip to S3.

  load_handler    IN the VPC, because it talks to RDS. Reads S3 through a
                  Gateway VPC Endpoint, which is free and needs no NAT. It has
                  no other internet dependency, so it needs no egress at all.

Putting both in one VPC-attached function would force a NAT Gateway at roughly
$32/month standing cost before a single byte moves.

DB credentials arrive as KMS-encrypted environment variables. Lambda decrypts
those in the service layer before invoking, so the VPC function needs no
Secrets Manager reachability and no interface endpoint either.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import UTC, date, datetime

import boto3

from .diff import EventType, summarise
from .loader import finish_run, load_stream, start_run
from .parser import iter_case_files_from_zip

RAW_BUCKET = os.environ.get("RAW_BUCKET", "")
ODP_SECRET_ARN = os.environ.get("ODP_SECRET_ARN", "")
PRODUCT = os.environ.get("PRODUCT", "TRTDXFAP")
ODP_BASE = "https://api.uspto.gov/api/v1/datasets/products"

s3 = boto3.client("s3")

DATE_IN_NAME = re.compile(r"(\d{2})(\d{2})(\d{2})\.zip$", re.I)


def _api_key() -> str:
    if not ODP_SECRET_ARN:
        raise RuntimeError("ODP_SECRET_ARN not configured")
    sm = boto3.client("secretsmanager")
    raw = sm.get_secret_value(SecretId=ODP_SECRET_ARN)["SecretString"]
    try:
        return json.loads(raw)["api_key"]
    except (json.JSONDecodeError, KeyError):
        return raw.strip()


def _file_date(name: str) -> date | None:
    m = DATE_IN_NAME.search(name)
    if not m:
        return None
    yy, mm, dd = (int(g) for g in m.groups())
    try:
        return date(2000 + yy, mm, dd)
    except ValueError:
        return None


def fetch_handler(event, context):
    """
    Download the latest daily file to S3. Idempotent: an object that already
    exists is left alone, so a retry or a manual re-invoke costs nothing.
    """
    key = _api_key()
    url = f"{ODP_BASE}/{PRODUCT}?latest=true"
    req = urllib.request.Request(url, headers={"X-API-KEY": key, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        doc = json.loads(r.read())

    urls = re.findall(r'https?://[^\s"\']+\.zip', json.dumps(doc))
    if not urls:
        raise RuntimeError(f"no zip URL in ODP response; keys={list(doc)[:10]}")

    file_url = urls[0]
    name = file_url.rsplit("/", 1)[-1]
    fd = _file_date(name) or datetime.now(UTC).date()
    s3_key = f"applications/{fd:%Y/%m}/{name}"

    try:
        s3.head_object(Bucket=RAW_BUCKET, Key=s3_key)
        return {"status": "already_have", "key": s3_key}
    except s3.exceptions.ClientError:
        pass

    # The download URL is not pre-signed: it needs the API key too.
    local = f"/tmp/{name}"
    dl = urllib.request.Request(file_url, headers={"X-API-KEY": key})
    with urllib.request.urlopen(dl, timeout=600) as r, open(local, "wb") as fh:
        while chunk := r.read(1 << 20):
            fh.write(chunk)
    s3.upload_file(local, RAW_BUCKET, s3_key)
    os.remove(local)

    # Invoke the loader directly rather than via an S3 notification. The
    # notification would make RawBucket and LoadFunction mutually dependent,
    # which CloudFormation rejects as a circular dependency. Calling it here is
    # also more explicit: replaying one day is a single invoke with a known key.
    target = os.environ.get("LOAD_FUNCTION")
    invoked = False
    if target:
        boto3.client("lambda").invoke(
            FunctionName=target,
            InvocationType="Event",
            Payload=json.dumps({"bucket": RAW_BUCKET, "key": s3_key}).encode(),
        )
        invoked = True

    # Second consumer of the same file: the lakehouse. Kept separate from the
    # Postgres loader so a Databricks outage cannot break the alerting pipeline,
    # and vice versa. Both are fire-and-forget.
    upload_target = os.environ.get("UPLOAD_FUNCTION")
    upload_invoked = False
    if upload_target:
        boto3.client("lambda").invoke(
            FunctionName=upload_target,
            InvocationType="Event",
            Payload=json.dumps({"bucket": RAW_BUCKET, "key": s3_key}).encode(),
        )
        upload_invoked = True

    return {
        "status": "uploaded",
        "key": s3_key,
        "file_date": fd.isoformat(),
        "load_invoked": invoked,
        "upload_invoked": upload_invoked,
    }


def load_handler(event, context):
    """
    Parse a raw zip from S3 into Postgres. Triggered by S3 ObjectCreated, or
    invoked directly with {"bucket": ..., "key": ...} to replay a past day.
    """
    import psycopg

    if "Records" in event:
        rec = event["Records"][0]["s3"]
        bucket, s3_key = rec["bucket"]["name"], rec["object"]["key"]
    else:
        bucket, s3_key = event["bucket"], event["key"]

    name = s3_key.rsplit("/", 1)[-1]
    fd = _file_date(name) or datetime.now(UTC).date()
    local = f"/tmp/{name}"
    s3.download_file(bucket, s3_key, local)

    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=False)
    run_id = start_run(conn, name, fd)
    conn.commit()

    records = 0
    counts: dict[str, int] = {}
    try:
        def counted():
            nonlocal records
            for cf in iter_case_files_from_zip(local):
                records += 1
                yield cf

        events = list(load_stream(conn, counted(), run_id, observed_on=fd))
        counts = dict(summarise(events))
        finish_run(conn, run_id, records, len(events))
        conn.commit()
    except Exception as e:
        conn.rollback()
        finish_run(conn, run_id, records, 0, error=str(e)[:2000])
        conn.commit()
        raise
    finally:
        conn.close()
        if os.path.exists(local):
            os.remove(local)

    return {
        "run_id": run_id,
        "file": name,
        "records": records,
        "events": counts,
        "publications": counts.get(EventType.PUBLISHED_FOR_OPPOSITION.value, 0),
    }


def migrate_handler(event, context):
    """
    Run SQL migration files against RDS, in order, exactly once each.

    Lives in the VPC because RDS is private and unreachable from outside it.

    State is tracked in schema_migration: a file that has been applied is
    skipped on the next run, so re-invoking is safe. Each file runs in its own
    transaction, so a failure leaves the database at the last good version
    rather than half-migrated.

    Invoke with {} to apply everything pending, or {"dry_run": true} to preview.
    """
    import pathlib

    import psycopg

    dry_run = bool(event.get("dry_run"))
    only = event.get("only")

    root = pathlib.Path(__file__).parent.parent / "migrations"
    files = sorted(p for p in root.glob("*.sql"))
    if only:
        files = [p for p in files if p.name == only]
    if not files:
        return {"error": f"no .sql files found in {root}", "only": only}

    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=False)
    applied, skipped, failed = [], [], None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS schema_migration (
                       filename    TEXT PRIMARY KEY,
                       applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                   )"""
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM schema_migration")
            done = {r[0] for r in cur.fetchall()}

        for path in files:
            if path.name in done:
                skipped.append(path.name)
                continue
            if dry_run:
                applied.append(f"WOULD RUN {path.name}")
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute(path.read_text())
                    cur.execute(
                        "INSERT INTO schema_migration (filename) VALUES (%s)",
                        (path.name,),
                    )
                conn.commit()
                applied.append(path.name)
            except Exception as e:
                conn.rollback()
                failed = {"file": path.name, "error": str(e)[:1500]}
                break
    finally:
        conn.close()

    return {"applied": applied, "skipped": skipped, "failed": failed, "dry_run": dry_run}


def query_handler(event, context):
    """
    Run a read-only SQL query against RDS and return rows as JSON.

    A development and operations tool: RDS is private, so there is no other way
    to inspect it. Invoke-only via IAM, no public URL, no write access enforced
    beyond the read-only transaction below.

    Invoke with {"sql": "SELECT ...", "params": [...], "limit": 100}.
    """
    import psycopg
    from psycopg.rows import dict_row

    sql = (event.get("sql") or "").strip()
    if not sql:
        return {"error": "no sql provided"}

    params = event.get("params") or None
    limit = int(event.get("limit") or 100)

    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        # read_only rejects INSERT/UPDATE/DELETE/DDL at the server, so a typo
        # here cannot damage the database.
        conn.read_only = True
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return {"rows": [], "note": "statement returned no rows"}
            rows = cur.fetchmany(limit)
        return {"row_count": len(rows), "rows": json.loads(json.dumps(rows, default=str))}
    except Exception as e:
        return {"error": str(e)[:1500]}
    finally:
        conn.close()


def databricks_upload_handler(event, context):
    """
    Push a parsed daily file into the Databricks bronze volume.

    Free Edition cannot read S3 directly: there is no storage credential and no
    external location, so Auto Loader over the raw bucket is not available. This
    pushes into Databricks instead of asking Databricks to pull, which the
    inbound files API does allow.

    In a paid workspace this function would not exist. The production design is
    an external location over the raw bucket with Auto Loader picking up new
    objects incrementally, and no copying at all.

    Invoke with {"bucket": ..., "key": ...}, or {} to use the newest object
    under the applications prefix.
    """
    import io
    import urllib.error

    import pyarrow as pa
    import pyarrow.parquet as pq

    from .parser import iter_case_files_from_zip

    cfg = json.loads(
        boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["DATABRICKS_SECRET"]
        )["SecretString"]
    )
    host, token = cfg["host"].rstrip("/"), cfg["token"]

    bucket = event.get("bucket") or RAW_BUCKET
    s3_key = event.get("key")
    if not s3_key:
        page = s3.list_objects_v2(Bucket=bucket, Prefix="applications/")
        objs = [o for o in page.get("Contents", []) if o["Key"].endswith(".zip")]
        if not objs:
            return {"error": "no zip objects found"}
        s3_key = max(objs, key=lambda o: o["LastModified"])["Key"]

    name = s3_key.rsplit("/", 1)[-1]
    local = f"/tmp/{name}"
    s3.download_file(bucket, s3_key, local)

    rows = []
    for cf in iter_case_files_from_zip(local):
        if not cf.serial_number:
            continue
        rows.append({
            "serial_number": cf.serial_number,
            "registration_number": cf.registration_number,
            "mark_identification": cf.mark_identification,
            "full_mark_text": cf.full_mark_text or None,
            "filing_date": cf.filing_date,
            "registration_date": cf.registration_date,
            "status_code": cf.status_code,
            "published_for_opposition_date": cf.published_for_opposition_date,
            "abandonment_date": cf.abandonment_date,
            "cancellation_date": cf.cancellation_date,
            "attorney_name": cf.attorney_name,
            "nice_classes": cf.nice_classes,
            "goods_classes": [c for c, _ in cf.goods_services],
            "goods_descriptions": [d for _, d in cf.goods_services],
            "pseudo_marks": cf.pseudo_marks,
            "translations": cf.translations,
            "transliterations": cf.transliterations,
            "owner_names": cf.owner_names,
            "source_file": name,
            "file_date": _file_date(name),
        })
    os.remove(local)

    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), buf, compression="snappy")
    payload = buf.getvalue()

    target = f"/Volumes/dev/bronze/raw_files/{name.replace('.zip', '.parquet')}"
    url = f"{host}/api/2.0/fs/files{target}?overwrite=true"
    req = urllib.request.Request(
        url,
        data=payload,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        return {"error": f"upload failed: HTTP {e.code}", "detail": e.read()[:400].decode("utf-8", "replace")}

    # Kick the DLT pipeline so bronze, silver and gold refresh from the file
    # we just pushed. Without this the tables only update when someone clicks
    # Run in the Databricks UI.
    pipeline_id = os.environ.get("DATABRICKS_PIPELINE_ID")
    triggered = None
    if pipeline_id:
        try:
            preq = urllib.request.Request(
                f"{host}/api/2.0/pipelines/{pipeline_id}/updates",
                data=json.dumps({"full_refresh": False}).encode(),
                method="POST",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(preq, timeout=60) as r:
                triggered = json.loads(r.read()).get("update_id")
        except urllib.error.HTTPError as e:
            # 409 means an update is already running, which is the outcome we
            # wanted. Anything else is a real failure worth surfacing.
            triggered = (
                "already running"
                if e.code == 409
                else f"trigger failed: HTTP {e.code} {e.read()[:200].decode('utf-8', 'replace')}"
            )

    return {
        "uploaded": target,
        "records": len(rows),
        "bytes": len(payload),
        "http_status": status,
        "pipeline_update": triggered,
    }
