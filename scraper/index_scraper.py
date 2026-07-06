import sqlite3
import time
import yaml
import requests
import certifi


# ---------------------------------------------------------------------------
# Step 1 — index
# ---------------------------------------------------------------------------

def get_api_total(api_url, retries=3):
    params = {
        "sf_culture": "en",
        "$top": 1,
        "$skip": 0,
        "$orderby": "PublicationDateAndTime desc",
        "$select": "DonId",
        "$count": "true",
    }
    for attempt in range(retries):
        try:
            resp = requests.get(api_url, params=params, timeout=60, verify=certifi.where())
            resp.raise_for_status()
            return resp.json().get("@odata.count", 0)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt == retries - 1:
                raise
            wait = 10 * (2 ** attempt)
            print(f"  get_api_total: retrying in {wait}s... ({e})")
            time.sleep(wait)


_CONTENT_FIELDS = "Summary,Overview,Epidemiology,Response,Assessment,Advice,FurtherInformation"


def fetch_index_page(api_url, skip, page_size, retries=3):
    params = {
        "sf_culture": "en",
        "$top": page_size,
        "$skip": skip,
        "$orderby": "PublicationDateAndTime desc",
        "$select": f"DonId,UrlName,Title,PublicationDateAndTime,{_CONTENT_FIELDS}",
    }
    for attempt in range(retries):
        try:
            resp = requests.get(api_url, params=params, timeout=60, verify=certifi.where())
            resp.raise_for_status()
            return resp.json().get("value", [])
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt == retries - 1:
                raise
            wait = 10 * (2 ** attempt)
            print(f"  fetch_index_page (skip={skip}): timeout/connection error, retrying in {wait}s... ({e})")
            time.sleep(wait)


def _build_content(item):
    parts = [item.get(f) or "" for f in _CONTENT_FIELDS.split(",")]
    return "\n\n".join(p for p in parts if p) or None


def step1_index(conn, base_url, api_url, page_size):
    # Exclude the two legacy empty-identifier rows that cannot be de-duplicated.
    conn.execute("DELETE FROM don WHERE identifier IS NULL OR TRIM(identifier) = ''")
    conn.commit()

    existing_ids = set(r[0] for r in conn.execute("SELECT identifier FROM don").fetchall())
    api_total = get_api_total(api_url)

    print(f"DB has {len(existing_ids)} DON(s), API reports {api_total} total")

    # Full scan when DB is missing records (backfill); incremental otherwise.
    full_scan = len(existing_ids) < api_total
    if full_scan:
        print("Full scan mode (backfill): fetching all pages.")
    else:
        print("Incremental mode: stopping when a full page of known IDs is found.")

    new_rows = []
    skip = 0

    while True:
        items = fetch_index_page(api_url, skip, page_size)
        if not items:
            break

        page_new = 0
        for item in items:
            don_id = item.get("DonId") or ""
            url_name = item.get("UrlName") or ""
            identifier = don_id or url_name
            if not identifier:
                continue  # no usable key; skip

            if identifier not in existing_ids:
                page_new += 1
                if don_id:
                    # Modern DON: content fetched separately in step 2.
                    url = f"{base_url}/emergencies/disease-outbreak-news/item/{don_id}"
                    content = None
                    scrape = 0
                else:
                    # Legacy DON (no DonId): content is in the index response.
                    url = f"{base_url}/emergencies/disease-outbreak-news/item/{url_name}"
                    content = _build_content(item)
                    scrape = 1

                new_rows.append({
                    "identifier": identifier,
                    "url": url,
                    "publication": item["PublicationDateAndTime"][:10],
                    "title": item["Title"],
                    "content": content,
                    "scrape": scrape,
                })
                existing_ids.add(identifier)

        # In incremental mode a full page of known IDs means we've caught up.
        if not full_scan and page_new == 0:
            break

        if len(items) < page_size:
            break

        skip += page_size
        if full_scan:
            time.sleep(1)  # be polite during large backfill

    if not new_rows:
        print("Step 1: no new DONs found.")
        return

    conn.executemany(
        "INSERT OR IGNORE INTO don (identifier, url, publication, title, content, scrape) "
        "VALUES (:identifier, :url, :publication, :title, :content, :scrape)",
        new_rows,
    )
    conn.commit()
    legacy = sum(1 for r in new_rows if r["scrape"] == 1)
    modern = len(new_rows) - legacy
    print(f"Step 1 complete: {len(new_rows)} new DON(s) added ({modern} modern, {legacy} legacy with inline content).")


# ---------------------------------------------------------------------------
# Step 2 — content scrape
# ---------------------------------------------------------------------------

def fetch_don_content(api_url, identifier, retries=3):
    params = {
        "sf_culture": "en",
        "$filter": f"DonId eq '{identifier}'",
        "$select": _CONTENT_FIELDS,
    }
    for attempt in range(retries):
        try:
            resp = requests.get(api_url, params=params, timeout=60, verify=certifi.where())
            resp.raise_for_status()
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt == retries - 1:
                raise
            wait = 10 * (2 ** attempt)
            print(f"  {identifier}: timeout/connection error, retrying in {wait}s... ({e})")
            time.sleep(wait)
    items = resp.json().get("value", [])
    if not items:
        return None
    return _build_content(items[0])


def step2_content(conn, api_url):
    pending = conn.execute(
        "SELECT identifier FROM don WHERE scrape = 0 ORDER BY publication DESC"
    ).fetchall()

    if not pending:
        print("Step 2: no DONs pending content scrape.")
        return

    print(f"Step 2: scraping content for {len(pending)} DON(s)...")
    for (identifier,) in pending:
        try:
            content = fetch_don_content(api_url, identifier)
        except requests.exceptions.SSLError as e:
            print(f"  {identifier}: SSL error — skipping for retry ({e})")
            continue
        except requests.exceptions.RequestException as e:
            print(f"  {identifier}: request error — skipping for retry ({e})")
            continue
        conn.execute(
            "UPDATE don SET content = ?, scrape = 1 WHERE identifier = ?",
            (content, identifier),
        )
        conn.commit()
        print(f"  {identifier}: {'ok' if content else 'empty'}")
        time.sleep(5)

    print("Step 2 complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    with open("config/config.yaml", "r") as f:
        config = yaml.safe_load(f)

    scraper = config["scraper"]

    with sqlite3.connect(config["database"]["path"]) as conn:
        step1_index(conn, scraper["base_url"], scraper["api_url"], scraper["page_size"])
        step2_content(conn, scraper["api_url"])
