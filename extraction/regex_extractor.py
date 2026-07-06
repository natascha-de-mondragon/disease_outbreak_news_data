import re
import sqlite3
import unicodedata
import yaml


# ---------------------------------------------------------------------------
# Outbreak / advisory classification
# ---------------------------------------------------------------------------

# Patterns matched case-insensitively against don.title.
# A match sets is_outbreak = 0 (advisory / non-outbreak).
NON_OUTBREAK_PATTERNS = [
    # Travel / pilgrimage advisories
    r"traveller",
    r"pilgrimage to mecca",
    r"international travel and health",
    # Non-disease events
    r"hurricane",
    r"repatriation",
    r"silicone implants?",
    # Policy / guidance documents
    r"antimicrobial drugs in food animals?",
    r"surveillance standards?",
    r"influenza vaccine for",
    r"virus sharing",
    r"medical impact of use",
    # WHO statements / meetings
    r"statement by who",
    r"who scientific meeting",
    r"director.general",
    r"who director",
    # Rhetorical / retrospective titles
    r"what happens if",
    r"can .+ be eradicated",
    r"effect of patents",
    r"chronology",
    r"one hundred days",
    # Advisory subtitles — disease context but advisory framing
    r"necessary precautions?",
    r" - prevention of further cases",
    r"need for virus sharing",
    # Food / chemical safety advisories
    r"international food safety event",
    r"melamine.contaminated",
    # AMR situation reports (not outbreak case-counts)
    r"antimicrobial resistance.*global situation",
    r"vancomycin resistant",
    # Animals
    r"poultry",
    r"domestic cats"
    r"wild animals",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in NON_OUTBREAK_PATTERNS]


def is_outbreak(title: str) -> int:
    """Return 0 if the title matches any non-outbreak pattern, else 1."""
    return 0 if any(p.search(title) for p in _COMPILED) else 1


def tag_outbreak_status(db_path: str) -> dict:
    with sqlite3.connect(db_path) as conn:
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(don)")}
        if "is_outbreak" not in existing_cols:
            conn.execute(
                "ALTER TABLE don ADD COLUMN is_outbreak INTEGER DEFAULT 1"
            )

        rows = conn.execute("SELECT identifier, title FROM don").fetchall()
        conn.executemany(
            "UPDATE don SET is_outbreak = ? WHERE identifier = ?",
            [(is_outbreak(title), identifier) for identifier, title in rows],
        )

        n_advisory = conn.execute(
            "SELECT COUNT(*) FROM don WHERE is_outbreak = 0"
        ).fetchone()[0]

    return {"tagged": len(rows), "advisory": n_advisory, "outbreak": len(rows) - n_advisory}


# ---------------------------------------------------------------------------
# Outbreak table population
# ---------------------------------------------------------------------------

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'"})


def _normalise(text: str) -> str:
    """NFD-decompose, strip combining characters, normalise apostrophe variants."""
    nfd = unicodedata.normalize("NFD", text)
    base = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return base.translate(_APOSTROPHES)


def _build_iso_lookup(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT code, name_en FROM iso").fetchall()
    aliases = conn.execute("SELECT iso_code, alias FROM iso_aliases").fetchall()
    lookup = [(_normalise(name), code) for code, name in rows] + [(_normalise(alias), code) for code, alias in aliases]
    lookup.sort(key=lambda x: len(x[0]), reverse=True)
    return lookup


def _icd_terms(name: str) -> list[str]:
    """Split an ICD name/alias with bracket annotations into matchable terms.

    'VHF - [viral haemorrhagic fever] NOS' yields ['VHF - NOS', 'viral haemorrhagic fever'].
    Names without brackets yield a single-element list.
    """
    contents = re.findall(r"\[([^\]]+)\]", name)
    primary = re.sub(r"\s*\[[^\]]*\]", "", name)
    primary = re.sub(r"  +", " ", primary).strip(" \t-–,")
    terms = [primary] if primary else []
    for content in contents:
        content = content.strip()
        if content and content not in terms:
            terms.append(content)
    return terms


def _build_icd_lookup(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT code, name_en FROM icd").fetchall()
    aliases = conn.execute("SELECT icd_code, alias FROM icd_aliases").fetchall()
    lookup = []
    for code, name in rows:
        for term in _icd_terms(name):
            lookup.append((_normalise(term), code))
    for code, alias in aliases:
        for term in _icd_terms(alias):
            lookup.append((_normalise(term), code))
    lookup.sort(key=lambda x: len(x[0]), reverse=True)
    return lookup


def _find_codes(title: str, lookup: list[tuple[str, str]]) -> set[str]:
    title = _normalise(title)
    codes = set()
    for term, code in lookup:
        if re.search(r"(?<![a-zA-Z\-'])" + re.escape(term) + r"(?![a-zA-Z\-'])", title, re.IGNORECASE):
            codes.add(code)
    return codes


_BOUNDARY_PATTERN = r"(?<![a-zA-Z\-']){term}(?![a-zA-Z\-'])"


def _precompile_lookup(lookup: list[tuple[str, str]]) -> list[tuple[re.Pattern, str]]:
    return [
        (re.compile(_BOUNDARY_PATTERN.format(term=re.escape(term)), re.IGNORECASE), code)
        for term, code in lookup
    ]


def _find_codes_fast(title: str, patterns: list[tuple[re.Pattern, str]]) -> set[str]:
    """Like _find_codes but uses precompiled patterns — use for batch title processing."""
    title = _normalise(title)
    return {code for pat, code in patterns if pat.search(title)}


def _deduplicate_icd(codes: set[str]) -> set[str]:
    """Drop parent ICD codes when a more specific child code also matched."""
    return {c for c in codes if not any(other.startswith(c) and other != c for other in codes)}


_ALLOWED_TABLES = {
    "outbreaks",
    "outbreaks_regex",
    "outbreaks_ner",
    "outbreaks_llama",
    "outbreaks_claude",
}

_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def populate_outbreaks(db_path: str, table: str = "outbreaks_regex") -> dict:
    """Populate an outbreaks table from all DONs tagged is_outbreak=1.

    Run tag_outbreak_status() before this to exclude advisories.
    Creates one row per (iso_code, icd_code) combination found in each title;
    iso_code or icd_code will be NULL when no match is found.
    """
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"table {table!r} not in allowed set {sorted(_ALLOWED_TABLES)}")

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {table} (
                recorded        TEXT DEFAULT CURRENT_TIMESTAMP,
                id_outbreak     TEXT,
                year            INTEGER,
                iso_code        TEXT,
                icd_code        TEXT,
                disease_mention TEXT,
                country_mention TEXT,
                don             TEXT
            )"""
        )

        iso_lookup = _build_iso_lookup(conn)
        icd_lookup = _build_icd_lookup(conn)

        # Precompile once; avoids recompiling ~10k patterns for every DON title.
        iso_patterns = _precompile_lookup(iso_lookup)
        icd_patterns = _precompile_lookup(icd_lookup)

        dons = conn.execute(
            "SELECT identifier, publication, title FROM don WHERE is_outbreak = 1"
        ).fetchall()

        rows = []
        for identifier, publication, title in dons:
            m = _YEAR_RE.search(identifier) or (publication and _YEAR_RE.search(str(publication)))
            year = int(m.group(0)) if m else int(identifier[:4])
            iso_codes = _find_codes_fast(title, iso_patterns) or {None}
            icd_codes = _deduplicate_icd(_find_codes_fast(title, icd_patterns)) or {None}

            for iso_code in iso_codes:
                for icd_code in icd_codes:
                    id_outbreak = (
                        f"{year}{iso_code}{icd_code.replace('.', '')}"
                        if iso_code and icd_code else None
                    )
                    rows.append((id_outbreak, year, iso_code, icd_code, None, None, identifier))

        conn.execute(f"DELETE FROM {table}")
        conn.executemany(
            f"INSERT INTO {table}"
            " (id_outbreak, year, iso_code, icd_code, disease_mention, country_mention, don)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    return {"dons_processed": len(dons), "outbreak_rows": len(rows)}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Regex outbreak extractor")
    parser.add_argument("--db", default=None, help="Path to don_registry.db")
    parser.add_argument("--populate", action="store_true", help="Write the outbreaks table")
    parser.add_argument("--table", default="outbreaks_regex", help="Target table (populate mode)")
    args = parser.parse_args()

    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)
    db_path = args.db or config["database"]["path"]

    if args.populate:
        result = populate_outbreaks(db_path, table=args.table)
        print(
            f"Processed {result['dons_processed']} DONs -> {result['outbreak_rows']} rows"
        )
    else:
        result = tag_outbreak_status(db_path=db_path)
        print(
            f"Tagged {result['tagged']} DONs — "
            f"{result['outbreak']} outbreak, {result['advisory']} advisory"
        )


if __name__ == "__main__":
    main()
