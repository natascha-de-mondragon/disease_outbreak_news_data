import sqlite3

import pytest

from extraction.regex_extractor import (
    _build_icd_lookup,
    _build_iso_lookup,
    _deduplicate_icd,
    _find_codes,
    _icd_terms,
    _normalise,
    is_outbreak,
    populate_outbreaks,
    tag_outbreak_status,
)

SCHEMA = """
CREATE TABLE don (
    identifier  TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    publication DATE NOT NULL,
    title       TEXT NOT NULL,
    content     TEXT,
    scrape      INTEGER DEFAULT 0,
    is_outbreak INTEGER DEFAULT 1
);
CREATE TABLE iso (
    code    TEXT PRIMARY KEY,
    name_en TEXT NOT NULL
);
CREATE TABLE iso_aliases (
    id       INTEGER PRIMARY KEY,
    iso_code TEXT NOT NULL,
    alias    TEXT NOT NULL,
    UNIQUE (iso_code, alias),
    FOREIGN KEY (iso_code) REFERENCES iso(code)
);
CREATE TABLE icd (
    code    TEXT PRIMARY KEY,
    name_en TEXT NOT NULL
);
CREATE TABLE icd_aliases (
    id       INTEGER PRIMARY KEY,
    icd_code TEXT NOT NULL,
    alias    TEXT NOT NULL,
    FOREIGN KEY (icd_code) REFERENCES icd(code)
);
CREATE TABLE outbreaks (
    id_outbreak TEXT PRIMARY KEY,
    year        INTEGER NOT NULL,
    iso_code    TEXT,
    icd_code    TEXT,
    don         TEXT,
    FOREIGN KEY (don) REFERENCES don(identifier),
    FOREIGN KEY (iso_code) REFERENCES iso(code),
    FOREIGN KEY (icd_code) REFERENCES icd(code)
);
"""


def _make_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)


def _seed(path, dons=None, iso=None, icd=None, icd_aliases=None):
    with sqlite3.connect(path) as conn:
        for code, name in (iso or []):
            conn.execute("INSERT INTO iso (code, name_en) VALUES (?, ?)", (code, name))
        for code, name in (icd or []):
            conn.execute("INSERT INTO icd (code, name_en) VALUES (?, ?)", (code, name))
        for icd_code, alias in (icd_aliases or []):
            conn.execute(
                "INSERT INTO icd_aliases (icd_code, alias) VALUES (?, ?)", (icd_code, alias)
            )
        for identifier, pub, title, is_ob in (dons or []):
            conn.execute(
                "INSERT INTO don (identifier, url, publication, title, is_outbreak)"
                " VALUES (?, 'http://x', ?, ?, ?)",
                (identifier, pub, title, is_ob),
            )


# --- is_outbreak ---

def test_is_outbreak_plain_title():
    assert is_outbreak("Cholera – Nigeria") == 1


def test_is_outbreak_advisory_travel():
    assert is_outbreak("International travel and health – Update") == 0


def test_is_outbreak_advisory_director_general():
    assert is_outbreak("WHO Director-General statement on Ebola") == 0


def test_is_outbreak_advisory_chronology():
    assert is_outbreak("Chronology of SARS") == 0


# --- _deduplicate_icd ---

def test_deduplicate_keeps_child_drops_parent():
    assert _deduplicate_icd({"1D60.0", "1D60.0Z"}) == {"1D60.0Z"}


def test_deduplicate_keeps_unrelated_codes():
    result = _deduplicate_icd({"1D60.0", "1E31"})
    assert result == {"1D60.0", "1E31"}


def test_deduplicate_three_level_hierarchy():
    # 1C1C → 1C1C.0 → 1C1C.0Z: keep only 1C1C.0Z
    assert _deduplicate_icd({"1C1C", "1C1C.0", "1C1C.0Z"}) == {"1C1C.0Z"}


def test_deduplicate_empty_set():
    assert _deduplicate_icd(set()) == set()


def test_deduplicate_single_code():
    assert _deduplicate_icd({"1A00"}) == {"1A00"}


# --- _normalise ---

def test_normalise_strips_diacritics():
    assert _normalise("Côte d'Ivoire") == "Cote d'Ivoire"


def test_normalise_curly_apostrophe_to_straight():
    assert _normalise("People’s Republic") == "People's Republic"


def test_normalise_modifier_letter_apostrophe():
    assert _normalise("Peopleʼs Republic") == "People's Republic"


def test_normalise_combined_diacritics_and_apostrophe():
    # São Tomé → Sao Tome; right-quote normalised
    assert _normalise("São Tomé’s") == "Sao Tome's"


# --- _icd_terms ---

def test_icd_terms_no_brackets():
    assert _icd_terms("Cholera") == ["Cholera"]


def test_icd_terms_abbreviation_plus_expansion():
    terms = _icd_terms("VHF - [viral haemorrhagic fever] NOS")
    assert "VHF - NOS" in terms
    assert "viral haemorrhagic fever" in terms


def test_icd_terms_abbreviation_only():
    # "SARS - [severe acute respiratory syndrome]" → primary "SARS", expansion retained
    terms = _icd_terms("SARS - [severe acute respiratory syndrome]")
    assert "SARS" in terms
    assert "severe acute respiratory syndrome" in terms


def test_icd_terms_expansion_used_as_lookup_term(tmp_path):
    """Bracket content from an API alias should become a searchable term."""
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed(db, icd=[("1D86", "Viral haemorrhagic fever, NEC")],
          icd_aliases=[("1D86", "VHF - [viral haemorrhagic fever] NOS")])
    with sqlite3.connect(db) as conn:
        lookup = _build_icd_lookup(conn)
    terms = {term for term, _ in lookup}
    assert "viral haemorrhagic fever" in terms


def test_find_codes_bracket_expansion_matches_title(tmp_path):
    """A title containing the expanded form matches even when the alias has brackets."""
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed(db, icd=[("1D86", "Viral haemorrhagic fever, NEC")],
          icd_aliases=[("1D86", "VHF - [viral haemorrhagic fever] NOS")])
    with sqlite3.connect(db) as conn:
        lookup = _build_icd_lookup(conn)
    codes = _find_codes("Acute viral haemorrhagic fever – Congo", lookup)
    assert "1D86" in codes


# --- _find_codes boundary ---

def test_find_codes_normalises_title_diacritics(tmp_path):
    """Title with diacritics should match a lookup term stored without them."""
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed(db, iso=[("CIV", "Côte d'Ivoire")])
    with sqlite3.connect(db) as conn:
        lookup = _build_iso_lookup(conn)
    # Title uses curly apostrophe; lookup is normalised to straight
    codes = _find_codes("Cholera – Côte d’Ivoire", lookup)
    assert codes == {"CIV"}


def test_find_codes_hyphen_boundary(tmp_path):
    """'Guinea' must not match inside 'Guinea-Bissau'."""
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed(db, iso=[("GIN", "Guinea"), ("GNB", "Guinea-Bissau")])
    with sqlite3.connect(db) as conn:
        lookup = _build_iso_lookup(conn)
    codes = _find_codes("Cholera – Guinea-Bissau", lookup)
    assert codes == {"GNB"}


def test_find_codes_apostrophe_boundary(tmp_path):
    """Term ending before apostrophe-s should not match a standalone word."""
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed(db, iso=[("LAO", "Lao People's Democratic Republic"), ("PRK", "People's Republic")])
    with sqlite3.connect(db) as conn:
        lookup = _build_iso_lookup(conn)
    codes = _find_codes("Avian influenza – Lao People's Democratic Republic", lookup)
    assert "LAO" in codes
    # "People's Republic" alone should not match inside the longer term
    assert "PRK" not in codes


# --- populate_outbreaks ---

def test_populate_basic_row(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed(db,
        iso=[("NGA", "Nigeria")],
        icd=[("1A00", "Cholera")],
        dons=[("2020-DON001", "2020-01-01", "Cholera – Nigeria", 1)],
    )
    result = populate_outbreaks(db)
    assert result["dons_processed"] == 1
    assert result["outbreak_rows"] == 1
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT year, iso_code, icd_code FROM outbreaks").fetchone()
    assert row == (2020, "NGA", "1A00")


def test_populate_skips_advisory(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed(db,
        iso=[("NGA", "Nigeria")],
        icd=[("1A00", "Cholera")],
        dons=[
            ("2020-DON001", "2020-01-01", "Cholera – Nigeria", 1),
            ("2020-DON002", "2020-02-01", "Advisory update", 0),
        ],
    )
    populate_outbreaks(db)
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM outbreaks").fetchone()[0]
    assert count == 1


def test_populate_multi_country_creates_multiple_rows(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed(db,
        iso=[("NGA", "Nigeria"), ("GHA", "Ghana")],
        icd=[("1A00", "Cholera")],
        dons=[("2020-DON003", "2020-03-01", "Cholera – Nigeria and Ghana", 1)],
    )
    populate_outbreaks(db)
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM outbreaks").fetchone()[0]
    assert count == 2


def test_populate_no_iso_match_stores_null(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed(db,
        icd=[("1E31", "Avian influenza")],
        icd_aliases=[("1E31", "Pandemic (H1N1) 2009")],
        dons=[("2009-DON001", "2009-06-01", "Pandemic (H1N1) 2009 - update 1", 1)],
    )
    populate_outbreaks(db)
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT iso_code, icd_code FROM outbreaks").fetchone()
    assert row[0] is None
    assert row[1] == "1E31"


def test_populate_no_icd_match_stores_null(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed(db,
        iso=[("BRA", "Brazil")],
        dons=[("2020-DON004", "2020-04-01", "Unknown illness – Brazil", 1)],
    )
    populate_outbreaks(db)
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT iso_code, icd_code FROM outbreaks").fetchone()
    assert row[0] == "BRA"
    assert row[1] is None


def test_populate_idempotent(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed(db,
        iso=[("NGA", "Nigeria")],
        icd=[("1A00", "Cholera")],
        dons=[("2020-DON001", "2020-01-01", "Cholera – Nigeria", 1)],
    )
    populate_outbreaks(db)
    populate_outbreaks(db)
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM outbreaks").fetchone()[0]
    assert count == 1


def test_populate_year_extracted_from_publication(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed(db,
        dons=[("1997-DON999", "1997-11-15", "Some event", 1)],
    )
    populate_outbreaks(db)
    with sqlite3.connect(db) as conn:
        year = conn.execute("SELECT year FROM outbreaks").fetchone()[0]
    assert year == 1997


def test_populate_region_alias_matches(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed(db,
        iso=[("WHO.AMR", "Region of the Americas")],
        icd=[("1D2Z", "Dengue")],
        dons=[("2024-DON001", "2024-01-01", "Dengue – Region of the Americas", 1)],
    )
    populate_outbreaks(db)
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT iso_code FROM outbreaks").fetchone()
    assert row[0] == "WHO.AMR"
