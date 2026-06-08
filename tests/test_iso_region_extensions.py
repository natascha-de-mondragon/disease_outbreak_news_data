import sqlite3

import pytest

from db.loaders.iso_region_extensions import COUNTRY_ALIASES, REGIONS, load_iso_region_extensions

SCHEMA = """
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
"""


def _make_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)


def test_inserts_all_region_codes(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    load_iso_region_extensions(db)
    with sqlite3.connect(db) as conn:
        codes = {r[0] for r in conn.execute("SELECT code FROM iso")}
    assert {code for code, _, _ in REGIONS}.issubset(codes)


def test_region_names_stored_correctly(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    load_iso_region_extensions(db)
    with sqlite3.connect(db) as conn:
        name_map = dict(conn.execute("SELECT code, name_en FROM iso").fetchall())
    for code, name, _ in REGIONS:
        assert name_map[code] == name


def test_inserts_all_aliases(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    load_iso_region_extensions(db)
    with sqlite3.connect(db) as conn:
        aliases = {r[0] for r in conn.execute("SELECT alias FROM iso_aliases")}
    for _, _, region_aliases in REGIONS:
        for alias in region_aliases:
            assert alias in aliases


def test_global_has_multi_country_alias(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    load_iso_region_extensions(db)
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT iso_code FROM iso_aliases WHERE alias = 'Multi-country'"
        ).fetchone()
    assert row is not None
    assert row[0] == "WHO.GLO"


def test_idempotent(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    load_iso_region_extensions(db)
    load_iso_region_extensions(db)
    with sqlite3.connect(db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM iso WHERE code LIKE 'WHO.%'"
        ).fetchone()[0]
    assert count == len(REGIONS)


def test_returns_alias_insert_count(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    # COUNTRY_ALIASES reference existing ISO codes; seed them so FK constraints pass
    with sqlite3.connect(db) as conn:
        for code, _ in COUNTRY_ALIASES:
            conn.execute(
                "INSERT OR IGNORE INTO iso (code, name_en) VALUES (?, ?)",
                (code, f"Dummy {code}"),
            )
    n = load_iso_region_extensions(db)
    expected = (
        sum(len(aliases) for _, _, aliases in REGIONS)
        + sum(len(aliases) for _, aliases in COUNTRY_ALIASES)
    )
    assert n == expected


def test_inserts_country_aliases(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    with sqlite3.connect(db) as conn:
        for code, _ in COUNTRY_ALIASES:
            conn.execute(
                "INSERT OR IGNORE INTO iso (code, name_en) VALUES (?, ?)",
                (code, f"Dummy {code}"),
            )
    load_iso_region_extensions(db)
    with sqlite3.connect(db) as conn:
        aliases = {r[0] for r in conn.execute("SELECT alias FROM iso_aliases")}
    for _, country_aliases in COUNTRY_ALIASES:
        for alias in country_aliases:
            assert alias in aliases


def test_country_aliases_idempotent(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    with sqlite3.connect(db) as conn:
        for code, _ in COUNTRY_ALIASES:
            conn.execute(
                "INSERT OR IGNORE INTO iso (code, name_en) VALUES (?, ?)",
                (code, f"Dummy {code}"),
            )
    load_iso_region_extensions(db)
    load_iso_region_extensions(db)
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM iso_aliases").fetchone()[0]
    expected = sum(len(aliases) for _, _, aliases in REGIONS) + sum(
        len(aliases) for _, aliases in COUNTRY_ALIASES
    )
    assert count == expected
