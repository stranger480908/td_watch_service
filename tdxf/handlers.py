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

    return {
        "status": "uploaded",
        "key": s3_key,
        "file_date": fd.isoformat(),
        "load_invoked": invoked,
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
