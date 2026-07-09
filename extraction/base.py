"""
Shared extraction core. Every extractor (regex, ner, llama, claude) resolves
mentions to codes, builds the HDX key, and writes rows by going through here,
so they cannot drift on anything the evaluation reads. The only thing an
extractor supplies is `extract_fn(don_text) -> OutbreakExtraction`: how raw
text becomes disease/country mentions. Resolution, keying, dedup, schema, the
year rule, sampling, and the DB write are defined once, below.

Table names are validated against ALLOWED_TABLES before they reach any SQL,
because a table name cannot be bound as a `?` parameter and must never be
interpolated from unchecked input.
"""

import html
import logging
import random
import re
import sqlite3
from typing import Callable, List, Optional

from tqdm import tqdm

from pydantic import BaseModel

from extraction.regex_extractor import (
    _build_icd_lookup,
    _build_iso_lookup,
    _deduplicate_icd,
    _find_codes,
)

logger = logging.getLogger(__name__)

ALLOWED_TABLES = {
    "outbreaks",
    "outbreaks_regex",
    "outbreaks_ner",
    "outbreaks_llama",
    "outbreaks_claude",
}

# One prompt, shared by every LLM extractor, so Llama and Claude differ only by
# model, not by instructions.
SYSTEM_PROMPT = """\
You are extracting structured information from WHO Disease Outbreak News (DON) articles.

Return a list of outbreaks. Each outbreak is ONE disease together with the \
countries where that disease is actually occurring. Group by disease: if one \
disease is spreading across several countries, that is a single outbreak object \
with several countries; if the article describes two different diseases, return \
two separate outbreak objects, each with its own countries. Never mix a disease \
with a country that the text does not link to it.

Rules:
- Extract only diseases that are the subject of the current outbreak report, \
  not diseases mentioned as historical context, differentials, or comparisons.
- Extract only countries where cases are occurring, not countries that are only \
  monitoring, supplying aid, or mentioned in passing.
- Report each outbreak at COUNTRY level. If the text names only a city, province, \
  district, or other sub-national place, return the country that place is in \
  (e.g. a report about Gulu should return "Uganda"). Use the country's common \
  English name as it would appear on a map.
- Use disease and country names as they appear in the text (e.g. \
  "Ebola haemorrhagic fever", "Viet Nam", "Democratic Republic of the Congo"). \
  Do not translate to codes or abbreviations.
- For update articles, extract the same disease and country as the ongoing \
  outbreak being updated.
- If no outbreak can be identified with confidence, return an empty list."""


class Outbreak(BaseModel):
    disease_name: str
    country_names: List[str]


class OutbreakExtraction(BaseModel):
    """One disease per object, grouped with the countries where it occurs.

    Grouping is the fix for the phantom-pair problem: a flat disease-list by
    country-list cross product invents pairs the article never asserted the
    moment a DON carries more than one disease.
    """

    outbreaks: List[Outbreak]


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def strip_html(text: str) -> str:
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def parse_year(identifier: str, publication: Optional[str]) -> int:
    """Year is the DON publication year, taken deterministically.

    The WHO identifier is reliably year-first ("2024-DON518", "1996_01_22c-en"),
    so it is the primary source. Publication is a fallback and is searched for a
    4-digit year rather than sliced, because those strings are day-first
    ("30 January 2024") and slicing them would silently corrupt the year.
    """
    for src in (identifier, publication):
        if src:
            m = _YEAR_RE.search(str(src))
            if m:
                return int(m.group(0))
    raise ValueError(f"no parseable year in identifier={identifier!r} publication={publication!r}")


def hdx_key(year: int, iso_code: Optional[str], icd_code: Optional[str]) -> Optional[str]:
    """HDX-format key: year + iso3 + dotless ICD (e.g. '2024ARGA979').

    None when either code is unresolved, so an unresolved row is kept for
    diagnosis but excluded from the HDX comparison by construction.
    """
    if iso_code is None or icd_code is None:
        return None
    return f"{year}{iso_code}{icd_code.replace('.', '')}"


def check_table(table: str) -> str:
    if table not in ALLOWED_TABLES:
        raise ValueError(f"table {table!r} is not in ALLOWED_TABLES {sorted(ALLOWED_TABLES)}")
    return table


def ensure_schema(conn: sqlite3.Connection, table: str) -> None:
    """Create the raw table if absent and add mention columns if missing.

    id_outbreak is intentionally NOT unique: the same tuple legitimately comes
    from several DONs (original plus updates).
    """
    check_table(table)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            recorded         TEXT DEFAULT CURRENT_TIMESTAMP,
            id_outbreak      TEXT,     -- HDX key year+iso3+icd4, NULL if unresolved, non-unique
            year             INTEGER,
            iso_code         TEXT,     -- iso3, NULL if country mention unresolved
            icd_code         TEXT,     -- dotless ICD, NULL if disease mention unresolved
            disease_mention  TEXT,     -- raw string the extractor returned
            country_mention  TEXT,     -- raw string the extractor returned
            don              TEXT      -- source DON identifier (provenance)
        )
        """
    )
    for col in ("disease_mention", "country_mention"):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists


def fetch_dons(conn: sqlite3.Connection, limit: Optional[int], seed: int) -> list:
    """All outbreak-tagged DONs, or a seeded random sample of `limit` of them.

    Seeded so a sample is reproducible across runs: iterating a prompt against
    the identical draw is the whole point. `LIMIT` alone would return a fixed
    insertion-order slice, which biases every comparison to the same few DONs.
    """
    rows = conn.execute(
        "SELECT identifier, publication, title, content FROM don WHERE is_outbreak = 1"
    ).fetchall()
    if limit is not None and limit < len(rows):
        rows = random.Random(seed).sample(rows, limit)
    return rows


def ensure_progress_schema(conn: sqlite3.Connection) -> None:
    """Per-table record of which DONs have already been written, for `resume`."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS extraction_progress (
            table_name TEXT NOT NULL,
            don        TEXT NOT NULL,
            PRIMARY KEY (table_name, don)
        )
        """
    )


def _extract_rows(
    identifier: str,
    publication,
    title,
    content,
    extract_fn: Callable[[str], OutbreakExtraction],
    iso_lookup,
    icd_lookup,
    seen: set,
    counters: dict,
) -> tuple[list[tuple], bool]:
    """Run one DON through `extract_fn`, resolving it to zero or more table rows.

    Returns `(rows, ok)`. `ok` is False on a parse/extraction failure, so the
    caller can leave a failed DON out of `extraction_progress` and let
    `--resume` retry it, rather than silently skipping it forever.
    """
    try:
        year = parse_year(identifier, publication)
    except ValueError as exc:
        logger.warning("Skipping %s: %s", identifier, exc)
        counters["errors"] += 1
        return [], False

    don_text = f"Title: {title}\n\n{strip_html(content or '')}"

    try:
        extraction = extract_fn(don_text)
    except Exception as exc:
        logger.warning("Extraction failed for %s: %s", identifier, exc)
        counters["errors"] += 1
        return [], False

    if not extraction.outbreaks:
        counters["empty"] += 1

    rows: list[tuple] = []
    # Resolve each disease against ONLY its own countries, so distinct
    # diseases in one DON are never cross-paired.
    for ob in extraction.outbreaks:
        disease = (ob.disease_name or "").strip()
        if not disease:
            continue

        icd_codes: set[Optional[str]] = _deduplicate_icd(set(_find_codes(disease, icd_lookup)))
        if not icd_codes:
            icd_codes = {None}
            counters["unresolved_disease"] += 1

        for raw_country in ob.country_names:
            country = (raw_country or "").strip()
            if not country:
                continue

            iso_codes: set[Optional[str]] = set(_find_codes(country, iso_lookup))
            if not iso_codes:
                iso_codes = {None}
                counters["unresolved_country"] += 1

            for iso_code in iso_codes:
                for icd_code in icd_codes:
                    dedup = (identifier, iso_code, icd_code, disease.lower(), country.lower())
                    if dedup in seen:
                        continue
                    seen.add(dedup)
                    rows.append(
                        (
                            hdx_key(year, iso_code, icd_code),
                            year,
                            iso_code,
                            icd_code,
                            disease,
                            country,
                            identifier,
                        )
                    )
    return rows, True


def run(
    db_path: str,
    table: str,
    extract_fn: Callable[[str], OutbreakExtraction],
    *,
    limit: Optional[int] = None,
    seed: int = 42,
    batch_size: Optional[int] = None,
    resume: bool = False,
) -> dict:
    """Drive one extractor end to end and repopulate `table`.

    Left at its defaults (`batch_size=None, resume=False`), behaviour is
    unchanged from before: everything is extracted into memory first, and the
    table is only cleared and rewritten once the whole run has succeeded, so a
    crash mid-run leaves prior data untouched.

    `batch_size` and `resume` are for slow, model-backed extractors that may be
    paused. With `batch_size` set, rows are committed to `table` (and the DONs
    marked done in `extraction_progress`) every N DONs, so an interruption
    loses at most one batch. `resume=True` skips DONs already marked done for
    this table instead of clearing it, so a paused run can continue rather
    than starting over; `resume=False` clears `table` and its progress record
    up front, as a fresh batched run needs a well-defined starting point.
    """
    table = check_table(table)
    incremental = batch_size is not None or resume

    with sqlite3.connect(db_path) as conn:
        ensure_schema(conn, table)
        ensure_progress_schema(conn)

        if incremental and not resume:
            conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM extraction_progress WHERE table_name = ?", (table,))
            conn.commit()

        iso_lookup = _build_iso_lookup(conn)
        icd_lookup = _build_icd_lookup(conn)
        dons = fetch_dons(conn, limit, seed)

        done_dons: set = set()
        if resume:
            done_dons = {
                row[0]
                for row in conn.execute(
                    "SELECT don FROM extraction_progress WHERE table_name = ?", (table,)
                )
            }
        pending = [d for d in dons if d[0] not in done_dons] if resume else dons

        seen: set[tuple] = set()
        counters = {"errors": 0, "empty": 0, "unresolved_country": 0, "unresolved_disease": 0}

        if not incremental:
            rows: list[tuple] = []
            for identifier, publication, title, content in tqdm(pending, desc="extracting", unit="DON"):
                don_rows, _ok = _extract_rows(
                    identifier, publication, title, content,
                    extract_fn, iso_lookup, icd_lookup, seen, counters,
                )
                rows.extend(don_rows)

            conn.execute(f"DELETE FROM {table}")
            conn.executemany(
                f"INSERT INTO {table}"
                " (id_outbreak, year, iso_code, icd_code, disease_mention, country_mention, don)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            return {"dons_processed": len(pending), "outbreak_rows": len(rows), **counters}

        total_rows = 0
        batch_rows: list[tuple] = []
        batch_dons: list[str] = []

        def flush():
            nonlocal batch_rows, batch_dons, total_rows
            if not batch_dons:
                return
            conn.executemany(
                f"INSERT INTO {table}"
                " (id_outbreak, year, iso_code, icd_code, disease_mention, country_mention, don)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                batch_rows,
            )
            conn.executemany(
                "INSERT OR IGNORE INTO extraction_progress (table_name, don) VALUES (?, ?)",
                [(table, d) for d in batch_dons],
            )
            conn.commit()
            total_rows += len(batch_rows)
            batch_rows = []
            batch_dons = []

        for identifier, publication, title, content in tqdm(pending, desc="extracting", unit="DON"):
            don_rows, ok = _extract_rows(
                identifier, publication, title, content,
                extract_fn, iso_lookup, icd_lookup, seen, counters,
            )
            batch_rows.extend(don_rows)
            if ok:
                batch_dons.append(identifier)
            if batch_size and len(batch_dons) >= batch_size:
                flush()

        flush()

    return {
        "dons_processed": len(pending),
        "dons_skipped": len(dons) - len(pending),
        "outbreak_rows": total_rows,
        **counters,
    }


def load_db_path(cli_db: Optional[str]) -> str:
    if cli_db:
        return cli_db
    import yaml

    with open("config/config.yaml") as f:
        return yaml.safe_load(f)["database"]["path"]


def print_stats(result: dict) -> None:
    skipped = result.get("dons_skipped")
    skipped_line = f" ({skipped} already done, skipped)" if skipped else ""
    print(
        f"Processed {result['dons_processed']} DONs{skipped_line} -> {result['outbreak_rows']} rows\n"
        f"  Errors:              {result['errors']}\n"
        f"  Empty extractions:   {result['empty']}\n"
        f"  Unresolved country:  {result['unresolved_country']}\n"
        f"  Unresolved disease:  {result['unresolved_disease']}"
    )
