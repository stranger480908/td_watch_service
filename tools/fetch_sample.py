"""
Fetch USPTO trademark daily bulk files.

Run this on your own machine. It is not runnable from the Claude container,
whose network allowlist does not include USPTO hosts.

Two backends:

  legacy  bulkdata.uspto.gov directory listing. No API key. Try this first --
          it is the fastest path to a sample and needs no ID.me verification.
          May disappear as BDSS is retired; that is why the ODP path exists.

  odp     api.uspto.gov Bulk Data Directory. Requires an ODP API key in the
          X-API-KEY header, which requires a USPTO.gov account linked to a
          validated ID.me identity.

Usage:

    python fetch_sample.py --backend legacy --days 10 --dry-run
    python fetch_sample.py --backend legacy --days 10 --out data/raw
    USPTO_ODP_KEY=... python fetch_sample.py --backend odp --days 10

A ten-day window is the useful sample size: it spans at least one full week,
which is what you need to see whether publication volume is flat or bursty.

UNVERIFIED: the ODP response shape below is inferred from USPTO's published
endpoint mapping, not from a live call. The parser walks the JSON looking for
anything URL-shaped rather than assuming fixed keys, so it should survive minor
schema differences -- but check --dry-run output before trusting it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

LEGACY_BASE = "https://bulkdata.uspto.gov/data/trademark/dailyxml/applications/"
LEGACY_TTAB = "https://bulkdata.uspto.gov/data/trademark/dailyxml/ttab/"
ODP_BASE = "https://api.uspto.gov/api/v1/datasets/products"

PRODUCTS = {
    "applications": ("TRTDXFAP", LEGACY_BASE),
    "ttab": ("TTABTDXF", LEGACY_TTAB),
}

UA = "tmwatch-sample/0.1 (research; contact via USPTO account holder)"


def _get(url: str, headers: dict[str, str] | None = None, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _urls_in_json(node, pattern: re.Pattern) -> list[str]:
    """Walk arbitrary JSON and collect every string matching pattern."""
    found: list[str] = []
    if isinstance(node, dict):
        for v in node.values():
            found.extend(_urls_in_json(v, pattern))
    elif isinstance(node, list):
        for v in node:
            found.extend(_urls_in_json(v, pattern))
    elif isinstance(node, str) and pattern.search(node):
        found.append(node)
    return found


def list_legacy(product: str) -> list[tuple[str, str]]:
    """
    Scrape the BDSS directory listing for zip links.

    Returns (filename, absolute_url) pairs. Filenames look like apc250813.zip
    (product prefix + YYMMDD), which is how we date-filter them.
    """
    _, base = PRODUCTS[product]
    html = _get(base).decode("utf-8", errors="replace")
    names = sorted(set(re.findall(r'href="([^"]+\.zip)"', html, re.I)))
    out = []
    for n in names:
        out.append((n.rsplit("/", 1)[-1], urllib.parse.urljoin(base, n)))
    return out


def list_odp(product: str, since: date, until: date, api_key: str) -> list[tuple[str, str]]:
    short_name, _ = PRODUCTS[product]
    qs = urllib.parse.urlencode(
        {
            "fileDataFromDate": since.isoformat(),
            "fileDataToDate": until.isoformat(),
        }
    )
    url = f"{ODP_BASE}/{short_name}?{qs}"
    raw = _get(url, headers={"X-API-KEY": api_key, "Accept": "application/json"})
    doc = json.loads(raw)
    urls = _urls_in_json(doc, re.compile(r"^https?://\S+\.zip$", re.I))
    seen, out = set(), []
    for u in urls:
        name = urllib.parse.urlparse(u).path.rsplit("/", 1)[-1]
        if name not in seen:
            seen.add(name)
            out.append((name, u))
    if not out:
        print(
            "  ODP returned no .zip URLs. Response keys: "
            f"{list(doc)[:10] if isinstance(doc, dict) else type(doc).__name__}",
            file=sys.stderr,
        )
    return out


DATE_IN_NAME = re.compile(r"(\d{2})(\d{2})(\d{2})\.zip$", re.I)


def file_date(name: str) -> date | None:
    """Pull YYMMDD out of names like apc250813.zip. None if it doesn't match."""
    m = DATE_IN_NAME.search(name)
    if not m:
        return None
    yy, mm, dd = (int(g) for g in m.groups())
    try:
        return date(2000 + yy, mm, dd)
    except ValueError:
        return None


def download(url: str, dest: Path) -> int:
    """Download to a .part file then rename, so a partial file is never trusted."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    total = 0
    with urllib.request.urlopen(req, timeout=600) as resp, open(tmp, "wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
            total += len(chunk)
    tmp.rename(dest)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", choices=("legacy", "odp"), default="legacy")
    ap.add_argument("--product", choices=tuple(PRODUCTS), default="applications")
    ap.add_argument("--days", type=int, default=10, help="look back this many days")
    ap.add_argument("--out", default="data/raw")
    ap.add_argument("--dry-run", action="store_true", help="list, do not download")
    ap.add_argument("--api-key", default=os.environ.get("USPTO_ODP_KEY", ""))
    args = ap.parse_args()

    until = date.today()
    since = until - timedelta(days=args.days)
    out_dir = Path(args.out) / args.product

    print(f"backend={args.backend} product={args.product} window={since}..{until}")

    try:
        if args.backend == "legacy":
            candidates = list_legacy(args.product)
        else:
            if not args.api_key:
                print("odp backend needs --api-key or USPTO_ODP_KEY", file=sys.stderr)
                return 2
            candidates = list_odp(args.product, since, until, args.api_key)
    except urllib.error.HTTPError as e:
        print(f"listing failed: HTTP {e.code} {e.reason}", file=sys.stderr)
        if e.code in (401, 403):
            print("  -> auth rejected; the legacy backend may also now be gated", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"listing failed: {e.reason}", file=sys.stderr)
        return 1

    print(f"listing returned {len(candidates)} zip file(s)")

    wanted = []
    undated = []
    for name, url in candidates:
        d = file_date(name)
        if d is None:
            undated.append((name, url))
        elif since <= d <= until:
            wanted.append((d, name, url))
    wanted.sort()

    if undated:
        print(f"  {len(undated)} file(s) had no parseable date, e.g. {undated[0][0]}")
        print("  -> check the naming convention and widen DATE_IN_NAME if needed")

    if not wanted:
        print("nothing in window. Recent names seen:")
        for name, _ in candidates[-8:]:
            print(f"    {name}")
        return 1

    print(f"{len(wanted)} file(s) in window:")
    for d, name, _url in wanted:
        dest = out_dir / name
        mark = "have" if dest.exists() else "get "
        print(f"  [{mark}] {d}  {d:%a}  {name}")

    if args.dry_run:
        print("\ndry run, nothing downloaded")
        return 0

    got = 0
    for _d, name, url in wanted:
        dest = out_dir / name
        if dest.exists():
            continue
        try:
            n = download(url, dest)
            got += 1
            print(f"  downloaded {name} ({n/1e6:.1f} MB)")
        except Exception as e:
            print(f"  FAILED {name}: {e}", file=sys.stderr)

    print(f"\n{got} new file(s) in {out_dir}")
    print("next: python tools/analyze_sample.py --dir", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
