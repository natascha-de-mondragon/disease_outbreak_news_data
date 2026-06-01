import re
import sqlite3
import sys

DB_PATH = "data/don_registry.db"

conn = sqlite3.connect(DB_PATH)

failures = []
warnings = []

def check(label, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not passed:
        failures.append(label)

def warn(label, detail=""):
    print(f"  [WARN] {label}" + (f" — {detail}" if detail else ""))
    warnings.append(label)


# ---------------------------------------------------------------------------
# 1. Row counts
# ---------------------------------------------------------------------------
print("\n=== Row counts ===")

total = conn.execute("SELECT COUNT(*) FROM don").fetchone()[0]
scraped = conn.execute("SELECT COUNT(*) FROM don WHERE scrape = 1").fetchone()[0]
pending = total - scraped

check("At least 1 000 DONs in registry", total >= 1000, f"{total} rows")
check("All DONs scraped (scrape=1)", pending == 0,
      f"{pending} pending — run step2_content to scrape, or investigate NULL/bad identifiers")

# ---------------------------------------------------------------------------
# 2. Content length
# ---------------------------------------------------------------------------
print("\n=== Content length ===")

stats = conn.execute(
    "SELECT MIN(length(content)), MAX(length(content)), AVG(length(content)) "
    "FROM don WHERE scrape = 1"
).fetchone()
min_len, max_len, avg_len = stats

check("Average content length > 5 000 chars", avg_len > 5000, f"avg={avg_len:.0f}")
check("Maximum content length < 2 000 000 chars", max_len < 2_000_000, f"max={max_len}")

# Very short (<100 chars) scraped rows are almost certainly truncated/broken.
very_short = conn.execute(
    "SELECT identifier, length(content) FROM don WHERE scrape=1 AND length(content) < 100"
).fetchall()
check("No scraped rows with content < 100 chars", len(very_short) == 0,
      f"{len(very_short)} rows: {[r[0] for r in very_short]}")

# Rows <500 chars exist legitimately (brief early-era updates) but worth surfacing.
short = conn.execute(
    "SELECT COUNT(*) FROM don WHERE scrape=1 AND length(content) < 500"
).fetchone()[0]
if short:
    warn(f"{short} scraped rows have content < 500 chars (may be brief update notices)")

# ---------------------------------------------------------------------------
# 3. NULL / empty content
# ---------------------------------------------------------------------------
print("\n=== NULL / empty content ===")

null_scraped = conn.execute(
    "SELECT COUNT(*) FROM don WHERE scrape=1 AND content IS NULL"
).fetchone()[0]
check("No scrape=1 rows with NULL content", null_scraped == 0, f"{null_scraped} rows")

empty_scraped = conn.execute(
    "SELECT COUNT(*) FROM don WHERE scrape=1 AND trim(content) = ''"
).fetchone()[0]
check("No scrape=1 rows with empty content", empty_scraped == 0, f"{empty_scraped} rows")

# ---------------------------------------------------------------------------
# 4. HTML sanity (most content is HTML; plain-text rows should be rare)
# ---------------------------------------------------------------------------
print("\n=== HTML sanity ===")

# Most DON content is wrapped in HTML tags. Flag rows with no tag at all —
# they are either plain text (acceptable) or truncated garbage (not acceptable).
no_html = conn.execute(
    "SELECT COUNT(*) FROM don WHERE scrape=1 AND content NOT LIKE '%<%'"
).fetchone()[0]
check("No scraped rows contain zero HTML tags", no_html == 0, f"{no_html} rows")

# Plain-text rows (no <p> specifically) should be rare — warn so we notice drift.
no_p = conn.execute(
    "SELECT COUNT(*) FROM don WHERE scrape=1 AND content NOT LIKE '%<p%'"
).fetchone()[0]
if no_p:
    warn(f"{no_p} scraped rows have no <p> tag (plain text or uses only other tags)")

# ---------------------------------------------------------------------------
# 5. Identifier format
# ---------------------------------------------------------------------------
print("\n=== Identifier format ===")

null_ids = conn.execute("SELECT COUNT(*) FROM don WHERE identifier IS NULL").fetchone()[0]
check("No rows with NULL identifier (scraper bug: DonId returned None)", null_ids == 0,
      f"{null_ids} rows")

identifiers = [r[0] for r in conn.execute(
    "SELECT identifier FROM don WHERE identifier IS NOT NULL"
).fetchall()]

# Accept both 2026-DON603 (recent) and 2000DON223 (older, no dash).
bad_ids = [i for i in identifiers if not re.fullmatch(r"\d{4}-?DON\d+", i)]
check("All identifiers match YYYY[-]DONnnn pattern", len(bad_ids) == 0,
      f"{len(bad_ids)} bad: {bad_ids}")

# ---------------------------------------------------------------------------
# 6. URL format
# ---------------------------------------------------------------------------
print("\n=== URL format ===")

base = "https://www.who.int/emergencies/disease-outbreak-news/item/"
bad_urls = conn.execute(
    "SELECT COUNT(*) FROM don WHERE url NOT LIKE ?", (base + "%",)
).fetchone()[0]
check("All URLs start with WHO DON base path", bad_urls == 0, f"{bad_urls} bad URLs")

# ---------------------------------------------------------------------------
# 7. Publication dates
# ---------------------------------------------------------------------------
print("\n=== Publication dates ===")

bad_dates = conn.execute(
    "SELECT COUNT(*) FROM don "
    "WHERE publication NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
).fetchone()[0]
check("All publication dates are YYYY-MM-DD", bad_dates == 0, f"{bad_dates} malformed")

earliest, latest = conn.execute(
    "SELECT MIN(publication), MAX(publication) FROM don"
).fetchone()
check("Earliest date is 1996 or later", earliest >= "1996-01-01", f"earliest={earliest}")
check("Latest date is not in the future", latest <= "2030-01-01", f"latest={latest}")

# ---------------------------------------------------------------------------
# 8. Title completeness
# ---------------------------------------------------------------------------
print("\n=== Titles ===")

missing_title = conn.execute(
    "SELECT COUNT(*) FROM don WHERE title IS NULL OR trim(title) = ''"
).fetchone()[0]
check("All rows have a non-empty title", missing_title == 0, f"{missing_title} rows")

# ---------------------------------------------------------------------------
# 9. scrape flag values
# ---------------------------------------------------------------------------
print("\n=== scrape flag ===")

bad_flag = conn.execute(
    "SELECT COUNT(*) FROM don WHERE scrape NOT IN (0, 1)"
).fetchone()[0]
check("scrape flag is always 0 or 1", bad_flag == 0, f"{bad_flag} bad values")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'='*50}")
print(f"Total DONs : {total}  |  Scraped: {scraped}  |  Pending: {pending}")
print(f"Content    : min={min_len}  avg={avg_len:.0f}  max={max_len}")
print(f"Date range : {earliest} → {latest}")
if warnings:
    print(f"Warnings   : {len(warnings)}")
if failures:
    print(f"\nFAILED ({len(failures)}): {', '.join(failures)}")
    sys.exit(1)
else:
    print("\nAll checks passed.")
