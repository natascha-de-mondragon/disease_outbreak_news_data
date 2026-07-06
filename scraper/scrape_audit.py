"""
Scraper completeness audit.

Pages the entire WHO DON index with a STABLE sort (a unique tiebreaker, so
skip-based paging cannot drop or repeat rows), collects every identifier, and
diffs the result against the `don` table. The three headline counts tell you
why the DB total and the API $count disagree:

  rows returned < $count      -> pagination is dropping rows (unstable sort).
                                 Real gap. Fix the scraper's $orderby.
  distinct ids  < $count      -> the feed has duplicate/no-id rows; $count is
                                 inflated. The DB may already be complete.
  distinct ids == $count      -> the count is clean; any 'in API not in DB'
                                 identifiers are genuinely missing articles.

Read-only: this does not write to the database.

  python scraper/scrape_audit.py
"""

import sqlite3
import time

import certifi
import requests
import yaml


def fetch_page(api_url: str, skip: int, page_size: int, retries: int = 3):
    params = {
        "sf_culture": "en",
        "$top": page_size,
        "$skip": skip,
        # Unique tiebreaker makes skip/top paging deterministic. Without it,
        # same-day publication ties let the server reorder between requests and
        # rows fall through page boundaries.
        "$orderby": "PublicationDateAndTime desc,DonId desc",
        "$select": "DonId,UrlName",
        "$count": "true",
    }
    for attempt in range(retries):
        try:
            resp = requests.get(api_url, params=params, timeout=60, verify=certifi.where())
            resp.raise_for_status()
            data = resp.json()
            return data.get("value", []), data.get("@odata.count", 0)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt == retries - 1:
                raise
            wait = 10 * (2 ** attempt)
            print(f"  skip={skip}: retrying in {wait}s ({exc})")
            time.sleep(wait)


def audit(db_path: str, api_url: str, page_size: int) -> None:
    ids: set[str] = set()
    total_returned = 0
    duplicate_hits = 0
    api_count = None
    skip = 0

    while True:
        items, count = fetch_page(api_url, skip, page_size)
        if api_count is None:
            api_count = count
        if not items:
            break

        total_returned += len(items)
        for item in items:
            ident = (item.get("DonId") or item.get("UrlName") or "").strip()
            if not ident:
                continue
            if ident in ids:
                duplicate_hits += 1
            ids.add(ident)

        if len(items) < page_size:
            break
        skip += page_size
        time.sleep(0.5)

    with sqlite3.connect(db_path) as conn:
        db_ids = {r[0] for r in conn.execute("SELECT identifier FROM don") if r[0]}

    missing = ids - db_ids       # in the API, absent from the DB
    extra = db_ids - ids         # in the DB, absent from the API

    print("\n=== scraper completeness audit ===")
    print(f"API $count:              {api_count}")
    print(f"rows returned (paged):   {total_returned}")
    print(f"  of which duplicates:   {duplicate_hits}")
    print(f"distinct identifiers:    {len(ids)}")
    print(f"DB identifiers:          {len(db_ids)}")
    print(f"in API not in DB:        {len(missing)}")
    print(f"in DB not in API:        {len(extra)}")

    print()
    if api_count and total_returned < api_count:
        print(
            f"-> pagination returned {api_count - total_returned} fewer rows than $count.\n"
            "   The sort is dropping rows. Add the tiebreaker to the scraper's $orderby."
        )
    elif api_count and len(ids) < api_count:
        print(
            f"-> {api_count - len(ids)} of the counted rows are duplicates or have no id.\n"
            "   $count is inflated; the DB is complete once 'in API not in DB' is 0."
        )
    else:
        print("-> $count is clean; every gap is a genuinely missing or extra article.")

    if missing:
        print(f"\nMissing identifiers ({len(missing)} total, first 50):")
        for ident in sorted(missing)[:50]:
            print(f"  {ident}")
    if extra:
        print(f"\nIn DB but not in API ({len(extra)} total, first 20):")
        for ident in sorted(extra)[:20]:
            print(f"  {ident}")


if __name__ == "__main__":
    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)
    audit(
        db_path=config["database"]["path"],
        api_url=config["scraper"]["api_url"],
        page_size=config["scraper"]["page_size"],
    )
